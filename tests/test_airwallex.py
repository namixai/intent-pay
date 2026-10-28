"""The Airwallex adapter on hand-built answers. The cassettes in tests/fixtures/airwallex/ follow
the API reference; each says in `_source` whether it was recorded from the sandbox."""

import json
import pathlib
from decimal import Decimal

import pytest

from intent_pay.adapters.airwallex import (CARD_REASONS, DEMO_CARDHOLDER, MERCHANT_INFO, AirwallexClient, AirwallexConfig, AirwallexError,
                                           AirwallexPayer, ConfigError, card_controls)
from intent_pay.adapters.http import ReplayTransport, Response
from intent_pay.agent import ScriptedAgent, Scenario, run_step
from intent_pay.gateway import Gateway, GatewayError
from intent_pay.intent import Intent
from intent_pay.ledger import Ledger, Receipt
from intent_pay.policy import Engine, Policy, Reason

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "airwallex"
SCENARIO = ROOT / "scenarios" / "airwallex-demo.json"
POLICY = Policy.load(ROOT / "policies" / "airwallex-sandbox.toml")
KEY = "api-key-never-printed"
MONTHLY = {"request_id": "acme-nov-t", "amount": "49.00", "currency": "USD", "merchant_category": "7372",
           "purpose": "Acme Analytics, November", "billing": "monthly"}
RECEIPT = Receipt(decision="allow", reason="ok", detail="", request_id="acme-nov-t", intent=MONTHLY,
                  policy_hash="p", at="2026-10-28T00:00:00Z", seq=1, prev="0", hash="ab" * 32)


