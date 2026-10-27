import json
from datetime import timezone

import pytest

from conftest import NOON, intent
from intent_pay.ledger import GENESIS, Ledger, LedgerError


def write_two(engine):
    assert engine.decide(intent(request_id="a")).allowed
    assert not engine.decide(intent(request_id="b", amount="70.00")).allowed


def test_receipts_chain_from_genesis(engine, ledger):
    write_two(engine)
    first, second = ledger.records()
    assert first["prev"] == GENESIS and first["seq"] == 1
    assert second["prev"] == first["hash"] and second["seq"] == 2
    assert first["at"] == NOON.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert ledger.verify() == 2


def test_refusals_are_receipts_too(engine, ledger):
    write_two(engine)
    second = ledger.records()[1]
    assert second["decision"] == "refuse"
    assert second["reason"] == "per_payment_limit_exceeded"
    assert second["intent"]["amount"] == "70.00"


@pytest.mark.parametrize("tamper", [
    lambda lines: [lines[0].replace('"amount":"49.00"', '"amount":"4.00"'), lines[1]],  # edit
    lambda lines: [lines[1]],                                                            # delete first
    lambda lines: [lines[1], lines[0]],                                                  # reorder
    lambda lines: lines + [lines[1]],                                                    # replay a line
])
def test_tampering_is_found(engine, ledger, tamper):
    write_two(engine)
    lines = ledger.path.read_text().splitlines()
    ledger.path.write_text("\n".join(tamper(lines)) + "\n")
    with pytest.raises(LedgerError):
        ledger.verify()


def test_rehashed_edit_still_breaks_the_next_line(engine, ledger):
    """Someone who edits a line and recomputes its hash still breaks the line after it."""
    from intent_pay.ledger import canonical, sha256_hex
    write_two(engine)
    first, second = (json.loads(l) for l in ledger.path.read_text().splitlines())
    first["intent"]["amount"] = "4.00"
    body = {k: v for k, v in first.items() if k != "hash"}
    first["hash"] = sha256_hex(canonical(body))
    ledger.path.write_text(canonical(first) + "\n" + canonical(second) + "\n")
    with pytest.raises(LedgerError, match="line 2 does not follow line 1"):
        ledger.verify()


def test_incomplete_last_line_stops_the_ledger(engine, ledger):
    write_two(engine)
    ledger.path.write_text(ledger.path.read_text() + '{"v":"1"')
    with pytest.raises(LedgerError, match="incomplete"):
        ledger.verify()


def test_missing_file_is_an_empty_ledger(tmp_path):
    assert Ledger(tmp_path / "none.jsonl").verify() == 0


def test_in_memory_ledger(clock):
    from conftest import POLICY
    from decimal import Decimal
    from intent_pay.policy import Engine, Policy
    ledger = Ledger()
    engine = Engine(Policy.from_mapping(POLICY), ledger, balance=lambda c: Decimal(2000), clock=clock)
    write_two(engine)
    assert ledger.verify() == 2 and ledger.path is None
