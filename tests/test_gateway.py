"""The gateway joins the policy to the sandbox. These tests hold it to three promises: a refused
proposal makes no sandbox call, every sandbox answer lands in the ledger next to its decision,
and nothing the agent reads carries a key or card data."""

import json
import os
import pathlib
import subprocess
import sys
from decimal import Decimal

import pytest

from conftest import POLICY, intent
from intent_pay.adapters.airwallex import AirwallexClient, AirwallexConfig, AirwallexPayer
from intent_pay.adapters.http import ReplayTransport
from intent_pay.adapters.paypal import CardSource, PayPalClient, PayPalConfig, PayPalPayer
from intent_pay.gateway import Gateway, GatewayError
from intent_pay.ledger import Ledger, LedgerError
from intent_pay.policy import Engine, Policy

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "paypal"
AIRWALLEX_FIX = ROOT / "tests" / "fixtures" / "airwallex"
SECRET = "client-secret-never-printed"
CARD_NUMBER = "4012888888881881"
CAPTURE = "5S461376DW338105P"  # recorded in pay-completed.json
# The real environment (Windows needs SYSTEMROOT), minus anything that could carry keys into the test.
CLEAN_ENV = {**{k: v for k, v in os.environ.items() if not k.startswith(("PAYPAL_", "AIRWALLEX_", "INTENT_PAY_"))},
             "PYTHONPATH": str(ROOT / "src")}


def gateway(ledger, clock, cassette=None, exchanges=None):
    transport = ReplayTransport(FIX / cassette if cassette else {"exchanges": exchanges or []})
    cfg = PayPalConfig(client_id="id", client_secret=SECRET,
                       card=CardSource(number=CARD_NUMBER, expiry="2030-12", security_code="123"))
    engine = Engine(Policy.from_mapping(POLICY), ledger, balance=lambda c: Decimal(2000), clock=clock)
    return Gateway(engine, PayPalPayer(PayPalClient(cfg, transport)), clock=clock), transport


def held_gateway(ledger, clock, cassette=None):
    policy = {**POLICY, "currencies": {**POLICY["currencies"],
                                        "USD": {**POLICY["currencies"]["USD"], "approve_above": "50.00"}}}
    transport = ReplayTransport(FIX / cassette if cassette else {"exchanges": []})
    cfg = PayPalConfig(client_id="id", client_secret=SECRET,
                       card=CardSource(number=CARD_NUMBER, expiry="2030-12", security_code="123"))
    engine = Engine(Policy.from_mapping(policy), ledger, balance=lambda c: Decimal(2000), clock=clock)
    return Gateway(engine, PayPalPayer(PayPalClient(cfg, transport)), clock=clock), transport


def test_a_payment_above_the_threshold_waits_and_makes_no_call(ledger, clock):
    g, t = held_gateway(ledger, clock)  # an empty cassette: any call would raise
    out = g.submit(intent(amount="55.00"))
    assert out.receipt.allowed and out.payment.status == "AWAITING_APPROVAL" and not out.payment.ok
    assert t.calls == [] and [r.get("operation") for r in ledger.records()] == [None, "await_approval"]
    assert ledger.verify() == 2


def test_a_payment_at_the_threshold_is_paid_without_approval(ledger, clock):
    g, _ = held_gateway(ledger, clock, "pay-completed.json")
    assert g.submit(intent(amount="50.00")).payment.ok


def test_approval_pays_the_held_payment_once(ledger, clock):
    g, t = held_gateway(ledger, clock, "pay-completed.json")
    held = g.submit(intent(amount="55.00"))
    out = g.approve(held.receipt.seq, "Alex")
    assert out.payment.ok and out.payment.status == "COMPLETED"
    ops = [(r.get("operation"), r.get("detail")) for r in ledger.records() if r["kind"] == "execution"]
    assert [o for o, _ in ops] == ["await_approval", "approval", "pay"] and ops[1][1] == "approved by Alex"
    with pytest.raises(GatewayError, match="already approved"):
        g.approve(held.receipt.seq, "Alex")
    assert ledger.verify() == 4


