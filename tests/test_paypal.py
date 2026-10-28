"""The PayPal adapter on cassettes recorded from the PayPal sandbox (see each file's _source)."""

import base64
import json
import pathlib

import pytest

from conftest import intent
from intent_pay.adapters.http import ReplayTransport
from intent_pay.adapters.paypal import CardSource, ConfigError, PayPalClient, PayPalConfig, PayPalPayer
from intent_pay.intent import Intent
from intent_pay.ledger import Receipt

FIX = pathlib.Path(__file__).parent / "fixtures" / "paypal"
CARD = CardSource(number="4012888888881881", expiry="2030-12", security_code="123", name="Owner Card")
SECRET = "client-secret-never-printed"
ORDER, CAPTURE, REFUND = "3B048052DS900025X", "5S461376DW338105P", "38J69305F2856770N"  # recorded IDs


def config(card=CARD):
    return PayPalConfig(client_id="client-id", client_secret=SECRET, card=card)


def receipt(seq=7, digest="ab" * 32):
    return Receipt(decision="allow", reason="ok", detail="", request_id="req-1", intent=None,
                   policy_hash="0" * 64, at="2026-10-27T12:00:00.000000Z", seq=seq, hash=digest)


def payer(cassette, card=CARD, clock=lambda: 0.0):
    transport = ReplayTransport(FIX / cassette)
    return PayPalPayer(PayPalClient(config(card), transport, clock=clock)), transport


def body(call):
    return json.loads(call["body"].decode())


def test_pay_creates_then_captures_with_the_card():
    p, t = payer("pay-completed.json")
    r = p.pay(Intent.parse(intent(payee="billing@acme.example")), receipt())
    assert r.ok and r.status == "COMPLETED" and r.reference == CAPTURE
    assert [s["call"] for s in r.steps] == ["create_order", "capture_order"]
    assert t.finished

    token, create, capture = t.calls
    assert token["headers"]["Authorization"] == "Basic " + base64.b64encode(f"client-id:{SECRET}".encode()).decode()
    assert token["body"] == b"grant_type=client_credentials"

    order = body(create)
    assert create["headers"]["PayPal-Request-Id"] == "req-1-order"
    assert create["headers"]["Authorization"] == "Bearer REDACTED"  # the cassette's token
    unit = order["purchase_units"][0]
    assert order["intent"] == "CAPTURE"
    assert unit["amount"] == {"currency_code": "USD", "value": "49.00"}
    assert unit["invoice_id"] == "req-1"  # PayPal refuses a second order with this ID by itself
    assert unit["custom_id"] == "receipt:7:" + "ab" * 8
    assert unit["payee"] == {"email_address": "billing@acme.example"}
    assert "payment_source" not in order

    assert capture["headers"]["PayPal-Request-Id"] == "req-1-capture"
    assert body(capture)["payment_source"] == {"card": {"number": "4012888888881881", "expiry": "2030-12",
                                                        "security_code": "123", "name": "Owner Card"}}


def test_saved_card_sends_only_its_vault_id():
    p, t = payer("pay-completed.json", card=CardSource(vault_id="vault-123"))
    assert p.pay(Intent.parse(intent()), receipt()).ok
    assert body(t.calls[2])["payment_source"] == {"card": {"vault_id": "vault-123"}}


def test_order_without_payee_lets_paypal_pay_the_app_account():
    p, t = payer("pay-completed.json")
    p.pay(Intent.parse(intent(payee=None)), receipt())
    assert "payee" not in body(t.calls[1])["purchase_units"][0]


def test_declined_card_is_a_failed_payment():
    # PayPal answers the capture of a refused card with HTTP 201; the capture itself says DECLINED.
    p, t = payer("pay-capture-declined.json")
    r = p.pay(Intent.parse(intent()), receipt())
    assert not r.ok and r.status == "DECLINED" and r.reference is None
    assert r.steps[-1]["http"] == 201 and t.finished


def test_reused_invoice_id_is_refused_by_paypal_at_capture():
    # A new PayPal-Request-Id with an invoice_id PayPal has already been paid for: the order is
    # created, the capture is refused. Only on an account that blocks duplicate invoice IDs.
    p, t = payer("pay-duplicate-invoice.json")
    r = p.pay(Intent.parse(intent()), receipt())
    assert not r.ok and r.reference is None and r.status == "UNPROCESSABLE_ENTITY:DUPLICATE_INVOICE_ID"
    assert "debug_id ca51308ad1b61" in r.detail
    assert r.steps[-1]["http"] == 422 and r.steps[-1]["debug_id"] == "ca51308ad1b61" and t.finished


def test_same_request_id_gets_paypal_first_answer_back():
    # The same PayPal-Request-Id headers again: PayPal answers 200 with the first order and capture.
    p, t = payer("pay-same-request-id.json")
    r = p.pay(Intent.parse(intent()), receipt())
    assert r.ok and r.reference == CAPTURE and [s["http"] for s in r.steps] == [200, 200]


def test_account_without_card_processing_is_a_failed_payment():
    p, t = payer("pay-card-not-enabled.json")
    r = p.pay(Intent.parse(intent()), receipt())
    assert not r.ok and r.status == "UNPROCESSABLE_ENTITY:PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING" and t.finished


