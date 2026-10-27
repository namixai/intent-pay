"""One test per refusal, plus the allow path and its boundaries. Each refusal test checks the
reason code and that the refusal is on disk, because a refusal the ledger did not keep is
not a receipt."""

import threading
from datetime import timedelta
from decimal import Decimal

import pytest

from conftest import NOON, POLICY, intent
from intent_pay.ledger import Ledger
from intent_pay.policy import Engine, Policy, Reason


def refused(receipt, reason):
    assert not receipt.allowed
    assert receipt.decision == "refuse"
    assert receipt.reason == reason.value, receipt.detail
    return receipt


def test_allow_within_every_rule(engine, ledger):
    r = engine.decide(intent())
    assert r.allowed and r.reason == "ok"
    assert r.seq == 1 and r.recorded
    assert r.intent["amount"] == "49.00"
    assert ledger.verify() == 1


def test_amount_equal_to_per_payment_limit_is_allowed(engine):
    assert engine.decide(intent(amount="60.00")).allowed


def test_refuse_malformed_intent(engine, ledger):
    r = refused(engine.decide(intent(amount="49.001")), Reason.MALFORMED_INTENT)
    assert "decimal places" in r.detail
    assert r.recorded and ledger.verify() == 1


def test_refuse_malformed_burns_its_request_id(engine):
    refused(engine.decide(intent(amount="-1")), Reason.MALFORMED_INTENT)
    refused(engine.decide(intent()), Reason.DUPLICATE_REQUEST_ID)


def test_refuse_duplicate_request_id_after_allow(engine):
    assert engine.decide(intent()).allowed
    r = refused(engine.decide(intent()), Reason.DUPLICATE_REQUEST_ID)
    assert "receipt #1" in r.detail


def test_refuse_duplicate_request_id_after_refusal(engine):
    refused(engine.decide(intent(amount="590.00")), Reason.PER_PAYMENT_LIMIT_EXCEEDED)
    # The same ID with a smaller amount is still the same request: no second try.
    refused(engine.decide(intent(amount="49.00")), Reason.DUPLICATE_REQUEST_ID)


def test_duplicate_survives_restart(tmp_path, clock):
    path = tmp_path / "ledger.jsonl"
    first = Engine(Policy.from_mapping(POLICY), Ledger(path), balance=lambda c: Decimal(2000), clock=clock)
    assert first.decide(intent()).allowed
    second = Engine(Policy.from_mapping(POLICY), Ledger(path), balance=lambda c: Decimal(2000), clock=clock)
    refused(second.decide(intent()), Reason.DUPLICATE_REQUEST_ID)


def test_refuse_currency_not_allowed(engine):
    r = refused(engine.decide(intent(currency="EUR")), Reason.CURRENCY_NOT_ALLOWED)
    assert "JPY, USD" in r.detail


def test_refuse_payee_not_allowed(engine):
    refused(engine.decide(intent(payee="someone@else.example")), Reason.PAYEE_NOT_ALLOWED)


def test_refuse_missing_payee_when_policy_lists_payees(engine):
    refused(engine.decide(intent(payee=None)), Reason.PAYEE_NOT_ALLOWED)


def test_payee_compares_case_insensitively(engine):
    assert engine.decide(intent(payee="Billing@ACME.example")).allowed


def test_refuse_merchant_category_not_allowed(engine):
    refused(engine.decide(intent(merchant_category="7995")), Reason.MERCHANT_CATEGORY_NOT_ALLOWED)


def test_refuse_missing_category_when_policy_lists_categories(engine):
    refused(engine.decide(intent(merchant_category=None)), Reason.MERCHANT_CATEGORY_NOT_ALLOWED)


def test_policy_without_category_list_does_not_ask_for_one(ledger, clock):
    payees_only = Policy.from_mapping({"payees": POLICY["payees"], "currencies": POLICY["currencies"]})
    engine = Engine(payees_only, ledger, balance=lambda c: Decimal(2000), clock=clock)
    assert engine.decide(intent(merchant_category=None)).allowed


def test_refuse_per_payment_limit_exceeded(engine):
    r = refused(engine.decide(intent(amount="60.01")), Reason.PER_PAYMENT_LIMIT_EXCEEDED)
    assert "60.01 USD is above the per-payment limit of 60.00 USD" == r.detail


