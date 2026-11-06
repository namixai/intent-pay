"""Claude as an outside agent: its two tools come over MCP from `python -m intent_pay.mcp`.

This is the third way in docs/judges.md, your own agent over MCP, with Claude as that agent. The
client connects to the MCP server as any outside agent does, over HTTP with the server's token,
and takes everything from it: the instructions (`initialize`), the tools (`tools/list`) and each
tool's answer (`tools/call`). It holds one secret, ANTHROPIC_API_KEY, read from its own
environment and nowhere else; it reads no key file. Unless --url names a server that is already
running, it starts the server as a separate process, without ANTHROPIC_API_KEY in that process's
environment, and the payment provider's keys go only to the server, through the server's --env.

    python -m intent_pay.claude_mcp --scenario scenarios/airwallex-demo.json --dry-run
    python -m intent_pay.claude_mcp --scenario scenarios/airwallex-demo.json --out DIR
    python -m intent_pay.claude_mcp --scenario scenarios/paypal-demo.json --sandbox --env FILE --out DIR
    python -m intent_pay.claude_mcp --url http://127.0.0.1:8765/mcp   # token in INTENT_PAY_MCP_TOKEN

Each task is one conversation, and each starts with the message the outside agent got on
2026-11-04. Tasks go on until reading the task page says they are all done. Before the first task
one small call checks the key. With --dry-run the client starts the server, reads its
instructions and tools, prints the first request and stops before any model call, so it needs no
key. Model calls are paid; the tests make none.

--out writes transcript.json: each model turn's text and tool calls, the server's answers, and
each call's token usage with its cost at the prices in PRICES. The server's ledger and journal
are copied next to it, and with --sandbox the server records the provider's answers, keys removed,
in DIR/cassettes. None of these files holds a key or the server's token.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any, Callable

from intent_pay import __version__
from intent_pay.agent import FALLBACK_BETA, MAX_TURNS, AgentError
from intent_pay.ledger import Ledger, LedgerError
from intent_pay.mcp import PROTOCOL_VERSIONS, TASKS_URL, TOKEN_ENV

MODEL = "claude-opus-5-5"
EFFORT = "medium"
TASK_MESSAGE = f"Read your next task at {TASKS_URL} with read_page and do it."
MAX_TASKS = 10
KEY_ENV = "ANTHROPIC_API_KEY"

# Claude API list prices in US dollars per million tokens. Cache writes are 5-minute writes, 1.25
# times the input price. A model not listed here gets no cost, and the transcript says so.
PRICES: dict[str, dict[str, Decimal]] = {
    "claude-opus-5-5": {"input": Decimal("4.00"), "cache_write": Decimal("5.00"),
                        "cache_read": Decimal("0.20"), "output": Decimal("20.00")},
}


class McpError(Exception):
    """The MCP server could not be reached or answered with an error."""


class McpClient:
    """MCP's Streamable HTTP transport from the client side, as the server speaks it: one JSON
    answer per POST, the token as a bearer header."""

    def __init__(self, url: str, token: str, timeout: float = 120):
        self.url, self.token, self.timeout = url, token, timeout
        self.version: str | None = None
        self.ids = itertools.count(1)

    def _post(self, body: dict[str, Any]) -> Any:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {self.token}"}
        if self.version:
            headers["MCP-Protocol-Version"] = self.version
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise McpError(f"the MCP server answered HTTP {exc.code} to {body.get('method')}") from None
        except OSError as exc:
            raise McpError(f"cannot reach the MCP server: {exc}") from None
        return json.loads(raw) if raw.strip() else None

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": next(self.ids), "method": method}
        if params is not None:
            body["params"] = params
        answer = self._post(body)
        if not isinstance(answer, dict) or not isinstance(answer.get("result"), dict):
            raise McpError(f"{method}: {answer.get('error') if isinstance(answer, dict) else answer}")
        return answer["result"]

    def initialize(self) -> dict[str, Any]:
        result = self.request("initialize", {"protocolVersion": PROTOCOL_VERSIONS[0], "capabilities": {},
                                             "clientInfo": {"name": "intent-pay claude_mcp", "version": __version__}})
        self.version = result.get("protocolVersion")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def tools(self) -> list[dict[str, Any]]:
        return self.request("tools/list")["tools"]

    def call(self, name: str, arguments: Any) -> tuple[str, bool]:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        return text, bool(result.get("isError"))


def usage_of(response: Any) -> dict[str, Any]:
    """One call's token usage and its cost. When a fallback model served the call, the top-level
    usage covers only that model's attempt, so no cost is given and the attempts are kept."""
    usage = response.usage
    row: dict[str, Any] = {"model": response.model}
    for field in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
        row[field] = getattr(usage, field, None) or 0
    iterations = getattr(usage, "iterations", None) or []
    row["fallback"] = any(getattr(i, "type", None) == "fallback_message" for i in iterations)
    if row["fallback"]:
        row["iterations"] = [i.model_dump() if hasattr(i, "model_dump") else dict(i) for i in iterations]
    row["cost_usd"] = cost(row)
    return row


