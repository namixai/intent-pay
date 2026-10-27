"""What the agent is allowed to say: one payment it would like made, as plain data.

The agent never builds a request to PayPal or Airwallex and never sees a key. It fills in an
intent and hands it over; the policy engine (`intent_pay.policy`) decides. Parsing is strict on
purpose. An unknown field, an amount with more decimals than the currency has, a missing
purpose or request ID: each is refused before any rule runs, so a proposal cannot carry
something past the policy in a field the policy does not read.

    {"request_id": "sub-2026-10-acme-1", "amount": "49.00", "currency": "USD",
     "payee": "billing@acme.example", "purpose": "Acme Analytics, October",
     "billing": "monthly"}
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

BILLING = ("one_time", "monthly", "annual")
FIELDS = frozenset({"request_id", "amount", "currency", "payee", "merchant_category", "purpose", "billing"})

# ISO 4217 minor units. A currency not listed here has two decimal places; add one here before a
# policy allows a currency with another exponent.
ZERO_DECIMAL = frozenset({"JPY"})

REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
CURRENCY = re.compile(r"[A-Z]{3}")
MCC = re.compile(r"[0-9]{4}")
# Plain decimal notation only: no sign, no exponent, at most 15 integer digits.
AMOUNT = re.compile(r"[0-9]{1,15}(\.[0-9]+)?")
MAX_PURPOSE = 200
MAX_PAYEE = 254


class IntentError(ValueError):
    """The proposal cannot be read as an intent. The engine refuses it as `malformed_intent`."""


def minor_units(currency: str) -> int:
    return 0 if currency in ZERO_DECIMAL else 2


def parse_money(value: Any, currency: str, field: str = "amount", allow_zero: bool = False) -> Decimal:
    """A positive amount (or zero, with `allow_zero`) with no more decimals than `currency`
    allows, as a Decimal.

    Strings and integers are taken as written. A float is accepted only because a model's JSON
    tool call carries numbers as floats: its shortest repr (29.99 -> "29.99") must still be plain
    decimal notation, so 1e-05 or 1e+16 are refused rather than rounded into something else.
    """
    if isinstance(value, bool):
        raise IntentError(f"{field} must be a number or a decimal string, not {value!r}")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise IntentError(f"{field} must be finite")
        text = repr(value)
    elif isinstance(value, (int, str)):
        text = str(value).strip()
    else:
        raise IntentError(f"{field} must be a number or a decimal string")
    if not AMOUNT.fullmatch(text):
        raise IntentError(f"{field} {text!r} is not a plain decimal amount")
    amount = Decimal(text)
    places = minor_units(currency)
    if amount != amount.quantize(Decimal(1).scaleb(-places)):
        raise IntentError(f"{field} {text} has more than {places} decimal places for {currency}")
    if amount < 0 or (amount == 0 and not allow_zero):
        raise IntentError(f"{field} must be above zero")
    return amount.quantize(Decimal(1).scaleb(-places))


def _text(raw: Mapping[str, Any], field: str, limit: int, required: bool) -> str | None:
    value = raw.get(field)
    if value is None:
        if required:
            raise IntentError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise IntentError(f"{field} must be a string")
    value = value.strip()
    if not value:
        if required:
            raise IntentError(f"{field} is required")
        return None
    if len(value) > limit:
        raise IntentError(f"{field} is longer than {limit} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise IntentError(f"{field} contains control characters")
    return value


@dataclass(frozen=True)
class Intent:
    request_id: str
    amount: Decimal
    currency: str
    purpose: str
    payee: str | None = None
    merchant_category: str | None = None
    billing: str = "one_time"

    @classmethod
    def parse(cls, raw: Any) -> Intent:
        if not isinstance(raw, Mapping):
            raise IntentError("an intent must be a JSON object")
        unknown = sorted(str(k) for k in raw if k not in FIELDS)
        if unknown:
            raise IntentError(f"unknown field(s): {', '.join(unknown)}")

        request_id = _text(raw, "request_id", 64, required=True)
        if not REQUEST_ID.fullmatch(request_id):
            raise IntentError("request_id may use letters, digits and . _ : - only, up to 64 characters")

        currency = raw.get("currency")
        if not isinstance(currency, str) or not CURRENCY.fullmatch(currency):
            raise IntentError("currency must be a three-letter upper-case ISO 4217 code")

        amount = parse_money(raw.get("amount"), currency)
        purpose = _text(raw, "purpose", MAX_PURPOSE, required=True)

        # Payees compare case-insensitively: PayPal treats e-mail addresses that way.
        payee = _text(raw, "payee", MAX_PAYEE, required=False)
        payee = payee.lower() if payee else None

        mcc = raw.get("merchant_category")
        if mcc is not None and (not isinstance(mcc, str) or not MCC.fullmatch(mcc)):
            raise IntentError("merchant_category must be a four-digit merchant category code, as a string")
        if payee is None and mcc is None:
            raise IntentError("an intent must name a payee, a merchant_category, or both")

        billing = raw.get("billing", "one_time")
        if billing not in BILLING:
            raise IntentError(f"billing must be one of {', '.join(BILLING)}")

        return cls(request_id=request_id, amount=amount, currency=currency, purpose=purpose,
                   payee=payee, merchant_category=mcc, billing=billing)

    def to_dict(self) -> dict[str, str | None]:
        """Money as a string, so a receipt hash never depends on float formatting."""
        return {
            "request_id": self.request_id,
            "amount": str(self.amount),
            "currency": self.currency,
            "payee": self.payee,
            "merchant_category": self.merchant_category,
            "purpose": self.purpose,
            "billing": self.billing,
        }
