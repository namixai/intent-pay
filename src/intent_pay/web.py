"""A local page for the demo: one button per step, and what the agent, the policy and PayPal
each did.

    python -m intent_pay.web                # http://127.0.0.1:8765, recorded sandbox answers
    python -m intent_pay.web --sandbox      # PayPal sandbox, keys from .env

It listens on 127.0.0.1 only. A step runs on POST with an `X-Intent-Pay` header, which a page
from another site cannot send here without a CORS answer this server never gives, and a request
whose Host is not this machine is refused, so another site cannot make the demo pay.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from intent_pay.adapters.paypal import ConfigError
from intent_pay.agent import AgentError, Scenario, fresh_run, make_agent, run_step
from intent_pay.ledger import LedgerError

WEB = pathlib.Path(__file__).resolve().parents[2] / "web"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
STEP = re.compile(r"/api/steps/([0-9]{1,2})")


class Demo:
    """One run of the scenario: a fresh ledger, a run tag, and what each step showed."""

    def __init__(self, scenario: Scenario, agent: Any, sandbox: bool, state_dir: pathlib.Path):
        self.scenario, self.agent, self.sandbox, self.state_dir = scenario, agent, sandbox, state_dir
        self.reset()

    def reset(self) -> None:
        self.run, self.ledger = fresh_run(self.state_dir, prefix="web-")
        self.results: dict[int, list[dict[str, Any]]] = {}

    def run_step(self, number: int) -> None:
        index = number - 1
        self.results[index] = run_step(self.scenario, index, self.agent, self.ledger, self.run, sandbox=self.sandbox)

    def view(self) -> dict[str, Any]:
        try:
            records = self.ledger.records()
            chain = {"ok": True, "records": self.ledger.verify()}
        except LedgerError as exc:
            records, chain = [], {"ok": False, "error": str(exc)}
        steps = []
        for i in range(len(self.scenario.steps)):
            step = self.scenario.step(i, self.run)
            steps.append({"number": i + 1, "title": step["title"], "task": step["task"],
                          "events": self.results.get(i)})
        return {"scenario": self.scenario.name, "run": self.run,
                "mode": {"agent": self.agent.label,
                         "payments": "PayPal sandbox" if self.sandbox else "recorded PayPal sandbox answers"},
                "steps": steps, "ledger": {"path": str(self.ledger.path), "records": records, "chain": chain}}


def handler_for(demo: Demo) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "intent-pay"

        def log_message(self, fmt: str, *args: Any) -> None:  # one short line per request
            sys.stderr.write(f"{self.command} {self.path} {args[1] if len(args) > 1 else ''}\n")

        def _local(self) -> bool:
            host = self.headers.get("Host") or ""
            host = host[:host.find("]") + 1] if host.startswith("[") else host.split(":", 1)[0]
            return host in LOCAL_HOSTS

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            if not self._local():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "this demo answers on 127.0.0.1 only"})
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                return self._json(HTTPStatus.OK, demo.view())
            if path in STATIC:
                name, content_type = STATIC[path]
                return self._send(HTTPStatus.OK, (WEB / name).read_bytes(), content_type)
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._local() or self.headers.get("X-Intent-Pay") != "1":
                return self._json(HTTPStatus.FORBIDDEN, {"error": "steps run only from the demo page"})
            path = self.path.split("?", 1)[0]
            try:
                if path == "/api/reset":
                    demo.reset()
                elif match := STEP.fullmatch(path):
                    number = int(match.group(1))
                    if not 1 <= number <= len(demo.scenario.steps):
                        return self._json(HTTPStatus.NOT_FOUND, {"error": f"no step {number}"})
                    demo.run_step(number)
                else:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except (AgentError, ConfigError) as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            self._json(HTTPStatus.OK, demo.view())

    return Handler


def main(argv: list[str] | None = None) -> int:
    from intent_pay.__main__ import load_env_file

    parser = argparse.ArgumentParser(prog="python -m intent_pay.web", description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenario", default="scenarios/paypal-demo.json")
    parser.add_argument("--sandbox", action="store_true", help="pay in the PayPal sandbox with keys from .env")
    parser.add_argument("--env", default=".env", help="file of KEY=VALUE lines to read first (default .env)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state", default="state/runs", help="where each run's ledger goes (default state/runs)")
    args = parser.parse_args(argv)
    load_env_file(pathlib.Path(args.env))
    try:
        demo = Demo(Scenario.load(args.scenario), make_agent(), args.sandbox, pathlib.Path(args.state))
    except AgentError as exc:
        print(json.dumps({"error": "AgentError", "detail": str(exc)}, indent=2))
        return 2
    server = HTTPServer(("127.0.0.1", args.port), handler_for(demo))
    print(f"intent-pay demo on http://127.0.0.1:{args.port} ({demo.agent.label}; "
          f"{'PayPal sandbox' if args.sandbox else 'recorded sandbox answers'}). Ctrl+C stops it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
