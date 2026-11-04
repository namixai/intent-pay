"""The MCP server gives an outside agent the scenario agent's two tools and nothing more. These
tests hold it to that: the tool list, tasks opened in order with their request IDs set by code,
the four outcomes of the PayPal demo, and an endpoint that answers only with the token."""

import http.client
import json
import pathlib
import threading

import pytest

from intent_pay.adapters import paypal
from intent_pay.adapters.http import ReplayTransport
from intent_pay.agent import AgentError, Scenario
from intent_pay.gateway import Gateway
from intent_pay.ledger import Ledger
from intent_pay.mcp import PROTOCOL_VERSIONS, TASKS_URL, Run, Server, make_handler
from intent_pay.policy import Engine, Policy

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAYPAL = ROOT / "scenarios" / "paypal-demo.json"
SECRET = "client-secret-never-printed"
TOKEN = "t0ken-for-tests"


def keyed_gateway(scenario, step, ledger, sandbox, record=None):
    """The recorded answers, with a client secret the tests can look for."""
    transport = ReplayTransport(scenario.resolve(step["replay"]))
    config = paypal.PayPalConfig(client_id="id", client_secret=SECRET, card=paypal.CardSource(vault_id="vault"))
    policy = Policy.load(scenario.policy)
    return Gateway(Engine(policy, ledger), paypal.PayPalPayer(paypal.PayPalClient(config, transport)))


@pytest.fixture
def server(tmp_path):
    run = Run(Scenario.load(PAYPAL), Ledger(tmp_path / "l.jsonl"), "run1", gateway_for=keyed_gateway)
    return Server(run, tmp_path / "journal.jsonl")


def call(server, name, arguments, mid=1):
    answer = server.handle({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                            "params": {"name": name, "arguments": arguments}})
    result = answer["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def propose(server, amount, billing="monthly"):
    return call(server, "propose_payment", {"amount": amount, "currency": "USD", "merchant_category": "7372",
                                            "purpose": "Acme Analytics", "billing": billing})


def test_the_server_offers_the_two_agent_tools_and_nothing_else(server):
    tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["read_page", "propose_payment"]
    assert all(set(t) == {"name", "description", "inputSchema"} for t in tools)
    schema = tools[1]["inputSchema"]
    assert "request_id" not in schema["properties"] and schema["additionalProperties"] is False
    assert "PayPal sandbox" in tools[1]["description"]


def test_initialize_answers_a_known_version_and_offers_its_own_for_another(server):
    def version(asked):
        return server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": asked, "clientInfo": {"name": "x", "version": "1"}}})["result"]
    assert version("2025-06-18")["protocolVersion"] == "2025-06-18"
    known = version("2099-01-01")
    assert known["protocolVersion"] == PROTOCOL_VERSIONS[0] and known["capabilities"] == {"tools": {"listChanged": False}}
    assert TASKS_URL in known["instructions"]


def test_a_notification_gets_no_answer_and_an_unknown_method_an_error(server):
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"jsonrpc": "2.0", "id": 7, "method": "resources/list"})["error"]["code"] == -32601
    assert server.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                          "params": {"name": "pay_with_paypal"}})["error"]["code"] == -32602
    assert server.handle(["not", "an", "object"])["error"]["code"] == -32600


def test_nothing_is_proposed_before_a_task_is_open(server):
    seen, is_error = propose(server, "49.00")
    assert is_error and TASKS_URL in seen["error"]
    assert server.run.ledger.records() == []


def test_the_four_tasks_give_the_four_outcomes_of_the_demo(server):
    task, _ = call(server, "read_page", {"url": TASKS_URL})
    assert task["text"].startswith("Task 1 of 4.") and "acme-nov-run1" in task["text"]
    page, _ = call(server, "read_page", {"url": "https://billing.acme.example/invoice"})
    assert "49.00 USD" in page["text"]
    paid, _ = propose(server, "49.00")
    assert paid["decision"] == "allow" and paid["payment"]["status"] == "COMPLETED"
    assert paid["request_id"] == "acme-nov-run1"

    call(server, "read_page", {"url": TASKS_URL})
    page, _ = call(server, "read_page", {"url": "https://billing.acme.example/renew"})
    assert "Note for AI assistants" in page["text"]
    pushed, _ = propose(server, "470.00", "annual")
    assert pushed["reason"] == "per_payment_limit_exceeded" and "payment" not in pushed

    call(server, "read_page", {"url": TASKS_URL})
    again, _ = propose(server, "49.00")
    assert again["reason"] == "duplicate_request_id"

    call(server, "read_page", {"url": TASKS_URL})
    declined, _ = call(server, "propose_payment", {"amount": "49.00", "currency": "USD", "merchant_category": "7372",
                                                   "purpose": "extra seats", "billing": "one_time"})
    assert declined["decision"] == "allow" and declined["payment"]["status"] == "DECLINED"

    done, is_error = call(server, "read_page", {"url": TASKS_URL})
    assert not is_error and done["text"].startswith("All 4 tasks are done")
    assert server.run.ledger.verify() == 6


