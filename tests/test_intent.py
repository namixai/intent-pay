from decimal import Decimal

import pytest

from conftest import intent
from intent_pay.intent import Intent, IntentError


def test_parse_normalises_amount_and_payee():
    i = Intent.parse(intent(amount="49", payee="Billing@Acme.Example"))
    assert i.amount == Decimal("49.00") and str(i.amount) == "49.00"
    assert i.payee == "billing@acme.example"
    assert i.to_dict()["amount"] == "49.00"


def test_float_from_a_model_tool_call_is_read_exactly():
    assert Intent.parse(intent(amount=29.99)).amount == Decimal("29.99")
    assert Intent.parse(intent(amount=49)).amount == Decimal("49.00")


def test_zero_decimal_currency():
    assert str(Intent.parse(intent(amount="9000", currency="JPY")).amount) == "9000"
    with pytest.raises(IntentError, match="0 decimal places for JPY"):
        Intent.parse(intent(amount="9000.5", currency="JPY"))


def test_billing_defaults_to_one_time():
    assert Intent.parse(intent(billing=None)).billing == "one_time"


def test_payee_or_category_alone_is_enough():
    assert Intent.parse(intent(merchant_category=None)).merchant_category is None
    assert Intent.parse(intent(payee=None)).payee is None


@pytest.mark.parametrize("over, message", [
    ({"amount": "0"}, "above zero"),
    ({"amount": "-5"}, "plain decimal"),
    ({"amount": "1e3"}, "plain decimal"),
    ({"amount": 1e-05}, "plain decimal"),
    ({"amount": 1e16}, "plain decimal"),
    ({"amount": float("inf")}, "finite"),
    ({"amount": float("nan")}, "finite"),
    ({"amount": True}, "not True"),
    ({"amount": ["49"]}, "number or a decimal string"),
    ({"amount": "49.001"}, "decimal places"),
    ({"amount": None}, "number or a decimal string"),
    ({"currency": "usd"}, "ISO 4217"),
    ({"currency": "US"}, "ISO 4217"),
    ({"currency": None}, "ISO 4217"),
    ({"request_id": None}, "request_id is required"),
    ({"request_id": "has space"}, "request_id may use"),
    ({"request_id": "x" * 65}, "longer than 64"),
    ({"purpose": None}, "purpose is required"),
    ({"purpose": "   "}, "purpose is required"),
    ({"purpose": "x" * 201}, "longer than 200"),
    ({"purpose": "pay\nnow"}, "control characters"),
    ({"payee": 42}, "payee must be a string"),
    ({"merchant_category": 5734}, "four-digit"),
    ({"merchant_category": "57340"}, "four-digit"),
    ({"payee": None, "merchant_category": None}, "a payee, a merchant_category, or both"),
    ({"billing": "weekly"}, "billing must be one of"),
])
def test_malformed_intents_are_refused(over, message):
    # intent() drops keys set to None, so {"purpose": None} means "no purpose field at all".
    with pytest.raises(IntentError, match=message):
        Intent.parse(intent(**over))


def test_unknown_field_is_refused():
    # A field the policy does not read is a place to hide an instruction. Refuse it.
    with pytest.raises(IntentError, match="unknown field"):
        Intent.parse({**intent(), "approve_override": True})


def test_non_object_is_refused():
    with pytest.raises(IntentError, match="JSON object"):
        Intent.parse(["req-1", "49.00"])
