import json

import pytest

from intent_pay.adapters.http import CassetteMismatch, RecordingTransport, ReplayTransport, Response, redact

SECRETS = ["client-secret-x", "A21AAtokenvalue", "4012888888881881", "2030-12", "987", "vault-abc"]


def fake_sandbox(method, url, headers, body):
    if url.endswith("/v1/oauth2/token"):
        return Response(200, json.dumps({"access_token": "A21AAtokenvalue", "expires_in": 32400}).encode(), {})
    return Response(201, json.dumps({"id": "ORDER1", "status": "COMPLETED"}).encode(), {})


def test_recording_drops_every_secret(tmp_path):
    path = tmp_path / "cassette.json"
    rec = RecordingTransport(fake_sandbox, path, source="test")
    rec("POST", "https://api-m.sandbox.paypal.com/v1/oauth2/token",
        {"Authorization": "Basic Y2xpZW50LXNlY3JldC14", "Content-Type": "application/x-www-form-urlencoded"},
        b"grant_type=client_credentials&client_secret=client-secret-x")
    rec("POST", "https://api-m.sandbox.paypal.com/v2/checkout/orders/ORDER1/capture",
        {"Authorization": "Bearer A21AAtokenvalue", "PayPal-Request-Id": "req-1-capture"},
        json.dumps({"payment_source": {"card": {"number": "4012888888881881", "expiry": "2030-12",
                                                "security_code": "987", "vault_id": "vault-abc", "name": "Owner"}}}).encode())
    text = path.read_text()
    for secret in SECRETS + ["Y2xpZW50LXNlY3JldC14", "Authorization"]:
        assert secret not in text
    cassette = json.loads(text)
    capture = cassette["exchanges"][1]["request"]
    assert capture["headers"] == {"PayPal-Request-Id": "req-1-capture"}
    assert capture["body"]["payment_source"]["card"]["name"] == "Owner"
    assert cassette["exchanges"][0]["request"]["body"] == {"form": ["client_secret", "grant_type"]}


def test_recorded_cassette_replays():
    cassette = {"exchanges": [{"request": {"method": "GET", "path": "/v2/checkout/orders/1"},
                               "response": {"status": 200, "body": {"id": "1"}}}]}
    t = ReplayTransport(cassette)
    assert t("GET", "https://api-m.sandbox.paypal.com/v2/checkout/orders/1", {}, None).json() == {"id": "1"}
    assert t.finished


def test_replay_refuses_a_call_the_recording_never_saw():
    t = ReplayTransport({"exchanges": [{"request": {"method": "POST", "path": "/v2/checkout/orders"},
                                        "response": {"status": 201, "body": {}}}]})
    with pytest.raises(CassetteMismatch):
        t("POST", "https://api-m.sandbox.paypal.com/v2/payments/captures/1/refund", {}, None)
    with pytest.raises(CassetteMismatch):
        ReplayTransport({"exchanges": []})("GET", "https://x/y", {}, None)


def test_redact_keeps_structure():
    assert redact({"a": [{"number": "1"}, {"ok": 1}], "token": None}) == {"a": [{"number": "REDACTED"}, {"ok": 1}], "token": None}


def test_committed_cassettes_hold_no_secret_values():
    import pathlib
    for path in (pathlib.Path(__file__).parent / "fixtures").rglob("*.json"):
        data = json.loads(path.read_text())
        for exchange in data["exchanges"]:
            assert "Authorization" not in (exchange["request"].get("headers") or {}), path
            token = (exchange["response"].get("body") or {}).get("access_token")
            assert token in (None, "REDACTED"), path