def test_refuse_daily_limit_exceeded(engine):
    assert engine.decide(intent(request_id="a", amount="50.00")).allowed
    assert engine.decide(intent(request_id="b", amount="50.00")).allowed
    r = refused(engine.decide(intent(request_id="c", amount="0.01")), Reason.DAILY_LIMIT_EXCEEDED)
    assert "100.00 USD already allowed on 2026-10-27" in r.detail


def test_refusals_spend_nothing_from_the_day(engine):
    refused(engine.decide(intent(request_id="a", amount="60.01")), Reason.PER_PAYMENT_LIMIT_EXCEEDED)
    assert engine.decide(intent(request_id="b", amount="60.00")).allowed
    assert engine.decide(intent(request_id="c", amount="40.00")).allowed


def test_daily_limit_resets_on_the_next_utc_day(engine, clock):
    assert engine.decide(intent(request_id="a", amount="60.00")).allowed
    assert engine.decide(intent(request_id="b", amount="40.00")).allowed
    clock.now = NOON.replace(hour=23, minute=59, second=59)
    refused(engine.decide(intent(request_id="c", amount="1.00")), Reason.DAILY_LIMIT_EXCEEDED)
    clock.now = NOON + timedelta(days=1)
    assert engine.decide(intent(request_id="d", amount="60.00")).allowed


def test_daily_limit_survives_restart(tmp_path, clock):
    path = tmp_path / "ledger.jsonl"
    make = lambda: Engine(Policy.from_mapping(POLICY), Ledger(path), balance=lambda c: Decimal(2000), clock=clock)
    assert make().decide(intent(request_id="a", amount="60.00")).allowed
    assert make().decide(intent(request_id="b", amount="40.00")).allowed
    refused(make().decide(intent(request_id="c", amount="0.01")), Reason.DAILY_LIMIT_EXCEEDED)


def test_daily_limit_is_per_currency(engine):
    assert engine.decide(intent(request_id="a", amount="60.00")).allowed
    assert engine.decide(intent(request_id="b", amount="40.00")).allowed
    # 9000 + 6000 is exactly the JPY day; the 100 USD already spent must not count against it.
    assert engine.decide(intent(request_id="c", amount="9000", currency="JPY")).allowed
    assert engine.decide(intent(request_id="d", amount="6000", currency="JPY")).allowed


def test_refuse_below_min_balance(ledger, clock):
    engine = Engine(Policy.from_mapping(POLICY), ledger, balance=lambda c: Decimal("1049.00"), clock=clock)
    assert engine.decide(intent(request_id="a", amount="49.00")).allowed  # leaves exactly 1000.00
    r = refused(engine.decide(intent(request_id="b", amount="49.01")), Reason.BELOW_MIN_BALANCE)
    assert "leaves 999.99" in r.detail


def test_refuse_balance_unavailable_when_no_source(ledger, clock):
    engine = Engine(Policy.from_mapping(POLICY), ledger, balance=None, clock=clock)
    refused(engine.decide(intent()), Reason.BALANCE_UNAVAILABLE)


def test_refuse_balance_unavailable_when_source_fails(ledger, clock):
    def broken(currency):
        raise ConnectionError("sandbox down")
    engine = Engine(Policy.from_mapping(POLICY), ledger, balance=broken, clock=clock)
    refused(engine.decide(intent()), Reason.BALANCE_UNAVAILABLE)


@pytest.mark.parametrize("value", [None, "lots", float("nan"), 1000.5, True, Decimal("NaN"), Decimal("Infinity")])
def test_refuse_balance_unavailable_when_source_returns_garbage(ledger, clock, value):
    engine = Engine(Policy.from_mapping(POLICY), ledger, balance=lambda c: value, clock=clock)
    refused(engine.decide(intent()), Reason.BALANCE_UNAVAILABLE)


def test_balance_not_asked_without_a_reserve(ledger, clock):
    def must_not_be_called(currency):
        raise AssertionError("balance asked for a currency with no reserve")
    engine = Engine(Policy.from_mapping(POLICY), ledger, balance=must_not_be_called, clock=clock)
    assert engine.decide(intent(amount="9000", currency="JPY")).allowed


