"""PayPal sandbox: a client-credentials token, an order, its capture, and a refund.

The payment path is picked so no buyer has to click through PayPal for every payment. The order
is created with no payment source, then captured with `payment_source.card` in the capture
request. PayPal's Orders v2 reference (checkout_orders_v2.json, version 2.32) says a capture
succeeds when "the buyer must first approve the order or a valid payment_source must be provided
in the request". The card is either a saved card (`PAYPAL_CARD_VAULT_ID`) or, in the sandbox, a
test card from PayPal's sandbox card generator. Both are read from the environment by this
process and never shown to the agent. Passing raw card fields needs PCI SAQ D outside a sandbox,
which is why a saved card's vault ID is the first choice.

Every order carries its decision. `invoice_id` is the intent's request ID, so PayPal itself
refuses a second order with that ID (DUPLICATE_INVOICE_ID on its Orders v2 error page) even if
our engine were bypassed. `custom_id` names the receipt as `receipt:<seq>:<first 16 of its hash>`.

Sandbox only: a base URL whose host is not a PayPal sandbox host is refused when the
configuration is read.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from intent_pay.adapters.http import Response, Transport, UrllibTransport
from intent_pay.gateway import PaymentResult
from intent_pay.intent import Intent
from intent_pay.ledger import Receipt

PROVIDER = "paypal"
DEFAULT_BASE = "https://api-m.sandbox.paypal.com"
SANDBOX_HOSTS = frozenset({"api-m.sandbox.paypal.com", "api.sandbox.paypal.com"})
CARD_NUMBER = re.compile(r"[0-9]{13,19}")
EXPIRY = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
SECURITY_CODE = re.compile(r"[0-9]{3,4}")
TOKEN_MARGIN_S = 60


class ConfigError(ValueError):
    """The PayPal settings are missing or wrong. Messages name variables, never their values."""


class PayPalError(RuntimeError):
    pass


@dataclass(frozen=True)
class CardSource:
    vault_id: str | None = field(default=None, repr=False)
    number: str | None = field(default=None, repr=False)
    expiry: str | None = field(default=None, repr=False)
    security_code: str | None = field(default=None, repr=False)
    # Not secret. In the sandbox, a rejection trigger here (e.g. CCREJECT-REFUSED) makes the card decline.
    name: str | None = None

    def payment_source(self) -> dict[str, Any]:
        if self.vault_id:
            return {"card": {"vault_id": self.vault_id}}
        card: dict[str, Any] = {"number": self.number, "expiry": self.expiry}
        if self.security_code:
            card["security_code"] = self.security_code
        if self.name:
            card["name"] = self.name
        return {"card": card}


@dataclass(frozen=True)
class PayPalConfig:
    client_id: str
    client_secret: str = field(repr=False)
    card: CardSource = field(repr=False)
    base_url: str = DEFAULT_BASE

    def __post_init__(self) -> None:
        parts = urlsplit(self.base_url)
        if parts.scheme != "https" or parts.hostname not in SANDBOX_HOSTS:
            raise ConfigError("PAYPAL_API_BASE must be a PayPal sandbox host over https; this build calls the sandbox only")
        if not self.client_id or not self.client_secret:
            raise ConfigError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are required")
        c = self.card
        if c.vault_id:
            return
        if not (c.number and CARD_NUMBER.fullmatch(c.number)):
            raise ConfigError("set PAYPAL_CARD_VAULT_ID, or PAYPAL_CARD_NUMBER as 13 to 19 digits")
        if not (c.expiry and EXPIRY.fullmatch(c.expiry)):
            raise ConfigError("PAYPAL_CARD_EXPIRY must be YYYY-MM")
        if c.security_code and not SECURITY_CODE.fullmatch(c.security_code):
            raise ConfigError("PAYPAL_CARD_SECURITY_CODE must be 3 or 4 digits")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> PayPalConfig:
        missing = [n for n in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET") if not env.get(n)]
        if not env.get("PAYPAL_CARD_VAULT_ID"):
            missing += [n for n in ("PAYPAL_CARD_NUMBER", "PAYPAL_CARD_EXPIRY") if not env.get(n)]
        if missing:
            raise ConfigError(f"missing environment variable(s): {', '.join(missing)} (see .env.example)")
        card = CardSource(
            vault_id=env.get("PAYPAL_CARD_VAULT_ID") or None,
            number=(env.get("PAYPAL_CARD_NUMBER") or "").replace(" ", "") or None,
            expiry=env.get("PAYPAL_CARD_EXPIRY") or None,
            security_code=env.get("PAYPAL_CARD_SECURITY_CODE") or None,
            name=env.get("PAYPAL_CARD_NAME") or None,
        )
        return cls(client_id=env["PAYPAL_CLIENT_ID"], client_secret=env["PAYPAL_CLIENT_SECRET"], card=card,
                   base_url=(env.get("PAYPAL_API_BASE") or DEFAULT_BASE).rstrip("/"))


class PayPalClient:
    """Thin REST client. It holds the client secret and the access token; nothing it returns does."""

    def __init__(self, config: PayPalConfig, transport: Transport | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.config = config
        self.transport = transport or UrllibTransport()
        self.clock = clock
        self._token: str | None = None
        self._token_expires = 0.0
        # One token fetch at a time; threads that arrive meanwhile reuse the token it got.
        self._token_lock = threading.Lock()

    def _access_token(self) -> str:
        with self._token_lock:
            token = self._token
            if token and self.clock() < self._token_expires:
                return token
            return self._fetch_token()

    def _fetch_token(self) -> str:
        basic = base64.b64encode(f"{self.config.client_id}:{self.config.client_secret}".encode()).decode()
        resp = self.transport("POST", self.config.base_url + "/v1/oauth2/token",
                              {"Authorization": f"Basic {basic}", "Accept": "application/json",
                               "Content-Type": "application/x-www-form-urlencoded"},
                              b"grant_type=client_credentials")
        body = _json(resp)
        if resp.status != 200 or not body.get("access_token"):
            raise PayPalError(f"token request failed: HTTP {resp.status} {body.get('error', '')} "
                              f"{body.get('error_description', '')}".rstrip())
        token = body["access_token"]
        self._token = token
        self._token_expires = self.clock() + max(0, int(body.get("expires_in", 0)) - TOKEN_MARGIN_S)
        return token

    def _drop_token(self, token: str) -> None:
        """Forget `token` after a 401, unless another thread has already replaced it."""
        with self._token_lock:
            if self._token == token:
                self._token = None

    def call(self, method: str, path: str, body: dict[str, Any] | None, request_id: str) -> Response:
        for attempt in (1, 2):
            token = self._access_token()
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                       "Prefer": "return=representation", "PayPal-Request-Id": request_id}
            data = json.dumps(body).encode("utf-8") if body is not None else b"{}"
            resp = self.transport(method, self.config.base_url + path, headers, data)
            if resp.status != 401 or attempt == 2:
                return resp
            self._drop_token(token)  # expired or revoked early: fetch a new one and try once more
        raise AssertionError("unreachable")

    def create_order(self, intent: Intent, receipt: Receipt) -> Response:
        unit: dict[str, Any] = {
            "reference_id": intent.request_id,
            "invoice_id": intent.request_id,
            "custom_id": f"receipt:{receipt.seq}:{(receipt.hash or '')[:16]}",
            "description": intent.purpose,
            "amount": {"currency_code": intent.currency, "value": str(intent.amount)},
        }
        if intent.payee:
            unit["payee"] = {"email_address": intent.payee}
        return self.call("POST", "/v2/checkout/orders", {"intent": "CAPTURE", "purchase_units": [unit]},
                         request_id=f"{intent.request_id}-order")

    def capture_order(self, order_id: str, request_id: str) -> Response:
        return self.call("POST", f"/v2/checkout/orders/{order_id}/capture",
                         {"payment_source": self.config.card.payment_source()}, request_id=f"{request_id}-capture")

    def refund_capture(self, capture_id: str, request_id: str) -> Response:
        return self.call("POST", f"/v2/payments/captures/{capture_id}/refund", {}, request_id=f"{request_id}-refund")


def _json(resp: Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def _error(resp: Response) -> tuple[str, str]:
    """PayPal's error body as (status, detail): `UNPROCESSABLE_ENTITY:INSTRUMENT_DECLINED` and the
    first issue's description with PayPal's debug_id, which their support asks for."""
    body = _json(resp)
    name = body.get("name") or f"HTTP_{resp.status}"
    details = body.get("details") or [{}]
    issue = details[0].get("issue") if isinstance(details[0], dict) else None
    text = (details[0].get("description") if isinstance(details[0], dict) else None) or body.get("message") or ""
    debug = body.get("debug_id")
    return (f"{name}:{issue}" if issue else name), (f"{text} (debug_id {debug})" if debug else text)


def _step(call: str, resp: Response, obj_id: str | None = None, status: str | None = None) -> dict[str, Any]:
    body = _json(resp)
    step = {"call": call, "http": resp.status, "id": obj_id or body.get("id"), "status": status or body.get("status")}
    if resp.status >= 400:
        step["status"] = _error(resp)[0]
        if body.get("debug_id"):
            step["debug_id"] = body["debug_id"]
    return step


def first_capture(order: dict[str, Any]) -> dict[str, Any]:
    for unit in order.get("purchase_units") or []:
        captures = (unit.get("payments") or {}).get("captures") or []
        if captures:
            return captures[0]
    return {}


class PayPalPayer:
    provider = PROVIDER

    def __init__(self, client: PayPalClient):
        self.client = client

    def pay(self, intent: Intent, receipt: Receipt) -> PaymentResult:
        steps: list[dict[str, Any]] = []
        created = self.client.create_order(intent, receipt)
        steps.append(_step("create_order", created))
        if created.status not in (200, 201):
            status, detail = _error(created)
            return PaymentResult(PROVIDER, "pay", False, status, None, f"order not created: {detail}", tuple(steps))
        order_id = _json(created).get("id")
        if not order_id:
            return PaymentResult(PROVIDER, "pay", False, "NO_ORDER_ID", None, "PayPal returned no order ID", tuple(steps))

        captured = self.client.capture_order(order_id, intent.request_id)
        capture = first_capture(_json(captured))
        steps.append(_step("capture_order", captured, capture.get("id"), capture.get("status")))
        if captured.status not in (200, 201):
            status, detail = _error(captured)
            return PaymentResult(PROVIDER, "pay", False, status, None, f"order {order_id} not captured: {detail}", tuple(steps))
        status = capture.get("status") or "UNKNOWN"
        # PENDING is money on its way (PayPal holds it for review); it can still be refunded.
        ok = status in ("COMPLETED", "PENDING")
        amount = capture.get("amount") or {}
        detail = (f"order {order_id}, capture {capture.get('id')}: {status} "
                  f"{amount.get('value', '?')} {amount.get('currency_code', '')}").rstrip()
        return PaymentResult(PROVIDER, "pay", ok, status, capture.get("id") if ok else None, detail, tuple(steps))

    def refund(self, reference: str, request_id: str) -> PaymentResult:
        resp = self.client.refund_capture(reference, request_id)
        step = _step("refund_capture", resp)
        if resp.status not in (200, 201):
            status, detail = _error(resp)
            return PaymentResult(PROVIDER, "refund", False, status, None, f"capture {reference} not refunded: {detail}", (step,))
        body = _json(resp)
        status = body.get("status") or "UNKNOWN"
        ok = status in ("COMPLETED", "PENDING")
        amount = body.get("amount") or {}
        detail = f"refund {body.get('id')} of capture {reference}: {status} {amount.get('value', '?')} {amount.get('currency_code', '')}".rstrip()
        return PaymentResult(PROVIDER, "refund", ok, status, body.get("id") if ok else None, detail, (step,))
