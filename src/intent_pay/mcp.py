"""The agent's two tools as a remote MCP server, for an agent that runs somewhere else.

An outside agent (a model in another company's app) adds this server as a remote MCP
connector and gets the two tools the agent in the scenario gets: `read_page` and
`propose_payment`. Nothing else: no payment-provider tool, no key, no policy. The gateway in
this process decides and pays, as it does under `python -m intent_pay.agent`.

The tasks come from the scenario. Reading TASKS_URL with `read_page` opens the next task and
returns its text. From then on the agent reads that task's page and its proposals go to the
gateway with that step's request ID and order, set by code; a second proposal in the same task
carries the same request ID. A proposal before the first task reaches no gateway.

    python -m intent_pay.mcp --scenario scenarios/paypal-demo.json            # recorded answers
    python -m intent_pay.mcp --scenario scenarios/paypal-demo.json --sandbox  # PayPal sandbox

The server speaks MCP's Streamable HTTP transport, answering each POST with JSON (it opens no
event stream), on 127.0.0.1 only. The endpoint is /mcp/<token>, or /mcp with the header
`Authorization: Bearer <token>`. The token is INTENT_PAY_MCP_TOKEN, or a new one printed at
start. Anything else gets 404, and a request that carries an Origin header not allowed with
--allow-origin gets 403. After initialize, an MCP-Protocol-Version header naming a version the
server does not speak gets 400. Every call and its answer go to a journal file; headers, the token and
keys never do.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import pathlib
import secrets
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping

from intent_pay import __version__
from intent_pay.agent import (CONFIG_ERRORS, AgentError, Scenario, StepTools, _line, fresh_run, sandbox_gateway,
                              system_for, tools_for)
from intent_pay.ledger import Ledger, LedgerError

TASKS_URL = "https://tasks.acme.example/next"
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MAX_BODY = 64 * 1024
TOKEN_ENV = "INTENT_PAY_MCP_TOKEN"
READ_PAGE = "Read a web page, such as your task page or a vendor page. Returns the page text."


def mcp_tools(provider: str) -> list[dict[str, Any]]:
    """The scenario agent's tools in MCP's shape: the same names and inputs."""
    tools = []
    for tool in tools_for(provider):
        description = READ_PAGE if tool["name"] == "read_page" else tool["description"]
        tools.append({"name": tool["name"], "description": description, "inputSchema": tool["input_schema"]})
    return tools


class Run:
    """One pass through a scenario by an outside agent: which task is open, and its tools."""

    def __init__(self, scenario: Scenario, ledger: Ledger, run: str, sandbox: bool = False,
                 record: pathlib.Path | None = None, gateway_for: Callable[..., Any] = sandbox_gateway,
                 show: Callable[[str], None] = lambda _line: None):
        for i, step in enumerate(scenario.steps):
            if step.get("vendor_charge"):
                raise AgentError(f"{scenario.path.name} step {i + 1} is not an agent task; this server runs agent tasks only")
        self.scenario, self.ledger, self.run = scenario, ledger, run
        self.sandbox, self.record, self.gateway_for, self.show = sandbox, record, gateway_for, show
        self.index = -1
        self.tools: StepTools | None = None

    def call(self, name: str, args: Any) -> tuple[dict[str, Any], bool]:
        if name == "read_page" and isinstance(args, dict) and args.get("url") == TASKS_URL:
            return self._next_task()
        if self.tools is None:
            return {"error": f"no task is open yet: read {TASKS_URL} first"}, True
        seen, is_error, shown = self.tools.call(name, args)
        for event in shown:
            self.show(_line(event, self.scenario.provider))
        return seen, is_error

    def _next_task(self) -> tuple[dict[str, Any], bool]:
        total = len(self.scenario.steps)
        if self.index + 1 >= total:
            return {"url": TASKS_URL, "text": f"All {total} tasks are done. There is nothing more to do."}, False
        nxt = self.index + 1
        step = self.scenario.step(nxt, self.run)
        record = None if self.record is None else self.record / f"step-{nxt + 1}.json"
        # The gateway first: if it cannot be made, the task stays unopened and the next read tries it again.
        gateway = self.gateway_for(self.scenario, step, self.ledger, self.sandbox, record=record)
        self.index, self.tools = nxt, StepTools(step, gateway, self.scenario.provider)
        self.show(f"Task {self.index + 1}: {step['title']}")
        return {"url": TASKS_URL, "text": f"Task {self.index + 1} of {total}. {step['task']}"}, False


def _result(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


class Server:
    """MCP's JSON-RPC methods for one run: initialize, ping, tools/list and tools/call. One
    message is handled at a time, so the open task and the ledger see one call after another."""

    def __init__(self, run: Run, journal: pathlib.Path, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.run = run
        self.journal = journal
        self.clock = clock
        self.tools = mcp_tools(run.scenario.provider)
        self.names = {t["name"] for t in self.tools}
        self.lock = threading.Lock()

    def handle(self, message: Any) -> dict[str, Any] | None:
        """The answer to one message, or None for a notification (it gets no answer)."""
        with self.lock:
            return self._handle(message)

    def _handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            answer = _error(message.get("id") if isinstance(message, dict) else None, -32600, "invalid request")
            self._write(message, answer)
            return answer
        if "id" not in message:
            self._write(message, None)
            return None
        mid, method = message["id"], message["method"]
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            answer = _result(mid, {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "intent-pay", "version": __version__},
                "instructions": f"{system_for(self.run.scenario.provider)}\n\nYour tasks are at {TASKS_URL}.",
            })
        elif method == "ping":
            answer = _result(mid, {})
        elif method == "tools/list":
            answer = _result(mid, {"tools": self.tools})
        elif method == "tools/call":
            name = params.get("name")
            if name not in self.names:
                answer = _error(mid, -32602, f"unknown tool {name!r}")
            else:
                seen, is_error = self.run.call(name, params.get("arguments", {}))
                answer = _result(mid, {"content": [{"type": "text", "text": json.dumps(seen)}], "isError": is_error})
        else:
            answer = _error(mid, -32601, f"method not found: {method}")
        self._write(message, answer)
        return answer

    def _write(self, message: Any, answer: dict[str, Any] | None) -> None:
        entry = {"at": self.clock().isoformat(timespec="seconds"), "request": message, "answer": answer}
        with self.journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def make_handler(server: Server, token: str, origins: frozenset[str] = frozenset()) -> type[BaseHTTPRequestHandler]:
    path_with_token = f"/mcp/{token}"
    bearer = f"Bearer {token}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "intent-pay-mcp"
        protocol_version = "HTTP/1.1"
        timeout = 60  # an idle connection is closed after a minute

        def log_message(self, format: str, *args: Any) -> None:
            # The default line holds the request path, and the path holds the token.
            pass

        def _authorized(self) -> bool:
            path = self.path.split("?", 1)[0]
            if hmac.compare_digest(path.encode(), path_with_token.encode()):
                return True
            sent = self.headers.get("Authorization") or ""
            return path == "/mcp" and hmac.compare_digest(sent.encode(), bearer.encode())

        def _gate(self) -> bool:
            if not self._authorized():
                self._send(404)
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in origins:
                self._send(403)
                return False
            return True

        def _send(self, status: int, body: dict[str, Any] | None = None, headers: Mapping[str, str] = {}) -> None:
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            if not self._gate():
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = 0
            if not 0 < length <= MAX_BODY:
                self._send(413 if length > MAX_BODY else 400, _error(None, -32600, "a body of 1 to 65536 bytes is needed"))
                return
            try:
                message = json.loads(self.rfile.read(length))
            except ValueError:
                self._send(400, _error(None, -32700, "parse error"))
                return
            if isinstance(message, list):
                self._send(400, _error(None, -32600, "batches are not supported"))
                return
            # After initialize the client names the version it agreed to; one we do not speak is refused.
            # An initialize itself is answered whatever its header says, since its body negotiates.
            version = self.headers.get("MCP-Protocol-Version")
            negotiating = isinstance(message, dict) and message.get("method") == "initialize"
            if version is not None and version not in PROTOCOL_VERSIONS and not negotiating:
                self._send(400, _error(message.get("id") if isinstance(message, dict) else None, -32600,
                                       f"unsupported MCP-Protocol-Version {version!r}"))
                return
            try:
                answer = server.handle(message)
            except Exception as exc:  # the caller gets an error, not a closed connection
                print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                self._send(500, _error(message.get("id") if isinstance(message, dict) else None, -32603, "internal error"))
                return
            if answer is None:
                self._send(202)
                return
            self._send(200, answer)

        def do_GET(self) -> None:
            if self._gate():
                self._send(405, None, {"Allow": "POST"})

        def do_DELETE(self) -> None:
            if self._gate():
                self._send(405, None, {"Allow": "POST"})

    return Handler


def main(argv: list[str] | None = None) -> int:
    from intent_pay.__main__ import load_env_file

    parser = argparse.ArgumentParser(prog="python -m intent_pay.mcp", description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenario", default="scenarios/paypal-demo.json")
    parser.add_argument("--sandbox", action="store_true", help="pay in the scenario's sandbox with keys from .env")
    parser.add_argument("--record", type=pathlib.Path,
                        help="with --sandbox: save each task's exchanges, keys removed, as DIR/step-N.json")
    parser.add_argument("--env", default=".env", help="file of KEY=VALUE lines to read first (default .env)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-origin", action="append", default=[], help="an Origin header to accept")
    parser.add_argument("--run", help="tag put into request IDs (default: the current UTC time)")
    args = parser.parse_args(argv)
    load_env_file(pathlib.Path(args.env))
    try:
        if args.record and not args.sandbox:
            raise AgentError("--record saves live sandbox answers, so it needs --sandbox")
        scenario = Scenario.load(args.scenario)
        tag, ledger = fresh_run(pathlib.Path("state") / "mcp")
        tag = args.run or tag
        run = Run(scenario, ledger, tag, sandbox=args.sandbox, record=args.record,
                  show=lambda text: print(text, flush=True))
        if args.sandbox:  # a missing key stops the server here, before an agent can connect
            sandbox_gateway(scenario, scenario.step(0, tag), ledger, True)
    except (AgentError, LedgerError, OSError, *CONFIG_ERRORS) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}, indent=2))
        return 2
    token = os.environ.get(TOKEN_ENV) or secrets.token_urlsafe(24)
    journal = ledger.path.with_suffix(".journal.jsonl")
    journal.parent.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port),
                       make_handler(Server(run, journal), token, frozenset(args.allow_origin)))
    where = f"{scenario.provider_name} sandbox" if args.sandbox else scenario.replay_label()
    print(f"intent-pay MCP server, run {tag}; payments: {where}")
    print(f"endpoint http://127.0.0.1:{httpd.server_port}/mcp/<token>"
          + ("" if os.environ.get(TOKEN_ENV) else f"; token {token}"))
    print(f"ledger {ledger.path}\njournal {journal}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    print(f"ledger {ledger.path}: {ledger.verify()} records, chain ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