def test_only_a_held_payment_can_be_approved(ledger, clock):
    g, _ = held_gateway(ledger, clock, "pay-completed.json")
    paid = g.submit(intent())
    refused = g.submit(intent(request_id="req-2", amount="490.00", billing="annual"))
    with pytest.raises(GatewayError, match="not waiting for approval"):
        g.approve(paid.receipt.seq, "Alex")
    with pytest.raises(GatewayError, match="not an allowed decision"):
        g.approve(refused.receipt.seq, "Alex")
    with pytest.raises(GatewayError, match="name of the person"):
        g.approve(paid.receipt.seq, " ")


class Stop(BaseException):
    """Stands for the process stopping: the gateway does not catch it."""


class OtherPayer:
    provider = "airwallex"

    def pay(self, intent, receipt):
        raise AssertionError("a payment held for another provider must not reach this payer")


def test_approval_goes_only_to_the_provider_that_held_the_payment(ledger, clock):
    g, _ = held_gateway(ledger, clock, "pay-completed.json")
    held = g.submit(intent(amount="55.00"))
    with pytest.raises(GatewayError, match="held for paypal, not for airwallex"):
        Gateway(g.engine, OtherPayer(), clock=clock).approve(held.receipt.seq, "Alex")
    assert [r.get("operation") for r in ledger.records()] == [None, "await_approval"]
    assert g.approve(held.receipt.seq, "Alex").payment.ok


def test_an_approved_payment_that_failed_is_paid_by_approving_again(ledger, clock):
    g, _ = held_gateway(ledger, clock, "pay-completed.json")
    held = g.submit(intent(amount="55.00"))
    transport = g.payer.client.transport
    def down(*args):
        raise ConnectionError("sandbox unreachable")
    g.payer.client.transport = down
    assert g.approve(held.receipt.seq, "Alex").payment.status == "ERROR"
    g.payer.client.transport = transport
    again = g.approve(held.receipt.seq, "Sam")
    assert again.payment.ok and "approved earlier as #3" in again.note
    with pytest.raises(GatewayError, match="already approved and paid"):
        g.approve(held.receipt.seq, "Alex")
    ops = [(r["operation"], r["ok"]) for r in ledger.records() if r["kind"] == "execution"]
    assert ops == [("await_approval", False), ("approval", True), ("pay", False), ("pay", True)]
    assert ledger.verify() == 5


def test_a_process_stopped_after_the_approval_is_finished_by_approving_again(ledger, clock):
    g, t = held_gateway(ledger, clock, "pay-completed.json")
    held = g.submit(intent(amount="55.00"))
    def stop(*args):
        raise Stop()
    g.payer.pay = stop
    with pytest.raises(Stop):
        g.approve(held.receipt.seq, "Alex")
    assert [r.get("operation") for r in ledger.records()] == [None, "await_approval", "approval"]
    del g.payer.pay
    assert g.approve(held.receipt.seq, "Alex").payment.ok
    assert [r.get("operation") for r in ledger.records()] == [None, "await_approval", "approval", "pay"]
    assert t.finished


def test_the_approve_command_pays_with_the_provider_that_held_it(tmp_path, capsys, clock):
    from intent_pay.__main__ import main
    policy = tmp_path / "p.toml"
    policy.write_text('merchant_categories = ["5734", "7372"]\n[currencies.USD]\n'
                      'per_payment = "60.00"\ndaily = "100.00"\napprove_above = "40.00"\n')
    ledger = Ledger(tmp_path / "l.jsonl")
    loaded = Policy.load(policy)
    payer = AirwallexPayer(AirwallexClient(AirwallexConfig("replay", "replay"), ReplayTransport({"exchanges": []})), loaded)
    held = Gateway(Engine(loaded, ledger, clock=clock), payer, clock=clock).submit(intent(merchant_category="7372"))
    assert held.payment.provider == "airwallex" and held.payment.status == "AWAITING_APPROVAL"
    # The recording's balance read belongs to the decision, which approval does not repeat.
    cassette = json.loads((AIRWALLEX_FIX / "pay-cleared.json").read_text(encoding="utf-8"))
    cassette["exchanges"] = [e for e in cassette["exchanges"] if "/balances/" not in e["request"]["path"]]
    (tmp_path / "c.json").write_text(json.dumps(cassette), encoding="utf-8")
    args = ["--env", str(tmp_path / "none"), "--ledger", str(ledger.path), "approve", "--policy", str(policy),
            "--receipt", str(held.receipt.seq), "--by", "Alex", "--replay", str(tmp_path / "c.json")]
    assert main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["payment"]["provider"] == "airwallex" and out["payment"]["ok"]


