"""The owner's rules, and the one place they are applied.

A policy is a TOML file the account owner writes; the model never sees it change and cannot
change it. `Engine.decide` takes the agent's proposal, checks it against every rule in a fixed
order, writes a receipt, and only then answers. The first rule that fails is the reason.

    payees = ["billing@acme.example"]
    merchant_categories = ["5734", "7372"]

    [currencies.USD]
    per_payment = "60.00"
    daily = "100.00"
    min_balance = "1000.00"

Rules, in the order they run:

1. the proposal parses as an intent                      malformed_intent
2. its request ID was never decided before, either way   duplicate_request_id
3. its currency has a rule                               currency_not_allowed
4. its payee is listed (when the policy lists payees)    payee_not_allowed
5. its merchant category is listed (when listed)         merchant_category_not_allowed
6. amount <= per_payment                                 per_payment_limit_exceeded
7. allowed today + amount <= daily (UTC day)             daily_limit_exceeded
8. balance - amount >= min_balance (when set)            balance_unavailable / below_min_balance

Anything else that goes wrong refuses as well: a ledger that cannot be read or written is
`ledger_unavailable`, an unexpected error is `policy_error`. The design follows the limits of
Usenami Signer (github.com/namixai/signer): a request-ID replay ledger, a per-day counter that
is fail-closed and survives restarts, and a receipt for every decision, allow and refuse alike.
Limits are per currency, so no rule ever depends on an exchange rate.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Callable, Mapping

from intent_pay.intent import CURRENCY, MCC, Intent, IntentError, parse_money
from intent_pay.ledger import ALLOW, REFUSE, Ledger, LedgerError, LedgerState, Receipt, canonical, iso_utc, sha256_hex


class Reason(StrEnum):
    OK = "ok"
    MALFORMED_INTENT = "malformed_intent"
    DUPLICATE_REQUEST_ID = "duplicate_request_id"
    CURRENCY_NOT_ALLOWED = "currency_not_allowed"
    PAYEE_NOT_ALLOWED = "payee_not_allowed"
    MERCHANT_CATEGORY_NOT_ALLOWED = "merchant_category_not_allowed"
    PER_PAYMENT_LIMIT_EXCEEDED = "per_payment_limit_exceeded"
    DAILY_LIMIT_EXCEEDED = "daily_limit_exceeded"
    AMOUNT_MISMATCH = "amount_mismatch"
    BALANCE_UNAVAILABLE = "balance_unavailable"
    BELOW_MIN_BALANCE = "below_min_balance"
    LEDGER_UNAVAILABLE = "ledger_unavailable"
    POLICY_ERROR = "policy_error"


@dataclass(frozen=True)
class Order:
    """What the owner asked for, set by code and not by the agent: a unit price, a quantity and a
    currency. The engine works out the amount itself and refuses a proposal that asks for another."""
    unit_price: Decimal
    quantity: int
    currency: str

    @property
    def amount(self) -> Decimal:
        return self.unit_price * self.quantity

    @classmethod
    def parse(cls, value: Any) -> Order:
        if isinstance(value, Order):
            # A ready Order meets the same rules as one read from a mapping: Order(-49.00, -1, "USD")
            # works out to 49.00 as well, and must still be refused.
            price = value.unit_price
            value = {"unit_price": format(price, "f") if isinstance(price, Decimal) else price,
                     "quantity": value.quantity, "currency": value.currency}
        if not isinstance(value, Mapping) or set(value) != {"unit_price", "quantity", "currency"}:
            raise ValueError("an order is exactly unit_price, quantity and currency")
        currency, quantity = value["currency"], value["quantity"]
        if not isinstance(currency, str) or not CURRENCY.fullmatch(currency):
            raise ValueError("order currency must be a three-letter code")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError("order quantity must be a whole number of at least 1")
        return cls(parse_money(value["unit_price"], currency, "unit_price"), quantity, currency)


class PolicyError(ValueError):
    """The policy file is not a valid policy. Nothing runs on a policy that does not load."""


@dataclass(frozen=True)
class CurrencyRule:
    per_payment: Decimal
    daily: Decimal
    min_balance: Decimal | None = None
    # An allowed payment above this waits for a person (Gateway.approve) instead of being paid.
    approve_above: Decimal | None = None


@dataclass(frozen=True)
class Policy:
    currencies: Mapping[str, CurrencyRule]
    payees: frozenset[str] | None = None
    merchant_categories: frozenset[str] | None = None

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Policy:
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise PolicyError(f"cannot read policy {path}: {exc}") from None
        return cls.from_mapping(data)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Policy:
        unknown = sorted(set(data) - {"currencies", "payees", "merchant_categories"})
        if unknown:
            raise PolicyError(f"unknown policy key(s): {', '.join(unknown)}")

        raw_currencies = data.get("currencies")
        if not isinstance(raw_currencies, Mapping) or not raw_currencies:
            raise PolicyError("a policy needs at least one [currencies.XXX] table")
        currencies = {code: _currency_rule(code, rule) for code, rule in raw_currencies.items()}

        payees = _string_set(data, "payees")
        if payees is not None:
            payees = frozenset(p.lower() for p in payees)
        categories = _string_set(data, "merchant_categories")
        if categories is not None and any(not MCC.fullmatch(c) for c in categories):
            raise PolicyError("merchant_categories are four-digit codes written as strings, e.g. \"5734\"")
        # A policy that names no one would pay anyone. Refuse to load it instead.
        if payees is None and categories is None:
            raise PolicyError("a policy must list payees, merchant_categories, or both")
        return cls(currencies=currencies, payees=payees, merchant_categories=categories)

    def to_dict(self) -> dict[str, Any]:
        return {
            "currencies": {
                code: {"per_payment": str(r.per_payment), "daily": str(r.daily),
                       "min_balance": None if r.min_balance is None else str(r.min_balance),
                       # Only when set, so a policy without it keeps the hash it always had.
                       **({} if r.approve_above is None else {"approve_above": str(r.approve_above)})}
                for code, r in sorted(self.currencies.items())
            },
            "payees": None if self.payees is None else sorted(self.payees),
            "merchant_categories": None if self.merchant_categories is None else sorted(self.merchant_categories),
        }

    @property
    def hash(self) -> str:
        """Every receipt names the policy it was decided under by this hash."""
        return sha256_hex(canonical(self.to_dict()))


def _money(code: str, rule: Mapping[str, Any], key: str, allow_zero: bool = False) -> Decimal:
    value = rule.get(key)
    if isinstance(value, float):
        raise PolicyError(f"currencies.{code}.{key}: write money as a string, e.g. \"49.99\"")
    try:
        return parse_money(value, code, field=f"currencies.{code}.{key}", allow_zero=allow_zero)
    except IntentError as exc:
        raise PolicyError(str(exc)) from None


def _currency_rule(code: Any, rule: Any) -> CurrencyRule:
    if not isinstance(code, str) or not CURRENCY.fullmatch(code):
        raise PolicyError(f"currency {code!r} must be a three-letter upper-case ISO 4217 code")
    if not isinstance(rule, Mapping):
        raise PolicyError(f"currencies.{code} must be a table")
    unknown = sorted(set(rule) - {"per_payment", "daily", "min_balance", "approve_above"})
    if unknown:
        raise PolicyError(f"unknown key(s) in currencies.{code}: {', '.join(unknown)}")
    per_payment = _money(code, rule, "per_payment")
    daily = _money(code, rule, "daily")
    if daily < per_payment:
        raise PolicyError(f"currencies.{code}: daily ({daily}) is below per_payment ({per_payment})")
    min_balance = None
    if rule.get("min_balance") is not None:
        # Zero is a meaningful reserve ("never overdraw"), so it is allowed here.
        min_balance = _money(code, rule, "min_balance", allow_zero=True)
    approve_above = None
    if rule.get("approve_above") is not None:
        approve_above = _money(code, rule, "approve_above", allow_zero=True)
    return CurrencyRule(per_payment=per_payment, daily=daily, min_balance=min_balance, approve_above=approve_above)


def _string_set(data: Mapping[str, Any], key: str) -> frozenset[str] | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        raise PolicyError(f"{key} must be a non-empty list of strings")
    return frozenset(v.strip() for v in value)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


BalanceSource = Callable[[str], Any]


class Engine:
    """Applies one policy, writing every decision to one ledger.

    `balance(currency)` returns the account's available balance in that currency. It is asked
    only when a rule sets `min_balance`, and only after every other rule has passed. If it is
    missing, raises, or returns something that is not an amount, the payment is refused.
    """

    def __init__(self, policy: Policy, ledger: Ledger, balance: BalanceSource | None = None,
                 clock: Callable[[], datetime] = utc_now):
        self.policy = policy
        self.ledger = ledger
        self.balance = balance
        self.clock = clock

    def decide(self, proposal: Any, order: Any = None) -> Receipt:
        """Never raises: whatever goes wrong comes back as a refusal. With an `order`, the amount
        must be the one the engine works out from it (amount_mismatch otherwise)."""
        try:
            return self._decide(proposal, order)
        except Exception as exc:  # fail-closed: nothing that breaks may read as an allow
            return self._unrecorded(utc_now(), Reason.POLICY_ERROR, f"{type(exc).__name__}: {exc}", None, None)

    def _decide(self, proposal: Any, order: Any) -> Receipt:
        try:
            now = self.clock()
            if not isinstance(now, datetime) or now.tzinfo is None:
                raise ValueError("the clock must return a timezone-aware datetime")
        except Exception as exc:
            return self._unrecorded(utc_now(), Reason.POLICY_ERROR, f"clock: {exc}", None, None)
        try:
            intent = Intent.parse(proposal)
        except IntentError as exc:
            request_id = proposal.get("request_id") if isinstance(proposal, Mapping) else None
            request_id = request_id if isinstance(request_id, str) and 0 < len(request_id) <= 64 else None
            return self._record(now, REFUSE, Reason.MALFORMED_INTENT, str(exc), request_id, None)

        try:
            with self.ledger.session() as state:
                try:
                    reason, detail = self._evaluate(intent, state, now, order)
                except Exception as exc:  # fail-closed: a rule that breaks refuses
                    reason, detail = Reason.POLICY_ERROR, f"{type(exc).__name__}: {exc}"
                decision = ALLOW if reason is Reason.OK else REFUSE
                return self.ledger.append(state, decision=decision, reason=reason.value, detail=detail,
                                          request_id=intent.request_id, intent=intent.to_dict(),
                                          policy_hash=self.policy.hash, at=now)
        except LedgerError as exc:
            return self._unrecorded(now, Reason.LEDGER_UNAVAILABLE, str(exc), intent.request_id, intent.to_dict())

    def recheck(self, receipt: Receipt, order: Any = None, read_balance: bool = True) -> tuple[Reason, str]:
        """The rules once more for an allowed, recorded decision, right before it is paid: the
        policy the engine holds now, against the ledger now without this receipt's own decision, so
        its request ID is not a duplicate of itself and its amount does not count twice against its
        day. With `read_balance` False the reserve is not read again, for a payment whose decision
        read it a moment ago in the same call. Writes nothing and never raises."""
        try:
            intent = Intent.parse(receipt.intent)
            at = datetime.fromisoformat(receipt.at)
            with self.ledger.session() as state:
                # Only what the rules read: the other decisions. Request IDs stay out, since the only
                # decisions with this one's ID are itself and the duplicates refused after it.
                others = LedgerState(records=[r for r in state.records if r["seq"] != receipt.seq])
                return self._evaluate(intent, others, at, order, read_balance)
        except LedgerError as exc:
            return Reason.LEDGER_UNAVAILABLE, str(exc)
        except Exception as exc:  # fail-closed: a rule that breaks refuses
            return Reason.POLICY_ERROR, f"{type(exc).__name__}: {exc}"

    def _evaluate(self, intent: Intent, state: LedgerState, now: datetime,
                  order: Any = None, read_balance: bool = True) -> tuple[Reason, str]:
        cur = intent.currency
        if intent.request_id in state.request_ids:
            return Reason.DUPLICATE_REQUEST_ID, (
                f"request ID {intent.request_id} was already decided (receipt #{state.request_ids[intent.request_id]})")

        # Read inside the ledger session: a broken order raises here and is recorded as policy_error
        # under the proposal's request ID, so a retry with that ID is a duplicate.
        order = None if order is None else Order.parse(order)

        rule = self.policy.currencies.get(cur)
        if rule is None:
            allowed = ", ".join(sorted(self.policy.currencies))
            return Reason.CURRENCY_NOT_ALLOWED, f"{cur} is not an allowed currency (allowed: {allowed})"

        if self.policy.payees is not None and intent.payee not in self.policy.payees:
            if intent.payee is None:
                return Reason.PAYEE_NOT_ALLOWED, "no payee given, and this policy pays listed payees only"
            return Reason.PAYEE_NOT_ALLOWED, f"payee {intent.payee} is not on the owner's list"

        if self.policy.merchant_categories is not None and intent.merchant_category not in self.policy.merchant_categories:
            if intent.merchant_category is None:
                return Reason.MERCHANT_CATEGORY_NOT_ALLOWED, "no merchant category given, and this policy allows listed categories only"
            return Reason.MERCHANT_CATEGORY_NOT_ALLOWED, f"merchant category {intent.merchant_category} is not allowed"

        if intent.amount > rule.per_payment:
            return Reason.PER_PAYMENT_LIMIT_EXCEEDED, (
                f"{intent.amount} {cur} is above the per-payment limit of {rule.per_payment} {cur}")

        day = now.astimezone(timezone.utc).date()
        spent = state.allowed_on(cur, day)
        if spent + intent.amount > rule.daily:
            return Reason.DAILY_LIMIT_EXCEEDED, (
                f"{spent} {cur} already allowed on {day.isoformat()} (UTC); {intent.amount} more "
                f"would pass the daily limit of {rule.daily} {cur}")

        # After the limits, so a proposal over a limit is refused for the limit. Before the
        # balance, so a wrong amount never costs a call to the provider.
        if order is not None and (intent.currency != order.currency or intent.amount != order.amount):
            return Reason.AMOUNT_MISMATCH, (
                f"proposed {intent.amount} {cur}, but the order is {order.quantity} x {order.unit_price} "
                f"{order.currency} = {order.amount} {order.currency}")

        if rule.min_balance is not None and read_balance:
            balance = self._read_balance(cur)
            if balance is None:
                return Reason.BALANCE_UNAVAILABLE, f"the {cur} balance could not be read, so the reserve cannot be checked"
            after = balance - intent.amount
            if after < rule.min_balance:
                return Reason.BELOW_MIN_BALANCE, (
                    f"balance {balance} {cur} minus {intent.amount} leaves {after}, "
                    f"below the reserve of {rule.min_balance} {cur}")

        return Reason.OK, "within every rule of the policy"

    def _read_balance(self, currency: str) -> Decimal | None:
        if self.balance is None:
            return None
        try:
            value = self.balance(currency)
        except Exception:
            return None
        if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
            return None
        try:
            amount = Decimal(value)
        except ArithmeticError:
            return None
        return amount if amount.is_finite() else None

    def _record(self, now: datetime, decision: str, reason: Reason, detail: str,
                request_id: str | None, intent: dict[str, Any] | None) -> Receipt:
        try:
            with self.ledger.session() as state:
                return self.ledger.append(state, decision=decision, reason=reason.value, detail=detail,
                                          request_id=request_id, intent=intent,
                                          policy_hash=self.policy.hash, at=now)
        except LedgerError as exc:
            return self._unrecorded(now, Reason.LEDGER_UNAVAILABLE, str(exc), request_id, intent)

    def _unrecorded(self, now: datetime, reason: Reason, detail: str,
                    request_id: str | None, intent: dict[str, Any] | None) -> Receipt:
        return Receipt(decision=REFUSE, reason=reason.value, detail=detail, request_id=request_id,
                       intent=intent, policy_hash=self.policy.hash, at=iso_utc(now))
