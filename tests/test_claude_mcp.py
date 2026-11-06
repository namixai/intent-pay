"""Claude as an outside agent over MCP, without calling Claude. A stand-in model makes the tool
calls written in the scenario; everything else is real: the HTTP transport, the MCP server, the
gateway on the recorded sandbox answers and the ledger. The tests hold the client to what it
claims: the key only from its environment, the tools only from the server, a record with token
usage and cost and with no key or token in it."""

import itertools
import json
import pathlib
import re
import threading
from decimal import Decimal
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from intent_pay import claude_mcp
from intent_pay.agent import Scenario
from intent_pay.claude_mcp import McpClient, ServerProcess, cost, run_tasks, totals, usage_of, write_record
from intent_pay.ledger import Ledger
from intent_pay.mcp import TASKS_URL, Run, Server, make_handler

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAYPAL = ROOT / "scenarios" / "paypal-demo.json"
AIRWALLEX = ROOT / "scenarios" / "airwallex-demo.json"
TOKEN = "t0ken-for-claude-tests"
FAKE_KEY = "not-a-real-key-for-tests"


def usage(inp=1000, out=100):
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0, iterations=None)


def response(content, stop_reason, model="claude-opus-5-5", use=None, **extra):
    return SimpleNamespace(content=content, stop_reason=stop_reason, model=model, usage=use or usage(), **extra)


def text(words):
    return SimpleNamespace(type="text", text=words)


class StandInModel:
    """Stands in for Claude: reads the task page, then makes the scenario script's tool calls for
    the task it opened, then stops. It answers the key check too, and keeps every request."""

    ids = itertools.count(1)

    def __init__(self, scenario_path, refuse_on=None):
        steps = json.loads(pathlib.Path(scenario_path).read_text(encoding="utf-8"))["steps"]
        self.scripts = [[i for i in s["script"] if "tool" in i] for s in steps if not s.get("vendor_charge")]
        self.queue = []
        self.requests = []
        self.refuse_on = refuse_on
        self.messages = SimpleNamespace(create=self._key_check)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _key_check(self, **request):
        self.requests.append(request)
        return response([text("ready")], "end_turn", use=usage(12, 3))

    def _tool(self, name, arguments):
        return response([text(f"Calling {name}."),
                         SimpleNamespace(type="tool_use", id=f"tu_{next(self.ids)}", name=name, input=arguments)],
                        "tool_use")

    def _create(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})  # as sent, before the loop adds to it
        last = request["messages"][-1]["content"]
        if isinstance(last, str):
            return self._tool("read_page", {"url": TASKS_URL})
        answer = json.loads(last[-1]["content"])
        if answer.get("url") == TASKS_URL:
            found = re.match(r"Task (\d+) of", answer["text"])
            if found is None:
                return response([text("Nothing left to do.")], "end_turn")
            number = int(found.group(1))
            if number == self.refuse_on:
                return response([], "refusal", stop_details=SimpleNamespace(category="cyber"))
            self.queue = list(self.scripts[number - 1])
        if self.queue:
            item = self.queue.pop(0)
            return self._tool(item["tool"], item["input"])
        return response([text("Done.")], "end_turn")


@pytest.fixture
def serve(tmp_path):
    servers = []

    def start(scenario_path):
        run = Run(Scenario.load(scenario_path), Ledger(tmp_path / "ledger.jsonl"), "run1")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Server(run, tmp_path / "journal.jsonl"), TOKEN))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return McpClient(f"http://127.0.0.1:{httpd.server_port}/mcp", TOKEN), run

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def answers(record, tool="propose_payment"):
    return [c["answer"] for t in record["tasks"] for turn in t["turns"] for c in turn["calls"] if c["tool"] == tool]


def test_claude_does_the_four_paypal_tasks_through_the_server(serve):
    mcp, run = serve(PAYPAL)
    model = StandInModel(PAYPAL)
    record = run_tasks(mcp, model, say=lambda _line: None)

    assert [t["opened"] for t in record["tasks"]] == ["Task 1 of 4", "Task 2 of 4", "Task 3 of 4", "Task 4 of 4", None]
    assert record["tasks"][-1]["all_done"]
    paid, pushed, again, declined = answers(record)
    assert (paid["decision"], paid["receipt"], paid["payment"]["status"]) == ("allow", 1, "COMPLETED")
    assert pushed["reason"] == "per_payment_limit_exceeded" and "payment" not in pushed
    assert again["reason"] == "duplicate_request_id"
    assert declined["decision"] == "allow" and declined["payment"]["status"] == "DECLINED"
    assert run.ledger.verify() == 6

    # The model got the server's instructions and the server's two tools, and nothing else.
    first = model.requests[1]
    assert first["system"].startswith("You are the purchasing agent") and TASKS_URL in first["system"]
    assert [t["name"] for t in first["tools"]] == ["read_page", "propose_payment"]
    assert first["messages"] == [{"role": "user", "content": claude_mcp.TASK_MESSAGE}]
    assert first["model"] == "claude-opus-5-5" and first["fallbacks"] == "default"