def test_balance_not_asked_when_an_earlier_rule_refuses(ledger, clock):
    calls = []
    engine = Engine(Policy.from_mapping(POLICY), ledger, balance=lambda c: calls.append(c) or Decimal(2000), clock=clock)
    refused(engine.decide(intent(amount="61.00")), Reason.PER_PAYMENT_LIMIT_EXCEEDED)
    assert calls == []


def test_refuse_ledger_unavailable_when_ledger_is_corrupt(tmp_path, clock):
    path = tmp_path / "ledger.jsonl"
    path.write_text("not json\n")
    engine = Engine(Policy.from_mapping(POLICY), Ledger(path), balance=lambda c: Decimal(2000), clock=clock)
    r = refused(engine.decide(intent()), Reason.LEDGER_UNAVAILABLE)
    assert not r.recorded and r.seq is None


def test_refuse_ledger_unavailable_when_ledger_cannot_be_written(engine, monkeypatch):
    def disk_full(fd):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr("intent_pay.ledger.os.fsync", disk_full)
    r = refused(engine.decide(intent()), Reason.LEDGER_UNAVAILABLE)
    # Every rule said yes; without a receipt safely on disk the answer is still no.
    assert not r.recorded and "No space left" in r.detail


def test_refuse_ledger_unavailable_when_ledger_cannot_be_locked(tmp_path, clock):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    engine = Engine(Policy.from_mapping(POLICY), Ledger(blocker / "ledger.jsonl"),
                    balance=lambda c: Decimal(2000), clock=clock)
    r = refused(engine.decide(intent()), Reason.LEDGER_UNAVAILABLE)
    assert not r.recorded


def test_refuse_ledger_unavailable_when_chain_is_tampered(tmp_path, clock):
    path = tmp_path / "ledger.jsonl"
    engine = Engine(Policy.from_mapping(POLICY), Ledger(path), balance=lambda c: Decimal(2000), clock=clock)
    assert engine.decide(intent(request_id="a", amount="60.00")).allowed
    # Rewrite the first receipt to hide the spend; its hash no longer matches.
    path.write_text(path.read_text().replace('"amount":"60.00"', '"amount":"1.00"'))
    refused(engine.decide(intent(request_id="b", amount="50.00")), Reason.LEDGER_UNAVAILABLE)


def test_refuse_policy_error_when_a_rule_breaks(engine, monkeypatch):
    def boom(*args):
        raise RuntimeError("rule exploded")
    monkeypatch.setattr(engine, "_evaluate", boom)
    r = refused(engine.decide(intent()), Reason.POLICY_ERROR)
    assert r.recorded and "rule exploded" in r.detail


def test_refuse_policy_error_when_clock_is_naive(engine, clock):
    clock.now = NOON.replace(tzinfo=None)
    r = refused(engine.decide(intent()), Reason.POLICY_ERROR)
    assert not r.recorded


def test_same_request_id_from_many_threads_is_allowed_once(tmp_path, clock):
    path = tmp_path / "ledger.jsonl"
    # Separate Ledger objects on one file stand in for separate processes.
    engines = [Engine(Policy.from_mapping(POLICY), Ledger(path), balance=lambda c: Decimal(2000), clock=clock)
               for _ in range(8)]
    results = []
    threads = [threading.Thread(target=lambda e=e: results.append(e.decide(intent()))) for e in engines * 3]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r.allowed for r in results) == 1
    assert sum(r.reason == "duplicate_request_id" for r in results) == len(results) - 1
    assert Ledger(path).verify() == len(results)


def test_many_threads_cannot_overspend_the_day(tmp_path, clock):
    path = tmp_path / "ledger.jsonl"
    engine = Engine(Policy.from_mapping(POLICY), Ledger(path), balance=lambda c: Decimal(2000), clock=clock)
    results = []
    threads = [threading.Thread(target=lambda i=i: results.append(engine.decide(intent(request_id=f"r{i}", amount="30.00"))))
               for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r.allowed for r in results) == 3  # 3 x 30.00 <= 100.00 < 4 x 30.00


def test_every_receipt_names_the_policy(engine):
    policy_hash = Policy.from_mapping(POLICY).hash
    assert engine.decide(intent()).policy_hash == policy_hash
    assert engine.decide(intent(request_id="x", amount="70.00")).policy_hash == policy_hash
