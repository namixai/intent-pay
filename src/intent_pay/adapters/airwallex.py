"""Airwallex sandbox: a token, a cardholder, one virtual card per allowed payment, and the
vendor's charge on that card, run as an issuing simulation.

An allowed intent gets its own card. The card's authorization controls repeat the policy: the
intent's currency is its only currency, the policy's merchant categories are its only
categories, the policy's per-payment and daily limits are its PER_TRANSACTION and DAILY limits,
and its ALL_TIME limit is the amount the engine allowed. So the rule is checked twice: by our
engine before the card exists, and by Airwallex whenever the card is charged.

In the sandbox no vendor charges the card, so this process plays the vendor with Airwallex's
issuing simulation (`POST /api/v1/simulation/issuing/create`, then a capture of that
authorization's lifecycle). `simulate_charge` plays a vendor that charges the card again
without asking anyone, which is how the demo shows the card refusing on its own. Airwallex's
decline reasons for the rules both sides check are in `CARD_REASONS`.

The minimum cash reserve reads the wallet's available balance (`GET /api/v1/balances/current`).

Sandbox only: a base URL whose host is not an Airwallex sandbox host is refused when the
configuration is read, and the simulation endpoints exist only in the sandbox.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit

from intent_pay.adapters.http import Response, Transport, UrllibTransport
from intent_pay.gateway import PaymentResult
from intent_pay.intent import Intent
from intent_pay.ledger import Receipt
from intent_pay.policy import Policy

PROVIDER = "airwallex"
DEFAULT_BASE = "https://api.sandbox.airwallex.com"
SANDBOX_HOSTS = frozenset({"api.sandbox.airwallex.com", "api-demo.airwallex.com"})
# Airwallex: "The access token remains valid for 30 minutes and can be reused until it expires."
TOKEN_LIFETIME_S = 30 * 60
TOKEN_MARGIN_S = 60
# The simulation takes a merchant description of exactly 43 characters.
MERCHANT_INFO = "INTENT PAY SANDBOX VENDOR".ljust(43)
CARD_WAIT_TRIES = 5

# Our refusal reasons, and the decline reason Airwallex gives when the card's own control stops
# the same charge. Rules with no card control (request IDs, the reserve) have no entry.
CARD_REASONS = {
    "per_payment_limit_exceeded": "LIMIT_EXCEEDED",
    "daily_limit_exceeded": "LIMIT_EXCEEDED",
    "currency_not_allowed": "CURRENCY_NOT_ALLOWED",
    "merchant_category_not_allowed": "MERCHANT_CATEGORY_NOT_ALLOWED",
}

# The cardholder the gateway creates when none is configured: a sandbox person, not a real one.
DEMO_CARDHOLDER = {
    "type": "INDIVIDUAL",
    "email": "agent@intent-pay.example",
    "individual": {
        "name": {"first_name": "Sandbox", "last_name": "Agent"},
        "date_of_birth": "1990-01-01",
        "address": {"line1": "1 Market Street", "city": "San Francisco", "state": "CA",
                    "postcode": "94105", "country": "US"},
        "express_consent_obtained": "yes",
    },
}


class ConfigError(ValueError):
    """The Airwallex settings are missing or wrong. Messages name variables, never their values."""


class AirwallexError(RuntimeError):
    pass


@dataclass(frozen=True)
class AirwallexConfig:
    client_id: str
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE
    # Only for a key that covers several accounts: the account to act for (x-login-as).
    login_as: str | None = None
    # An existing cardholder to issue cards to. Without one, the gateway finds or creates one.
    cardholder_id: str | None = None

    def __post_init__(self) -> None:
        parts = urlsplit(self.base_url)
        if parts.scheme != "https" or parts.hostname not in SANDBOX_HOSTS:
            raise ConfigError("AIRWALLEX_API_BASE must be an Airwallex sandbox host over https; "
                              "this build calls the sandbox only")
        if not self.client_id or not self.api_key:
            raise ConfigError("AIRWALLEX_CLIENT_ID and AIRWALLEX_API_KEY are required")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> AirwallexConfig:
        missing = [n for n in ("AIRWALLEX_CLIENT_ID", "AIRWALLEX_API_KEY") if not env.get(n)]
        if missing:
            raise ConfigError(f"missing environment variable(s): {', '.join(missing)} (see .env.example)")
        return cls(client_id=env["AIRWALLEX_CLIENT_ID"], api_key=env["AIRWALLEX_API_KEY"],
                   base_url=(env.get("AIRWALLEX_API_BASE") or DEFAULT_BASE).rstrip("/"),
                   login_as=env.get("AIRWALLEX_LOGIN_AS") or None,
                   cardholder_id=env.get("AIRWALLEX_CARDHOLDER_ID") or None)


def _number(amount: Decimal) -> int | float:
    """Airwallex takes amounts as JSON numbers. A Decimal with at most two places converts
    exactly in both directions."""
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def _json(resp: Response) -> Any:
    try:
        return resp.json()
    except (ValueError, UnicodeDecodeError):
        return {}


def _obj(resp: Response) -> dict[str, Any]:
    body = _json(resp)
    return body if isinstance(body, dict) else {}


def _error(resp: Response) -> tuple[str, str]:
    """Airwallex's error body as (code, message), e.g. ("validation_error", "...")."""
    body = _obj(resp)
    return (body.get("code") or f"HTTP_{resp.status}"), str(body.get("message") or "")