def test_claude_does_the_airwallex_tasks_and_the_server_runs_the_vendor_step(serve):
    mcp, run = serve(AIRWALLEX)
    record = run_tasks(mcp, StandInModel(AIRWALLEX), say=lambda _line: None)
    assert [t["opened"] for t in record["tasks"]] == ["Task 1 of 3", "Task 2 of 3", "Task 3 of 3", None]
    paid, pushed, again = answers(record)
    assert paid["decision"] == "allow" and paid["payment"]["ok"]
    assert pushed["reason"] == "per_payment_limit_exceeded"
    assert again["reason"] == "duplicate_request_id"
    assert run.ledger.verify() == 5


def test_usage_and_cost_add_up_at_the_listed_price(serve):
    mcp, _ = serve(PAYPAL)
    record = run_tasks(mcp, StandInModel(PAYPAL), say=lambda _line: None)
    calls = record["usage"]["calls"]
    turns = sum(len(t["turns"]) for t in record["tasks"])
    assert calls == turns + 1  # and the key check
    # 1,000 input and 100 output tokens a turn at 4.00 and 20.00 USD a million: 0.006 USD.
    # The key check, 12 and 3 tokens: 0.000108 USD.
    assert record["usage"]["cost_usd"] == str(Decimal("0.006") * turns + Decimal("0.000108"))
    assert record["usage"]["input_tokens"] == 1000 * turns + 12 and record["usage"]["every_call_priced"]


def test_a_call_served_by_a_fallback_model_is_not_priced():
    attempt = SimpleNamespace(type="fallback_message", model_dump=lambda: {"type": "fallback_message", "model": "x"})
    row = usage_of(response([], "end_turn", model="claude-opus-5",
                            use=SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0,
                                                cache_read_input_tokens=0, iterations=[attempt])))
    assert row["fallback"] and row["cost_usd"] is None and row["iterations"][0]["type"] == "fallback_message"
    summary = totals([row])
    assert summary["cost_usd"] == "0" and not summary["every_call_priced"]
    assert cost({"model": "claude-opus-5-5", "input_tokens": 1_000_000, "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": 1_000_000, "output_tokens": 1_000_000}) == "24.200000"


def test_a_refusal_stops_the_run_and_is_recorded(serve):
    mcp, _ = serve(PAYPAL)
    record = run_tasks(mcp, StandInModel(PAYPAL, refuse_on=2), say=lambda _line: None)
    assert len(record["tasks"]) == 2 and record["tasks"][-1]["stopped"] == "the model declined (cyber)"


def test_the_record_holds_no_key_and_no_token(serve, tmp_path):
    mcp, _ = serve(PAYPAL)
    record = run_tasks(mcp, StandInModel(PAYPAL), say=lambda _line: None)
    out = tmp_path / "out"
    write_record(out, record, None)
    written = (out / "transcript.json").read_text(encoding="utf-8")
    assert TOKEN not in written and FAKE_KEY not in written
    assert json.loads(written)["usage"]["calls"] == record["usage"]["calls"]


def test_an_empty_key_stops_the_client_before_a_server_starts(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(claude_mcp.subprocess, "Popen", lambda *a, **k: pytest.fail("a server was started"))
    assert claude_mcp.main(["--scenario", str(PAYPAL)]) == 2
    assert "ANTHROPIC_API_KEY is empty" in capsys.readouterr().err


def test_the_server_process_gets_no_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    seen = {}

    def popen(args, **kwargs):
        seen.update(kwargs["env"], args=args)
        raise RuntimeError("not started in this test")

    monkeypatch.setattr(claude_mcp.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError):
        ServerProcess(str(AIRWALLEX), sandbox=True, env_file="keys.env")
    assert "ANTHROPIC_API_KEY" not in seen and seen["INTENT_PAY_MCP_TOKEN"]
    assert seen["args"][-5:] == ["--port", "0", "--sandbox", "--env", "keys.env"]


def test_the_dry_run_starts_the_server_reads_its_tools_and_calls_no_model(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(claude_mcp, "_client", lambda key: pytest.fail("a model client was made"))
    assert claude_mcp.main(["--scenario", str(AIRWALLEX), "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "tools: read_page, propose_payment" in printed and "dry run: stopped before" in printed
    assert "0 records, chain ok" in printed


def test_a_whole_run_through_a_started_server_writes_the_record(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setattr(claude_mcp, "_client", lambda key: StandInModel(AIRWALLEX))
    out = tmp_path / "out"
    assert claude_mcp.main(["--scenario", str(AIRWALLEX), "--out", str(out)]) == 0
    record = json.loads((out / "transcript.json").read_text(encoding="utf-8"))
    assert record["ledger"]["records"] == 5 and record["tasks"][-1]["all_done"]
    server_output = "\n".join(record["server_output"])
    assert "Step 3 of the scenario, no agent" in server_output and "LIMIT_EXCEEDED" in server_output
    assert Ledger(out / "ledger.jsonl").verify() == 5 and (out / "mcp-journal.jsonl").exists()
    for path in out.iterdir():
        assert FAKE_KEY not in path.read_text(encoding="utf-8")
    assert "USD" in capsys.readouterr().out
