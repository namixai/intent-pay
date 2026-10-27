"""The HTTP layer under the adapters: one live transport, one that records, one that replays.

Adapters take a `Transport`, so the same adapter code runs against the live sandbox, against
the sandbox while saving every exchange to a cassette file, and against a cassette with no
network at all (tests, and the offline demo).

A cassette never holds a secret. Before an exchange is written, `redact` drops the
Authorization header and blanks every field named in `SECRET_FIELDS` (client secret, access
token, card number, expiry, security code, vault ID), wherever it sits in a JSON body.
"""

from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

REDACTED = "REDACTED"
SECRET_FIELDS = frozenset({
    "access_token", "refresh_token", "client_secret", "token",
    "number", "expiry", "security_code", "cvv", "vault_id",
})
KEPT_HEADERS = frozenset({"content-type", "prefer", "paypal-request-id", "x-client-id"})


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: Mapping[str, str]

    def json(self) -> Any:
        if not self.body:
            return {}
        return json.loads(self.body.decode("utf-8"))


class Transport(Protocol):
    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> Response: ...


class UrllibTransport:
    """The live transport. Standard library only, TLS verified by default. HTTP errors come back
    as responses with their status, so the adapter reads the provider's error body."""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> Response:
        if urlsplit(url).scheme != "https":
            raise ValueError("sandbox calls go over https only")
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return Response(resp.status, resp.read(), dict(resp.headers))
        except urllib.error.HTTPError as err:
            return Response(err.code, err.read(), dict(err.headers or {}))


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED if k in SECRET_FIELDS and v not in (None, "") else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _body_for_cassette(body: bytes | None) -> Any:
    if not body:
        return None
    try:
        return redact(json.loads(body.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Form bodies (the OAuth grant) carry no secret of their own; keep only the field names.
        text = body.decode("utf-8", "replace")
        return {"form": sorted(part.split("=", 1)[0] for part in text.split("&") if part)}


def exchange_record(method: str, url: str, headers: Mapping[str, str], body: bytes | None, response: Response) -> dict[str, Any]:
    parts = urlsplit(url)
    return {
        "request": {
            "method": method,
            "path": parts.path + (f"?{parts.query}" if parts.query else ""),
            "headers": {k: v for k, v in headers.items() if k.lower() in KEPT_HEADERS},
            "body": _body_for_cassette(body),
        },
        "response": {"status": response.status, "body": _body_for_cassette(response.body)},
    }


class RecordingTransport:
    """Passes every call to `inner` and appends the redacted exchange to `path` (a JSON list)."""

    def __init__(self, inner: Transport, path: str | pathlib.Path, source: str):
        self.inner = inner
        self.path = pathlib.Path(path)
        self.cassette = {"_source": source, "exchanges": []}

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> Response:
        response = self.inner(method, url, headers, body)
        self.cassette["exchanges"].append(exchange_record(method, url, headers, body, response))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.cassette, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return response


class CassetteMismatch(AssertionError):
    """The code asked for a call the cassette does not have next."""


class ReplayTransport:
    """Answers calls from a cassette, in order. A call that does not match the next exchange (method
    and path) raises, so a test notices the adapter doing something the recording never saw."""

    def __init__(self, cassette: Mapping[str, Any] | str | pathlib.Path):
        if not isinstance(cassette, Mapping):
            cassette = json.loads(pathlib.Path(cassette).read_text(encoding="utf-8"))
        self.exchanges = list(cassette["exchanges"])
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> Response:
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if not self.exchanges:
            raise CassetteMismatch(f"no recorded exchange left for {method} {url}")
        nxt = self.exchanges.pop(0)
        path = urlsplit(url).path
        want = nxt["request"]
        if want["method"] != method or want["path"].split("?")[0] != path:
            raise CassetteMismatch(f"expected {want['method']} {want['path']}, got {method} {path}")
        resp = nxt["response"]
        body = b"" if resp.get("body") is None else json.dumps(resp["body"]).encode("utf-8")
        return Response(resp["status"], body, {"content-type": "application/json"})

    @property
    def finished(self) -> bool:
        return not self.exchanges