class AirwallexClient:
    """Thin REST client. It holds the API key and the access token; nothing it returns does."""

    def __init__(self, config: AirwallexConfig, transport: Transport | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.config = config
        self.transport = transport or UrllibTransport()
        self.clock = clock
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = threading.Lock()

    def _access_token(self) -> str:
        with self._token_lock:
            token = self._token
            if token and self.clock() < self._token_expires:
                return token
            return self._fetch_token()

    def _fetch_token(self) -> str:
        headers = {"x-client-id": self.config.client_id, "x-api-key": self.config.api_key,
                   "Content-Type": "application/json"}
        if self.config.login_as:
            headers["x-login-as"] = self.config.login_as
        resp = self.transport("POST", self.config.base_url + "/api/v1/authentication/login", headers, b"")
        body = _obj(resp)
        if resp.status not in (200, 201) or not body.get("token"):
            code, message = _error(resp)
            raise AirwallexError(f"login failed: HTTP {resp.status} {code} {message}".rstrip())
        self._token = body["token"]
        self._token_expires = self.clock() + TOKEN_LIFETIME_S - TOKEN_MARGIN_S
        return self._token

    def _drop_token(self, token: str) -> None:
        with self._token_lock:
            if self._token == token:
                self._token = None

    def call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Response:
        for attempt in (1, 2):
            token = self._access_token()
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            data = json.dumps(body).encode("utf-8") if body is not None else None
            resp = self.transport(method, self.config.base_url + path, headers, data)
            if resp.status != 401 or attempt == 2:
                return resp
            self._drop_token(token)  # expired early: log in again and try once more
        raise AssertionError("unreachable")

    def balances(self) -> Response:
        return self.call("GET", "/api/v1/balances/current")

    def find_cardholders(self, email: str) -> Response:
        return self.call("GET", f"/api/v1/issuing/cardholders?email={quote(email)}&cardholder_status=READY")

    def create_cardholder(self, details: Mapping[str, Any]) -> Response:
        return self.call("POST", "/api/v1/issuing/cardholders/create", details)

    def pass_review(self, cardholder_id: str) -> Response:
        return self.call("POST", f"/api/v1/simulation/issuing/cardholders/{cardholder_id}/pass_review", {})

    def create_card(self, body: Mapping[str, Any]) -> Response:
        return self.call("POST", "/api/v1/issuing/cards/create", body)

    def get_card(self, card_id: str) -> Response:
        return self.call("GET", f"/api/v1/issuing/cards/{card_id}")

    def simulate_authorization(self, card_id: str, amount: Decimal, currency: str, mcc: str | None) -> Response:
        body: dict[str, Any] = {"card_id": card_id, "transaction_amount": _number(amount),
                                "transaction_currency": currency, "merchant_info": MERCHANT_INFO}
        if mcc:
            body["merchant_category_code"] = mcc
        return self.call("POST", "/api/v1/simulation/issuing/create", body)

    def simulate_capture(self, lifecycle_id: str) -> Response:
        return self.call("POST", f"/api/v1/simulation/issuing/card_transaction_lifecycles/{lifecycle_id}/capture", {})

    def simulate_reversal(self, lifecycle_id: str) -> Response:
        return self.call("POST", f"/api/v1/simulation/issuing/card_transaction_lifecycles/{lifecycle_id}/reverse", {})


def card_controls(policy: Policy, intent: Intent) -> dict[str, Any]:
    """The card's authorization controls: the policy's rules for this currency, plus the
    allowed amount as the most the card can ever spend."""
    rule = policy.currencies[intent.currency]
    controls: dict[str, Any] = {
        "allowed_transaction_count": "MULTIPLE",
        "allowed_currencies": [intent.currency],
        "transaction_limits": {
            "currency": intent.currency,
            "limits": [
                {"amount": _number(rule.per_payment), "interval": "PER_TRANSACTION"},
                {"amount": _number(rule.daily), "interval": "DAILY"},
                {"amount": _number(intent.amount), "interval": "ALL_TIME"},
            ],
        },
    }
    if policy.merchant_categories is not None:
        controls["allowed_merchant_categories"] = sorted(policy.merchant_categories)
    return controls


def _step(call: str, resp: Response, obj_id: str | None = None, status: str | None = None) -> dict[str, Any]:
    step = {"call": call, "http": resp.status, "id": obj_id, "status": status}
    if resp.status >= 400:
        step["status"] = _error(resp)[0]
    return step


def _sim_status(body: Mapping[str, Any]) -> str:
    """APPROVED, or DECLINED:<Airwallex's failure_reason>."""
    result = body.get("process_result") or "UNKNOWN"
    if result == "DECLINED":
        return f"DECLINED:{body.get('failure_reason') or 'UNKNOWN'}"
    return str(result)


class AirwallexPayer:
    provider = PROVIDER

    def __init__(self, client: AirwallexClient, policy: Policy, sleep: Callable[[float], None] = time.sleep):
        self.client = client
        self.policy = policy
        self.sleep = sleep
        self._cardholder_id = client.config.cardholder_id

    def available_balance(self, currency: str) -> Decimal:
        """The wallet's available balance in `currency`, for the minimum-reserve rule. Anything
        unexpected raises, and the engine refuses as balance_unavailable."""
        resp = self.client.balances()
        body = _json(resp)
        if resp.status != 200 or not isinstance(body, list):
            raise AirwallexError(f"balances: HTTP {resp.status} {_error(resp)[0]}")
        for item in body:
            if isinstance(item, dict) and item.get("currency") == currency:
                return Decimal(str(item["available_amount"]))
        return Decimal(0)

    def _cardholder(self, steps: list[dict[str, Any]]) -> str:
        if self._cardholder_id:
            return self._cardholder_id
        found = self.client.find_cardholders(DEMO_CARDHOLDER["email"])
        items = _obj(found).get("items") or []
        steps.append(_step("find_cardholder", found, items[0].get("cardholder_id") if items else None,
                           items[0].get("status") if items else "NONE"))
        if found.status == 200 and items:
            self._cardholder_id = items[0]["cardholder_id"]
            return self._cardholder_id
        created = self.client.create_cardholder(DEMO_CARDHOLDER)
        body = _obj(created)
        steps.append(_step("create_cardholder", created, body.get("cardholder_id"), body.get("status")))
        if created.status not in (200, 201, 202) or not body.get("cardholder_id"):
            code, message = _error(created)
            raise AirwallexError(f"cardholder not created: {code} {message}".rstrip())
        cardholder_id = body["cardholder_id"]
        if body.get("status") != "READY":
            # The sandbox reviews new cardholders; its simulation endpoint passes the review.
            reviewed = self.client.pass_review(cardholder_id)
            steps.append(_step("pass_review", reviewed, cardholder_id, "OK" if reviewed.status == 200 else None))
        self._cardholder_id = cardholder_id
        return cardholder_id

    def _active_card(self, card_id: str, status: str | None, steps: list[dict[str, Any]]) -> str | None:
        """Wait a little for a new card to become ACTIVE; return its last status."""
        tries = 0
        while status in (None, "PENDING", "INACTIVE") and tries < CARD_WAIT_TRIES:
            tries += 1
            self.sleep(1.0)
            resp = self.client.get_card(card_id)
            status = _obj(resp).get("card_status") if resp.status == 200 else None
            steps.append(_step("get_card", resp, card_id, status))
        return status

    def pay(self, intent: Intent, receipt: Receipt) -> PaymentResult:
        steps: list[dict[str, Any]] = []
        try:
            cardholder_id = self._cardholder(steps)
        except AirwallexError as exc:
            return PaymentResult(PROVIDER, "pay", False, "NO_CARDHOLDER", None, str(exc), tuple(steps))

        body = {
            "request_id": f"{intent.request_id}-card",
            "cardholder_id": cardholder_id,
            "created_by": "intent-pay gateway",
            "form_factor": "VIRTUAL",
            "is_personalized": False,
            "activate_on_issue": True,
            "program": {"purpose": "COMMERCIAL"},
            "nick_name": f"intent-pay {intent.request_id}"[:64],
            "client_data": f"receipt:{receipt.seq}:{(receipt.hash or '')[:16]}",
            "authorization_controls": card_controls(self.policy, intent),
        }
        created = self.client.create_card(body)
        card = _obj(created)
        card_id = card.get("card_id")
        steps.append(_step("create_card", created, card_id, card.get("card_status")))
        if created.status not in (200, 201, 202) or not card_id:
            code, message = _error(created)
            return PaymentResult(PROVIDER, "pay", False, code, None, f"card not created: {message}".rstrip(": "), tuple(steps))
        status = self._active_card(card_id, card.get("card_status"), steps)
        if status != "ACTIVE":
            return PaymentResult(PROVIDER, "pay", False, f"CARD_{status or 'UNKNOWN'}", None,
                                 f"card {card_id} is {status or 'unknown'}, not ACTIVE", tuple(steps))

        auth = self.client.simulate_authorization(card_id, intent.amount, intent.currency, intent.merchant_category)
        a = _obj(auth)
        lifecycle = a.get("card_transaction_lifecycle_id")
        steps.append(_step("authorize", auth, lifecycle, _sim_status(a) if auth.status < 400 else None))
        if auth.status >= 400:
            code, message = _error(auth)
            return PaymentResult(PROVIDER, "pay", False, code, None, f"card {card_id}: authorization failed: {message}", tuple(steps))
        if a.get("process_result") != "APPROVED" or not lifecycle:
            return PaymentResult(PROVIDER, "pay", False, _sim_status(a), None,
                                 f"card {card_id}: Airwallex declined the authorization ({a.get('failure_reason')})", tuple(steps))

        cap = self.client.simulate_capture(lifecycle)
        c = _obj(cap)
        steps.append(_step("capture", cap, lifecycle, _sim_status(c) if cap.status < 400 else None))
        if cap.status >= 400 or c.get("process_result") != "APPROVED":
            code = _error(cap)[0] if cap.status >= 400 else _sim_status(c)
            return PaymentResult(PROVIDER, "pay", False, code, None,
                                 f"card {card_id}: authorized, capture failed ({code})", tuple(steps))
        detail = f"card {card_id}: {intent.amount} {intent.currency} authorized and captured, lifecycle {lifecycle}"
        return PaymentResult(PROVIDER, "pay", True, "CLEARED", card_id, detail, tuple(steps))

    def simulate_charge(self, card_id: str, amount: Decimal, currency: str, mcc: str | None) -> PaymentResult:
        """A vendor charging the card directly, with no intent and no policy. If Airwallex
        approves it, the hold is reversed at once; the result says the card let it through, and
        whether the reversal did too."""
        steps: list[dict[str, Any]] = []
        auth = self.client.simulate_authorization(card_id, amount, currency, mcc)
        a = _obj(auth)
        lifecycle = a.get("card_transaction_lifecycle_id")
        status = _sim_status(a) if auth.status < 400 else _error(auth)[0]
        steps.append(_step("authorize", auth, lifecycle, status if auth.status < 400 else None))
        if auth.status >= 400:
            return PaymentResult(PROVIDER, "forced_charge", False, status, None,
                                 f"card {card_id}: the simulation was not accepted: {_error(auth)[1]}", tuple(steps))
        if a.get("process_result") == "APPROVED":
            reversed_hold = False
            if lifecycle:
                rev = self.client.simulate_reversal(lifecycle)
                reversed_hold = rev.status < 400
                steps.append(_step("reverse", rev, lifecycle, _sim_status(_obj(rev)) if reversed_hold else None))
            if not reversed_hold:
                return PaymentResult(PROVIDER, "forced_charge", True, "APPROVED_NOT_REVERSED", lifecycle,
                                     f"card {card_id}: Airwallex approved {amount} {currency}; the hold could not "
                                     f"be reversed and is still on the card", tuple(steps))
            return PaymentResult(PROVIDER, "forced_charge", True, "APPROVED", lifecycle,
                                 f"card {card_id}: Airwallex approved {amount} {currency}; the hold was reversed", tuple(steps))
        return PaymentResult(PROVIDER, "forced_charge", False, status, lifecycle,
                             f"card {card_id}: Airwallex declined {amount} {currency}: {a.get('failure_reason')}", tuple(steps))

    def refund(self, reference: str, request_id: str) -> PaymentResult:
        return PaymentResult(PROVIDER, "refund", False, "NOT_BUILT", None,
                             "refunds are not built for the Airwallex adapter yet")