def cost(row: dict[str, Any]) -> str | None:
    price = PRICES.get(row["model"])
    if price is None or row.get("fallback"):
        return None
    total = (row["input_tokens"] * price["input"] + row["cache_creation_input_tokens"] * price["cache_write"]
             + row["cache_read_input_tokens"] * price["cache_read"] + row["output_tokens"] * price["output"])
    return str((total / Decimal(1_000_000)).quantize(Decimal("0.000001")))


def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fields = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    out: dict[str, Any] = {"calls": len(rows), **{f: sum(r[f] for r in rows) for f in fields}}
    priced = [Decimal(r["cost_usd"]) for r in rows if r["cost_usd"] is not None]
    out["cost_usd"] = str(sum(priced, Decimal("0")))
    out["every_call_priced"] = len(priced) == len(rows)
    out["prices_usd_per_million"] = {m: {k: str(v) for k, v in p.items()} for m, p in PRICES.items()}
    return out


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _task_page_says(answer: Any, words: str) -> bool:
    return isinstance(answer, dict) and answer.get("url") == TASKS_URL and words in str(answer.get("text", ""))


class ClaudeOverMcp:
    """Claude in a plain tool-use loop. Each tool call goes to the MCP server and nowhere else."""

    def __init__(self, mcp: McpClient, client: Any, model: str = MODEL, effort: str = EFFORT,
                 say: Callable[[str], None] = print):
        self.mcp, self.client, self.model, self.effort, self.say = mcp, client, model, effort, say
        self.calls: list[dict[str, Any]] = []

    def key_check(self) -> dict[str, Any]:
        """One short message, to find out whether the key works before any task starts."""
        response = self.client.messages.create(model=self.model, max_tokens=16, output_config={"effort": "low"},
                                               messages=[{"role": "user", "content": "Reply with the word ready."}])
        row = usage_of(response)
        self.calls.append(row)
        self.say(f"key check: {response.model} answered, stop {response.stop_reason}")
        return {"stop_reason": response.stop_reason, "usage": row}

    def task(self, number: int, system: str, tools: list[dict[str, Any]],
             into: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """One conversation. Its record goes into `into` before the first call and grows with each
        turn, so an error part way keeps the turns that were already paid for."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": TASK_MESSAGE}]
        turns: list[dict[str, Any]] = []
        result: dict[str, Any] = {"conversation": number, "message": TASK_MESSAGE, "turns": turns,
                                  "opened": None, "all_done": False, "stopped": None}
        if into is not None:
            into.append(result)
        self.say(f"conversation {number}: {TASK_MESSAGE}")
        for _ in range(MAX_TURNS):
            response = self.client.beta.messages.create(
                model=self.model, max_tokens=16000, system=system, tools=tools, messages=messages,
                output_config={"effort": self.effort},
                # If a classifier declines the request, the API retries it on another model in the same call.
                betas=[FALLBACK_BETA], fallbacks="default")
            row = usage_of(response)
            self.calls.append(row)
            turn: dict[str, Any] = {"stop_reason": response.stop_reason, "usage": row, "text": [], "calls": []}
            turns.append(turn)
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    turn["text"].append(block.text.strip())
                    self.say(f"  says     {block.text.strip()}")
            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                result["stopped"] = f"the model declined ({getattr(details, 'category', None)})"
                return result
            if response.stop_reason != "tool_use":
                if response.stop_reason not in ("end_turn", "stop_sequence"):
                    result["stopped"] = f"the model stopped: {response.stop_reason}"
                return result
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                text, is_error = self.mcp.call(block.name, block.input)
                answer = _parse(text)
                turn["calls"].append({"tool": block.name, "input": block.input, "answer": answer, "is_error": is_error})
                self.say(f"  {block.name:<8} {json.dumps(block.input)}")
                self.say(f"  answer   {text[:300]}")
                if _task_page_says(answer, "tasks are done"):
                    result["all_done"] = True
                elif _task_page_says(answer, "Task ") and result["opened"] is None:
                    result["opened"] = answer["text"].split(".", 1)[0]
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": is_error})
            messages.append({"role": "user", "content": results})
        result["stopped"] = f"stopped after {MAX_TURNS} model turns"
        return result


def run_tasks(mcp: McpClient, client: Any, model: str = MODEL, effort: str = EFFORT, max_tasks: int = MAX_TASKS,
              say: Callable[[str], None] = print, dry_run: bool = False,
              record: dict[str, Any] | None = None) -> dict[str, Any]:
    """The run, written into `record` as it goes: if a call fails part way, the caller still
    holds every finished turn and the usage of every call made."""
    record = {} if record is None else record
    record.update(model=model, effort=effort)
    info = mcp.initialize()
    listed = mcp.tools()
    system = info.get("instructions") or ""
    tools = [{"name": t["name"], "description": t.get("description", ""), "input_schema": t["inputSchema"]}
             for t in listed]
    record.update(server=info.get("serverInfo"), protocol_version=info.get("protocolVersion"), system=system, tools=tools)
    say(f"server {info.get('serverInfo')}, protocol {info.get('protocolVersion')}")
    say(f"tools: {', '.join(t['name'] for t in tools)}")
    if dry_run:
        say(f"system prompt, from the server's instructions:\n{system}")
        say(f"first message: {TASK_MESSAGE}")
        say(f"dry run: stopped before the first call to {model}; no key was needed")
        record["dry_run"] = True
        return record
    agent = ClaudeOverMcp(mcp, client, model, effort, say)
    record["tasks"] = []
    try:
        record["key_check"] = agent.key_check()
        for number in range(1, max_tasks + 1):
            task = agent.task(number, system, tools, into=record["tasks"])
            if task["all_done"] or task["opened"] is None or task["stopped"]:
                break
    finally:
        record["usage"] = totals(agent.calls)
    return record


class ServerProcess:
    """`python -m intent_pay.mcp` in its own process, on a free port, with a new token and without
    ANTHROPIC_API_KEY in its environment. Its output is kept, and it never holds the token."""

    def __init__(self, scenario: str, sandbox: bool = False, env_file: str | None = None,
                 record: pathlib.Path | None = None, say: Callable[[str], None] = print):
        self.token = secrets.token_urlsafe(24)
        args = [sys.executable, "-m", "intent_pay.mcp", "--scenario", scenario, "--port", "0"]
        if sandbox:
            args.append("--sandbox")
        if env_file:
            args += ["--env", env_file]
        if record is not None:
            args += ["--record", str(record)]
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
        env[TOKEN_ENV] = self.token
        env["PYTHONUNBUFFERED"] = "1"
        # The package's own directory first, so the server is this code even when it is not installed.
        package_root = str(pathlib.Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join(p for p in (package_root, env.get("PYTHONPATH")) if p)
        self.say = say
        self.lines: list[str] = []
        self.process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", env=env)
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self._keep(line)
            if line.startswith("journal "):
                break
        else:
            self.process.wait()
            raise AgentError(f"the MCP server stopped at start, exit {self.process.returncode}; its output is above")
        found = re.search(r"endpoint http://127\.0\.0\.1:(\d+)/mcp/", "\n".join(self.lines))
        paths = dict(line.split(" ", 1) for line in self.lines if line.startswith(("ledger ", "journal ")))
        if found is None or "ledger" not in paths:
            self.stop()
            raise AgentError("the MCP server did not say where it listens:\n" + "\n".join(self.lines))
        self.url = f"http://127.0.0.1:{found.group(1)}/mcp"
        self.ledger, self.journal = pathlib.Path(paths["ledger"]), pathlib.Path(paths["journal"])
        self.reader = threading.Thread(target=self._read_rest, daemon=True)
        self.reader.start()

    def _keep(self, line: str) -> None:
        line = line.rstrip("\n")
        self.lines.append(line)
        self.say(f"server | {line}")

    def _read_rest(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self._keep(line)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if getattr(self, "reader", None) is not None:
            self.reader.join(timeout=5)


def write_record(out: pathlib.Path, record: dict[str, Any], server: ServerProcess | None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    if server is not None:
        record["server_output"] = server.lines
        for source, name in ((server.ledger, "ledger.jsonl"), (server.journal, "mcp-journal.jsonl")):
            if source.exists():
                shutil.copyfile(source, out / name)
    (out / "transcript.json").write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str) + "\n",
                                         encoding="utf-8")


def _client(key: str) -> Any:
    try:
        import anthropic
    except ImportError as exc:
        raise AgentError("the Claude client needs the Anthropic SDK: pip install -e '.[agent]'") from exc
    return anthropic.Anthropic(api_key=key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m intent_pay.claude_mcp", description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenario", default="scenarios/paypal-demo.json", help="the scenario the server runs")
    parser.add_argument("--sandbox", action="store_true", help="the server pays in the scenario's sandbox")
    parser.add_argument("--env", help="the server's file of KEY=VALUE lines; this client never reads it")
    parser.add_argument("--url", help="use an MCP server that is already running; its token in " + TOKEN_ENV)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--effort", default=EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--tasks", type=int, default=MAX_TASKS, help="at most this many conversations")
    parser.add_argument("--out", type=pathlib.Path, help="directory for transcript.json, the ledger and the journal")
    parser.add_argument("--dry-run", action="store_true", help="stop before the first model call; no key needed")
    args = parser.parse_args(argv)

    def fail(message: str) -> int:
        print(f"error: {message}", file=sys.stderr, flush=True)
        return 2

    key = (os.environ.get(KEY_ENV) or "").strip()
    if not key and not args.dry_run:
        return fail(f"{KEY_ENV} is empty. This client reads the key from its environment and from nowhere "
                    "else; set it there, or use --dry-run to stop before the first model call.")
    if args.url and (args.sandbox or args.env):
        return fail("--sandbox and --env go to a server this client starts; with --url the server is already running")
    if args.url and not os.environ.get(TOKEN_ENV):
        return fail(f"with --url, put the server's token in {TOKEN_ENV}")

    server: ServerProcess | None = None
    record: dict[str, Any] = {}
    status = 0
    try:
        client = None if args.dry_run else _client(key)
        if args.url:
            mcp = McpClient(args.url, os.environ[TOKEN_ENV])
        else:
            cassettes = args.out / "cassettes" if args.out is not None and args.sandbox else None
            server = ServerProcess(args.scenario, args.sandbox, args.env, cassettes)
            mcp = McpClient(server.url, server.token)
        run_tasks(mcp, client, args.model, args.effort, args.tasks, dry_run=args.dry_run, record=record)
    except (AgentError, McpError) as exc:
        status = fail(str(exc))
    except Exception as exc:  # an API error: its class and message, which hold no key
        if type(exc).__module__.split(".")[0] != "anthropic":
            raise
        status = fail(f"the Anthropic API: {type(exc).__name__}: {exc}")
    finally:
        if server is not None:
            server.stop()
            try:
                record["ledger"] = {"path": str(server.ledger), "records": Ledger(server.ledger).verify(), "chain": "ok"}
                print(f"ledger {server.ledger}: {record['ledger']['records']} records, chain ok")
            except (LedgerError, OSError) as exc:
                record["ledger"] = {"path": str(server.ledger), "error": str(exc)}
                print(f"ledger {server.ledger}: {exc}")
        if args.out is not None and record and not args.dry_run:
            write_record(args.out, record, server)
            print(f"record written to {args.out}")
    usage = record.get("usage")
    if usage:
        print(f"{usage['calls']} model calls: {usage['input_tokens']} input, {usage['output_tokens']} output, "
              f"{usage['cache_read_input_tokens']} cache read, {usage['cache_creation_input_tokens']} cache write "
              f"tokens; {usage['cost_usd']} USD" + ("" if usage["every_call_priced"] else " for the priced calls"))
    return status


if __name__ == "__main__":
    sys.exit(main())
