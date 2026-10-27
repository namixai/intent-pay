import pathlib
from decimal import Decimal

import pytest

from conftest import POLICY
from intent_pay.policy import Policy, PolicyError

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_example_policy_loads():
    policy = Policy.load(ROOT / "policies" / "example.toml")
    usd = policy.currencies["USD"]
    assert usd.per_payment == Decimal("60.00") and usd.daily == Decimal("100.00")
    assert usd.min_balance == Decimal("1000.00")


def test_hash_does_not_depend_on_list_order():
    shuffled = {**POLICY, "merchant_categories": list(reversed(POLICY["merchant_categories"]))}
    assert Policy.from_mapping(POLICY).hash == Policy.from_mapping(shuffled).hash


def test_hash_changes_with_any_limit():
    tighter = {**POLICY, "currencies": {**POLICY["currencies"], "USD": {**POLICY["currencies"]["USD"], "daily": "99.99"}}}
    assert Policy.from_mapping(POLICY).hash != Policy.from_mapping(tighter).hash


def test_zero_reserve_is_allowed():
    p = Policy.from_mapping({"payees": ["a@b.example"], "currencies": {"USD": {"per_payment": "1", "daily": "1", "min_balance": "0"}}})
    assert p.currencies["USD"].min_balance == Decimal(0)


@pytest.mark.parametrize("data, message", [
    ({"currencies": {"USD": {"per_payment": "1", "daily": "1"}}}, "payees, merchant_categories, or both"),
    ({"payees": ["a@b.example"]}, "at least one"),
    ({"payees": ["a@b.example"], "currencies": {}}, "at least one"),
    ({"payees": ["a@b.example"], "currencies": {"usd": {"per_payment": "1", "daily": "1"}}}, "ISO 4217"),
    ({"payees": ["a@b.example"], "currencies": {"USD": {"per_payment": 1.5, "daily": "2"}}}, "as a string"),
    ({"payees": ["a@b.example"], "currencies": {"USD": {"per_payment": "0", "daily": "2"}}}, "above zero"),
    ({"payees": ["a@b.example"], "currencies": {"USD": {"per_payment": "5", "daily": "2"}}}, "below per_payment"),
    ({"payees": ["a@b.example"], "currencies": {"USD": {"per_payment": "1", "daily": "1", "cap": "1"}}}, "unknown key"),
    ({"payees": ["a@b.example"], "currencies": {"USD": {"per_payment": "1", "daily": "1", "min_balance": "-1"}}}, "plain decimal"),
    ({"payees": [], "currencies": {"USD": {"per_payment": "1", "daily": "1"}}}, "non-empty list"),
    ({"merchant_categories": [5734], "currencies": {"USD": {"per_payment": "1", "daily": "1"}}}, "non-empty list of strings"),
    ({"merchant_categories": ["573"], "currencies": {"USD": {"per_payment": "1", "daily": "1"}}}, "four-digit"),
    ({"payees": ["a@b.example"], "allow_all": True, "currencies": {"USD": {"per_payment": "1", "daily": "1"}}}, "unknown policy key"),
])
def test_invalid_policies_do_not_load(data, message):
    with pytest.raises(PolicyError, match=message):
        Policy.from_mapping(data)


def test_unreadable_policy_file(tmp_path):
    bad = tmp_path / "p.toml"
    bad.write_text("currencies = [")
    with pytest.raises(PolicyError, match="cannot read policy"):
        Policy.load(bad)