class Fake:
    """A transport that answers from a list of (status, body) in order and keeps every call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": json.loads(body) if body else None})
        status, payload = self.answers.pop(0)
        return Response(status, json.dumps(payload).encode(), {})

    def paths(self):
        return [c["url"].split(".com", 1)[1] for c in self.calls]


LOGIN = (201, {"token": "tok-1", "expires_at": "2026-10-28T13:00:00+0000"})


def payer(transport, cardholder_id="ch-1", policy=POLICY):
    config = AirwallexConfig(client_id="cid", api_key=KEY, cardholder_id=cardholder_id)
    return AirwallexPayer(AirwallexClient(config, transport), policy, sleep=lambda _s: None)


def test_config_reads_env_names_only_and_calls_the_sandbox_only():
    with pytest.raises(ConfigError, match="AIRWALLEX_CLIENT_ID, AIRWALLEX_API_KEY"):
        AirwallexConfig.from_env({})
    with pytest.raises(ConfigError, match="sandbox"):
        AirwallexConfig.from_env({"AIRWALLEX_CLIENT_ID": "c", "AIRWALLEX_API_KEY": "k",
                                  "AIRWALLEX_API_BASE": "https://api.airwallex.com"})
    config = AirwallexConfig.from_env({"AIRWALLEX_CLIENT_ID": "c", "AIRWALLEX_API_KEY": KEY})
    assert config.base_url == "https://api.sandbox.airwallex.com" and KEY not in repr(config)


def test_card_controls_repeat_the_policy_and_cap_the_card_at_the_allowed_amount():
    controls = card_controls(POLICY, Intent.parse(MONTHLY))
    assert controls["allowed_currencies"] == ["USD"]
    assert controls["allowed_merchant_categories"] == ["5734", "7372"]
    assert controls["transaction_limits"] == {"currency": "USD", "limits": [
        {"amount": 60, "interval": "PER_TRANSACTION"}, {"amount": 100, "interval": "DAILY"},
        {"amount": 49, "interval": "ALL_TIME"}]}


def test_pay_issues_a_card_then_authorizes_and_captures_on_it():
    t = Fake(LOGIN, (202, {"card_id": "card-1", "card_status": "ACTIVE"}),
             (201, {"process_result": "APPROVED", "card_transaction_lifecycle_id": "lc-1"}),
             (200, {"process_result": "APPROVED", "type": "CLEARING"}))
    result = payer(t).pay(Intent.parse(MONTHLY), RECEIPT)
    assert result.ok and result.status == "CLEARED" and result.reference == "card-1"
    assert t.paths() == ["/api/v1/authentication/login", "/api/v1/issuing/cards/create",
                         "/api/v1/simulation/issuing/create",
                         "/api/v1/simulation/issuing/card_transaction_lifecycles/lc-1/capture"]
    card = t.calls[1]["body"]
    assert card["request_id"] == "acme-nov-t-card" and card["cardholder_id"] == "ch-1"
    assert card["form_factor"] == "VIRTUAL" and card["client_data"] == "receipt:1:" + "ab" * 8
    assert card["authorization_controls"] == card_controls(POLICY, Intent.parse(MONTHLY))
    auth = t.calls[2]["body"]
    assert auth == {"card_id": "card-1", "transaction_amount": 49, "transaction_currency": "USD",
                    "merchant_info": MERCHANT_INFO, "merchant_category_code": "7372"}
    assert len(MERCHANT_INFO) == 43
    # The key goes only to the login call; the rest carry the token.
    assert t.calls[0]["headers"]["x-api-key"] == KEY
    assert all("x-api-key" not in c["headers"] and c["headers"]["Authorization"] == "Bearer tok-1" for c in t.calls[1:])


def test_pay_waits_for_a_pending_card():
    t = Fake(LOGIN, (202, {"card_id": "card-1", "card_status": "PENDING"}), (200, {"card_status": "PENDING"}),
             (200, {"card_status": "ACTIVE"}), (201, {"process_result": "APPROVED", "card_transaction_lifecycle_id": "lc"}),
             (200, {"process_result": "APPROVED"}))
    result = payer(t).pay(Intent.parse(MONTHLY), RECEIPT)
    assert result.ok and [s["call"] for s in result.steps] == ["create_card", "get_card", "get_card", "authorize", "capture"]


def test_a_card_that_never_activates_is_not_charged():
    t = Fake(LOGIN, (202, {"card_id": "card-1", "card_status": "PENDING"}), *[(200, {"card_status": "PENDING"})] * 5)
    result = payer(t).pay(Intent.parse(MONTHLY), RECEIPT)
    assert not result.ok and result.status == "CARD_PENDING" and result.reference is None
    assert "/api/v1/simulation/issuing/create" not in t.paths()


def test_a_declined_authorization_is_not_paid_and_names_the_reason():
    t = Fake(LOGIN, (202, {"card_id": "card-1", "card_status": "ACTIVE"}),
             (201, {"process_result": "DECLINED", "failure_reason": "INSUFFICIENT_FUNDS"}))
    result = payer(t).pay(Intent.parse(MONTHLY), RECEIPT)
    assert not result.ok and result.status == "DECLINED:INSUFFICIENT_FUNDS" and result.reference is None
    assert len(t.calls) == 3  # no capture


def test_a_card_error_comes_back_with_airwallex_code():
    t = Fake(LOGIN, (400, {"code": "validation_error", "message": "cardholder_id is not ready"}))
    result = payer(t).pay(Intent.parse(MONTHLY), RECEIPT)
    assert not result.ok and result.status == "validation_error" and "not ready" in result.detail


def test_without_a_cardholder_the_gateway_finds_or_creates_one():
    t = Fake(LOGIN, (200, {"items": [], "has_more": False}), (202, {"cardholder_id": "ch-new", "status": "PENDING"}),
             (200, "OK"), (202, {"card_id": "card-1", "card_status": "ACTIVE"}),
             (201, {"process_result": "APPROVED", "card_transaction_lifecycle_id": "lc"}), (200, {"process_result": "APPROVED"}))
    p = payer(t, cardholder_id=None)
    assert p.pay(Intent.parse(MONTHLY), RECEIPT).ok
    assert t.calls[2]["body"] == DEMO_CARDHOLDER and t.paths()[3] == "/api/v1/simulation/issuing/cardholders/ch-new/pass_review"
    assert t.calls[4]["body"]["cardholder_id"] == "ch-new"
    # The next payment reuses it.
    t.answers = [(202, {"card_id": "card-2", "card_status": "ACTIVE"}),
                 (201, {"process_result": "APPROVED", "card_transaction_lifecycle_id": "lc2"}), (200, {"process_result": "APPROVED"})]
    assert p.pay(Intent.parse({**MONTHLY, "request_id": "acme-2"}), RECEIPT).reference == "card-2"
    assert t.calls[-3]["body"]["cardholder_id"] == "ch-new"


def test_a_vendor_charge_that_the_card_declines():
    t = Fake(LOGIN, (201, {"process_result": "DECLINED", "failure_reason": "LIMIT_EXCEEDED", "card_transaction_lifecycle_id": "lc"}))
    result = payer(t).simulate_charge("card-1", Decimal("470.00"), "USD", "7372")
    assert not result.ok and result.status == "DECLINED:LIMIT_EXCEEDED" and result.operation == "forced_charge"


def test_a_vendor_charge_the_card_lets_through_is_reversed_and_reported():
    t = Fake(LOGIN, (201, {"process_result": "APPROVED", "card_transaction_lifecycle_id": "lc"}), (200, {"process_result": "APPROVED"}))
    result = payer(t).simulate_charge("card-1", Decimal("470.00"), "USD", "7372")
    assert result.ok and result.status == "APPROVED" and "reversed" in result.detail
    assert t.paths()[-1] == "/api/v1/simulation/issuing/card_transaction_lifecycles/lc/reverse"


def test_reserve_reads_the_available_balance():
    t = Fake(LOGIN, (200, [{"currency": "EUR", "available_amount": 9}, {"currency": "USD", "available_amount": 5000.5}]))
    assert payer(t).available_balance("USD") == Decimal("5000.5")
    t = Fake(LOGIN, (200, [{"currency": "EUR", "available_amount": 9}]))
    assert payer(t).available_balance("USD") == 0


def test_an_unreadable_balance_refuses_the_payment(tmp_path):
    t = Fake(LOGIN, (500, {"code": "service_unavailable"}))
    p = payer(t)
    receipt = Engine(POLICY, Ledger(tmp_path / "l.jsonl"), balance=p.available_balance).decide(MONTHLY)
    assert receipt.reason == Reason.BALANCE_UNAVAILABLE


def test_a_low_balance_refuses_before_any_card_exists(tmp_path):
    t = Fake(LOGIN, (200, [{"currency": "USD", "available_amount": 1020}]))
    p = payer(t)
    outcome = Gateway(Engine(POLICY, Ledger(tmp_path / "l.jsonl"), balance=p.available_balance), p).submit(MONTHLY)
    assert outcome.receipt.reason == Reason.BELOW_MIN_BALANCE and outcome.payment is None and len(t.calls) == 2


def test_token_is_reused_and_renewed_after_a_401():
    t = Fake(LOGIN, (200, []), (401, {"code": "credentials_expired"}), (201, {"token": "tok-2"}), (200, []))
    client = AirwallexClient(AirwallexConfig(client_id="cid", api_key=KEY), t)
    client.balances()
    client.balances()
    assert t.paths().count("/api/v1/authentication/login") == 2
    assert t.calls[-1]["headers"]["Authorization"] == "Bearer tok-2"


def test_a_failed_login_says_so_without_the_key():
    t = Fake((403, {"code": "unauthorized", "message": "Forbidden"}))
    client = AirwallexClient(AirwallexConfig(client_id="cid", api_key=KEY), t)
    with pytest.raises(AirwallexError) as err:
        client.balances()
    assert "403 unauthorized" in str(err.value) and KEY not in str(err.value)


def test_refunds_are_not_built_and_say_so():
    assert payer(Fake()).refund("card-1", "r").status == "NOT_BUILT"


def test_card_reasons_cover_the_rules_a_card_can_check():
    assert set(CARD_REASONS) == {"per_payment_limit_exceeded", "daily_limit_exceeded", "currency_not_allowed",
                                 "merchant_category_not_allowed"}


# The gateway and the scenario, on the cassettes.

def gateway_on(cassette, ledger):
    p = AirwallexPayer(AirwallexClient(AirwallexConfig("replay", "replay"), ReplayTransport(cassette)), POLICY,
                       sleep=lambda _s: None)
    return Gateway(Engine(POLICY, ledger, balance=p.available_balance), p)


def test_force_charge_needs_an_allowed_payment_with_a_card(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl")
    with pytest.raises(GatewayError, match="no allowed decision"):
        gateway_on(FIXTURES / "vendor-charge-declined.json", ledger).force_charge("acme-nov-t", "470.00", "USD", "7372")
    gateway_on(FIXTURES / "pay-cleared.json", ledger).submit({**MONTHLY, "request_id": "acme-nov-t"})
    outcome = gateway_on(FIXTURES / "vendor-charge-declined.json", ledger).force_charge("acme-nov-t", "470.00", "USD", "7372")
    assert outcome.payment.status == "DECLINED:LIMIT_EXCEEDED"
    assert outcome.execution.decision_seq == 1 and outcome.execution.operation == "forced_charge"
    assert ledger.verify() == 3


def test_force_charge_never_touches_a_refused_request(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl")
    gateway_on(FIXTURES / "vendor-charge-declined.json", ledger).submit({**MONTHLY, "amount": "470.00"})
    with pytest.raises(GatewayError, match="no allowed decision"):
        gateway_on(FIXTURES / "vendor-charge-declined.json", ledger).force_charge("acme-nov-t", "470.00", "USD", "7372")


def test_force_charge_needs_a_payer_that_can_play_the_vendor(tmp_path):
    class NoSimulation:
        provider = "paypal"
    with pytest.raises(GatewayError, match="cannot simulate"):
        Gateway(Engine(POLICY, Ledger(tmp_path / "l.jsonl")), NoSimulation()).force_charge("r", "1.00", "USD", None)


def test_scenario_runs_the_four_submitted_steps(tmp_path):
    scenario, ledger = Scenario.load(SCENARIO), Ledger(tmp_path / "l.jsonl")
    steps = [run_step(scenario, i, ScriptedAgent(), ledger, "t") for i in range(4)]
    decisions = [e["agent_sees"] for s in steps for e in s if e["kind"] == "decision"]
    assert [d["reason"] for d in decisions] == ["ok", "per_payment_limit_exceeded", "duplicate_request_id"]
    assert decisions[0]["payment"]["status"] == "CLEARED" and "payment" not in decisions[1]
    charge = [e for e in steps[2] if e["kind"] == "vendor_charge"][0]
    assert charge["compare"] == {"request_id": "acme-dec-t", "receipt": 3, "reason": "per_payment_limit_exceeded",
                                 "expected": "LIMIT_EXCEEDED", "card": "LIMIT_EXCEEDED", "match": True}
    assert [e["kind"] for e in steps[2]] == ["task", "vendor_charge"]  # no agent, no proposal
    assert "Nothing was sent to Airwallex" in steps[1][-1]["text"]
    assert ledger.verify() == 5


def test_refused_steps_have_no_recording_and_make_no_call(tmp_path):
    scenario = Scenario.load(SCENARIO)
    assert "replay" not in scenario.steps[1] and "replay" not in scenario.steps[3]
    # With an empty recording, any sandbox call would fail the step; these steps pass.
    ledger = Ledger(tmp_path / "l.jsonl")
    for i in range(4):
        run_step(scenario, i, ScriptedAgent(), ledger, "t")
    assert ledger.verify() == 5


def test_vendor_step_before_any_payment_says_what_is_missing(tmp_path):
    events = run_step(Scenario.load(SCENARIO), 2, ScriptedAgent(), Ledger(tmp_path / "l.jsonl"), "t")
    assert events[-1]["kind"] == "error" and "no allowed decision" in events[-1]["text"]


def test_fixtures_say_whether_they_were_recorded():
    for path in FIXTURES.glob("*.json"):
        source = json.loads(path.read_text())["_source"]
        assert "recorded" in source, path
