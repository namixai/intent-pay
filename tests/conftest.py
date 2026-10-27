from datetime import datetime, timezone
from decimal import Decimal

import pytest

from intent_pay.ledger import Ledger
from intent_pay.policy import Engine, Policy

NOON = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)

POLICY = {
    "payees": ["billing@acme.example"],
    "merchant_categories": ["5734", "7372"],
    "currencies": {
        "USD": {"per_payment": "60.00", "daily": "100.00", "min_balance": "1000.00"},
        "JPY": {"per_payment": "9000", "daily": "15000"},
    },
}


class Clock:
    def __init__(self, now=NOON):
        self.now = now

    def __call__(self):
        return self.now


def intent(**over):
    base = {
        "request_id": "req-1",
        "amount": "49.00",
        "currency": "USD",
        "payee": "billing@acme.example",
        "merchant_category": "5734",
        "purpose": "Acme Analytics, October",
        "billing": "monthly",
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger.jsonl")


@pytest.fixture
def engine(ledger, clock):
    return Engine(Policy.from_mapping(POLICY), ledger, balance=lambda cur: Decimal("2000.00"), clock=clock)