def test_refund():
    p, t = payer("refund-completed.json")
    r = p.refund(CAPTURE, "req-1")
    assert r.ok and r.operation == "refund" and r.reference == REFUND
    assert t.calls[1]["headers"]["PayPal-Request-Id"] == "req-1-refund"
    assert body(t.calls[1]) == {}


def test_token_is_reused_until_it_expires():
    cassette = json.loads((FIX / "pay-completed.json").read_text())
    token, create, capture = cassette["exchanges"]
    now = [0.0]
    client = PayPalClient(config(), ReplayTransport({"exchanges": [token, create, token, capture]}), clock=lambda: now[0])
    client.create_order(Intent.parse(intent()), receipt())
    now[0] = 32400 - 59  # within PayPal's expires_in, but inside our one-minute margin
    client.capture_order(ORDER, "req-1")
    assert client.transport.finished


def test_expired_token_is_refreshed_once_on_401():
    cassette = json.loads((FIX / "pay-completed.json").read_text())
    token, create, _ = cassette["exchanges"]
    unauthorized = {"request": create["request"], "response": {"status": 401, "body": {"name": "AUTHENTICATION_FAILURE"}}}
    t = ReplayTransport({"exchanges": [token, unauthorized, token, create]})
    resp = PayPalClient(config(), t).create_order(Intent.parse(intent()), receipt())
    assert resp.status == 201 and t.finished


def test_second_401_is_returned_not_retried_forever():
    cassette = json.loads((FIX / "pay-completed.json").read_text())
    token, create, _ = cassette["exchanges"]
    unauthorized = {"request": create["request"], "response": {"status": 401, "body": {"name": "AUTHENTICATION_FAILURE"}}}
    t = ReplayTransport({"exchanges": [token, unauthorized, token, unauthorized]})
    p = PayPalPayer(PayPalClient(config(), t))
    r = p.pay(Intent.parse(intent()), receipt())
    assert not r.ok and r.status == "AUTHENTICATION_FAILURE" and t.finished


def test_token_failure_names_paypal_error_not_the_secret():
    t = ReplayTransport({"exchanges": [{"request": {"method": "POST", "path": "/v1/oauth2/token"},
                                        "response": {"status": 401, "body": {"error": "invalid_client",
                                                                             "error_description": "Client Authentication failed"}}}]})
    with pytest.raises(Exception) as err:
        PayPalClient(config(), t).create_order(Intent.parse(intent()), receipt())
    assert "invalid_client" in str(err.value) and SECRET not in str(err.value)


ENV = {"PAYPAL_CLIENT_ID": "id", "PAYPAL_CLIENT_SECRET": SECRET, "PAYPAL_CARD_NUMBER": "4012 8888 8888 1881",
       "PAYPAL_CARD_EXPIRY": "2030-12", "PAYPAL_CARD_SECURITY_CODE": "123"}


def test_config_from_env():
    c = PayPalConfig.from_env(ENV)
    assert c.base_url == "https://api-m.sandbox.paypal.com"
    assert c.card.number == "4012888888881881"
    shown = repr(c)
    assert SECRET not in shown and "4012888888881881" not in shown and "123" not in shown


def test_config_vault_id_needs_no_card_number():
    env = {"PAYPAL_CLIENT_ID": "id", "PAYPAL_CLIENT_SECRET": SECRET, "PAYPAL_CARD_VAULT_ID": "v-1"}
    assert PayPalConfig.from_env(env).card.payment_source() == {"card": {"vault_id": "v-1"}}


@pytest.mark.parametrize("change, message", [
    ({"PAYPAL_CLIENT_SECRET": ""}, "PAYPAL_CLIENT_SECRET"),
    ({"PAYPAL_CARD_NUMBER": ""}, "PAYPAL_CARD_NUMBER"),
    ({"PAYPAL_CARD_NUMBER": "4012-8888"}, "13 to 19 digits"),
    ({"PAYPAL_CARD_EXPIRY": "12/30"}, "YYYY-MM"),
    ({"PAYPAL_CARD_SECURITY_CODE": "12a"}, "3 or 4 digits"),
    ({"PAYPAL_API_BASE": "https://api-m.paypal.com"}, "sandbox"),
    ({"PAYPAL_API_BASE": "http://api-m.sandbox.paypal.com"}, "sandbox"),
])
def test_bad_config_is_refused_without_echoing_values(change, message):
    with pytest.raises(ConfigError, match=message) as err:
        PayPalConfig.from_env({**ENV, **change})
    assert SECRET not in str(err.value) and "4012" not in str(err.value)


def test_threads_share_one_token_fetch():
    import threading
    import time
    cassette = json.loads((FIX / "pay-completed.json").read_text())
    token, create, _ = cassette["exchanges"]
    replay = ReplayTransport({"exchanges": [token] + [create] * 4})

    def slow_token(method, url, headers, body):
        if url.endswith("/v1/oauth2/token"):
            time.sleep(0.05)  # long enough for every thread to arrive while the first one waits
        return replay(method, url, headers, body)

    client = PayPalClient(config(), slow_token)
    errors = []
    def run():
        try:
            assert client.create_order(Intent.parse(intent()), receipt()).status == 201
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and replay.finished
    assert sum(c["url"].endswith("/v1/oauth2/token") for c in replay.calls) == 1