def test_the_approve_command_pays_a_held_payment(tmp_path, capsys):
    from intent_pay.__main__ import main
    policy = tmp_path / "p.toml"
    policy.write_text('merchant_categories = ["5734", "7372"]\n[currencies.USD]\n'
                      'per_payment = "60.00"\ndaily = "100.00"\napprove_above = "50.00"\n')
    proposal = tmp_path / "i.json"
    proposal.write_text(json.dumps(intent(amount="55.00", payee=None)))
    common = ["--env", str(tmp_path / "none"), "--ledger", str(tmp_path / "l.jsonl")]
    cassette = str(FIX / "pay-completed.json")
    assert main([*common, "pay", "--policy", str(policy), "--intent", str(proposal), "--replay", cassette]) == 0
    assert json.loads(capsys.readouterr().out)["payment"]["status"] == "AWAITING_APPROVAL"
    assert main([*common, "approve", "--policy", str(policy), "--receipt", "1", "--by", "Alex", "--replay", cassette]) == 0
    assert json.loads(capsys.readouterr().out)["payment"]["status"] == "COMPLETED"


def test_a_threshold_leaves_the_policy_hash_of_policies_without_one_alone():
    with_none = Policy.from_mapping(POLICY)
    assert "approve_above" not in json.dumps(with_none.to_dict())


def test_refused_proposal_makes_no_sandbox_call(ledger, clock):
    g, t = gateway(ledger, clock)  # an empty cassette: any call would raise
    out = g.submit(intent(amount="490.00", billing="annual"))
    assert out.receipt.reason == "per_payment_limit_exceeded"
    assert out.payment is None and out.execution is None and t.calls == []


def test_allowed_proposal_is_paid_and_recorded(ledger, clock):
    g, t = gateway(ledger, clock, "pay-completed.json")
    out = g.submit(intent())
    assert out.receipt.allowed and out.payment.ok and out.payment.status == "COMPLETED"
    assert out.execution.decision_seq == out.receipt.seq == 1 and out.execution.seq == 2
    records = ledger.records()
    assert [r["kind"] for r in records] == ["decision", "execution"]
    assert records[1]["reference"] == CAPTURE
    assert [s["call"] for s in records[1]["steps"]] == ["create_order", "capture_order"]
    assert ledger.verify() == 2


def test_same_request_twice_pays_once(ledger, clock):
    g, t = gateway(ledger, clock, "pay-completed.json")
    assert g.submit(intent()).payment.ok
    second = g.submit(intent())
    assert second.receipt.reason == "duplicate_request_id" and second.payment is None
    assert len(t.calls) == 3  # token, create, capture: all from the first submit


def test_declined_payment_still_counts_against_the_day(ledger, clock):
    g, _ = gateway(ledger, clock, "pay-capture-declined.json")
    out = g.submit(intent(request_id="a", amount="60.00"))
    assert out.receipt.allowed and not out.payment.ok
    assert out.execution.ok is False and out.execution.status == "DECLINED"
    g2, _ = gateway(ledger, clock)
    assert g2.submit(intent(request_id="b", amount="40.00")).receipt.allowed
    assert g2.submit(intent(request_id="c", amount="0.01")).receipt.reason == "daily_limit_exceeded"


def test_network_error_is_recorded_as_a_failed_execution(ledger, clock):
    g, _ = gateway(ledger, clock)
    def down(*args):
        raise ConnectionError("sandbox unreachable")
    g.payer.client.transport = down
    out = g.submit(intent())
    assert out.receipt.allowed and not out.payment.ok and out.payment.status == "ERROR"
    assert "sandbox unreachable" in out.execution.detail and ledger.verify() == 2