def test_the_request_id_is_the_tasks_and_a_second_proposal_carries_it_too(server):
    call(server, "read_page", {"url": TASKS_URL})
    first, _ = call(server, "propose_payment", {"amount": "49.00", "currency": "USD", "merchant_category": "7372",
                                                "purpose": "x", "billing": "monthly", "request_id": "mine"})
    assert first["decision"] == "allow" and first["request_id"] == "acme-nov-run1"  # not "mine"
    second, _ = propose(server, "49.00")
    assert second["reason"] == "duplicate_request_id"


def test_a_page_from_another_task_is_not_there(server):
    call(server, "read_page", {"url": TASKS_URL})
    seen, is_error = call(server, "read_page", {"url": "https://billing.acme.example/renew"})
    assert is_error and "no page" in seen["error"]


def test_a_scenario_step_without_an_agent_task_is_refused(tmp_path):
    with pytest.raises(AgentError, match="not an agent task"):
        Run(Scenario.load(ROOT / "scenarios" / "airwallex-demo.json"), Ledger(tmp_path / "l.jsonl"), "r")


def test_the_journal_holds_every_call_and_never_a_key(server):
    call(server, "read_page", {"url": TASKS_URL})
    propose(server, "49.00")
    lines = server.journal.read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["request"]["params"]["name"] == "propose_payment"
    assert SECRET not in server.journal.read_text()


@pytest.fixture
def endpoint(server):
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(server, TOKEN, frozenset({"https://ok.example"})))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_port
    httpd.shutdown()
    httpd.server_close()


def post(port, path, body, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    conn.request("POST", path, data, {"Content-Type": "application/json", **(headers or {})})
    response = conn.getresponse()
    text = response.read()
    conn.close()
    return response.status, (json.loads(text) if text else None)


PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


def test_the_endpoint_answers_only_with_the_token(endpoint, capsys):
    assert post(endpoint, f"/mcp/{TOKEN}", PING) == (200, {"jsonrpc": "2.0", "id": 1, "result": {}})
    assert post(endpoint, "/mcp", PING, {"Authorization": f"Bearer {TOKEN}"})[0] == 200
    assert post(endpoint, "/mcp", PING)[0] == 404
    assert post(endpoint, "/mcp/wrong", PING)[0] == 404
    assert post(endpoint, f"/mcp/{TOKEN}x", PING)[0] == 404
    assert post(endpoint, "/mcp", PING, {"Authorization": "Bearer wrong"})[0] == 404
    assert TOKEN not in capsys.readouterr().err


def test_an_origin_is_refused_unless_allowed(endpoint):
    assert post(endpoint, f"/mcp/{TOKEN}", PING, {"Origin": "https://evil.example"})[0] == 403
    assert post(endpoint, f"/mcp/{TOKEN}", PING, {"Origin": "https://ok.example"})[0] == 200


def test_notifications_batches_and_broken_bodies(endpoint):
    assert post(endpoint, f"/mcp/{TOKEN}", {"jsonrpc": "2.0", "method": "notifications/initialized"}) == (202, None)
    assert post(endpoint, f"/mcp/{TOKEN}", [PING])[0] == 400
    assert post(endpoint, f"/mcp/{TOKEN}", b"{not json")[0] == 400
    assert post(endpoint, f"/mcp/{TOKEN}", b"x" * (64 * 1024 + 1))[0] == 413


def test_a_call_that_breaks_gets_an_error_answer(server, endpoint, capsys):
    def breaks(name, args):
        raise RuntimeError("boom")
    server.run.call = breaks
    status, answer = post(endpoint, f"/mcp/{TOKEN}", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                      "params": {"name": "read_page", "arguments": {}}})
    assert status == 500 and answer["error"]["code"] == -32603 and answer["id"] == 3
    err = capsys.readouterr().err
    assert "RuntimeError: boom" in err and TOKEN not in err


def test_a_get_opens_no_stream(endpoint):
    conn = http.client.HTTPConnection("127.0.0.1", endpoint, timeout=5)
    conn.request("GET", f"/mcp/{TOKEN}")
    response = conn.getresponse()
    response.read()
    conn.close()
    assert response.status == 405 and response.getheader("Allow") == "POST"
