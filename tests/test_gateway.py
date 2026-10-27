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
from intent_pay.adapters.http import ReplayTransport
from intent_pay.adapters.paypal import CardSource, PayPalClient, PayPalConfig, PayPalPayer
from intent_pay.gateway import Gateway, GatewayError
from intent_pay.ledger import Ledger, LedgerError
from intent_pay.policy import Engine, Policy

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "paypal"
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