def test_refund_of_a_completed_payment(ledger, clock):
    g, _ = gateway(ledger, clock, "pay-completed.json")
    paid = g.submit(intent())
    g2, t = gateway(ledger, clock, "refund-completed.json")
    out = g2.refund(paid.receipt.seq)
    assert out.payment.ok and out.payment.operation == "refund"
    assert t.calls[1]["url"].endswith(f"/v2/payments/captures/{CAPTURE}/refund")
    assert [r["operation"] for r in ledger.records() if r["kind"] == "execution"] == ["pay", "refund"]
    with pytest.raises(GatewayError, match="already refunded"):
        g2.refund(paid.receipt.seq)


def test_refund_refused_for_a_refusal_or_an_unpaid_decision(ledger, clock):
    g, _ = gateway(ledger, clock, "pay-capture-declined.json")
    refused = g.submit(intent(request_id="x", amount="490.00"))
    declined = g.submit(intent(request_id="y"))
    with pytest.raises(GatewayError, match="not an allowed decision"):
        g.refund(refused.receipt.seq)
    with pytest.raises(GatewayError, match="no completed payment"):
        g.refund(declined.receipt.seq)
    with pytest.raises(GatewayError, match="not an allowed decision"):
        g.refund(99)


def test_execution_for_a_refused_decision_breaks_the_ledger(ledger, clock):
    from intent_pay.ledger import canonical, sha256_hex
    g, _ = gateway(ledger, clock)
    g.submit(intent(amount="490.00"))
    forged = {"v": "1", "kind": "execution", "seq": 2, "at": "2026-10-27T12:00:00.000000Z", "decision_seq": 1,
              "provider": "paypal", "operation": "pay", "ok": True, "status": "COMPLETED", "reference": "X",
              "detail": "", "steps": [], "prev": ledger.records()[0]["hash"]}
    forged["hash"] = sha256_hex(canonical(forged))
    with open(ledger.path, "a") as fh:
        fh.write(canonical(forged) + "\n")
    with pytest.raises(LedgerError, match="no allowed decision"):
        ledger.verify()


def test_agent_view_and_ledger_carry_no_secrets(ledger, clock):
    g, _ = gateway(ledger, clock, "pay-completed.json")
    out = g.submit(intent())
    for text in (json.dumps(out.for_agent()), json.dumps(out.to_dict()), ledger.path.read_text()):
        assert SECRET not in text and CARD_NUMBER not in text and "Bearer" not in text


def test_cli_end_to_end_on_cassettes(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    run = lambda *a: subprocess.run([sys.executable, "-m", "intent_pay", "--env", str(tmp_path / "none"),
                                     "--ledger", str(ledger), *a], cwd=ROOT, capture_output=True, text=True,
                                    env=CLEAN_ENV)
    paid = run("pay", "--policy", "policies/paypal-sandbox.toml", "--intent", "examples/intent-monthly.json",
               "--replay", "tests/fixtures/paypal/pay-completed.json")
    assert paid.returncode == 0, paid.stdout + paid.stderr
    assert json.loads(paid.stdout)["agent_sees"]["payment"]["status"] == "COMPLETED"
    refused = run("pay", "--policy", "policies/paypal-sandbox.toml", "--intent", "examples/intent-annual.json",
                  "--replay", "tests/fixtures/paypal/pay-completed.json")
    assert json.loads(refused.stdout)["agent_sees"]["reason"] == "per_payment_limit_exceeded"
    verified = run("verify")
    assert json.loads(verified.stdout) == {"ledger": str(ledger), "records": 3, "chain": "ok"}


def test_cli_without_keys_names_the_missing_variables(tmp_path):
    out = subprocess.run([sys.executable, "-m", "intent_pay", "--env", str(tmp_path / "none"),
                          "--ledger", str(tmp_path / "l.jsonl"), "pay", "--policy", "policies/paypal-sandbox.toml",
                          "--intent", "examples/intent-monthly.json"],
                         cwd=ROOT, capture_output=True, text=True, env=CLEAN_ENV)
    assert out.returncode == 2
    assert "PAYPAL_CLIENT_ID" in json.loads(out.stdout)["detail"]
