"""The MCP server gives an outside agent the scenario agent's two tools and nothing more. These
tests hold it to that: the tool list, tasks opened in order with their request IDs set by code,
the four outcomes of the PayPal demo, and an endpoint that answers only with the token."""

import http.client
import json
import pathlib
import socket
import threading

import pytest

from intent_pay.adapters import paypal
from intent_pay.adapters.http import ReplayTransport
from intent_pay.agent import AgentError, Scenario, sandbox_gateway
from intent_pay.gateway import Gateway
from intent_pay.ledger import Ledger
from intent_pay.mcp import PROTOCOL_VERSIONS, TASKS_URL, Run, Server, make_handler, mcp_gateway
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


def test_a_task_whose_gateway_cannot_be_made_stays_unopened(tmp_path):
    made = []
    def fails_once(scenario, step, ledger, sandbox, record=None):
        made.append(step["title"])
        if len(made) == 1:
            raise OSError("no recording")
        return keyed_gateway(scenario, step, ledger, sandbox, record)
    run = Run(Scenario.load(PAYPAL), Ledger(tmp_path / "l.jsonl"), "run1", gateway_for=fails_once)
    with pytest.raises(OSError):
        run.call("read_page", {"url": TASKS_URL})
    assert run.tools is None and run.index == -1
    task, is_error = run.call("read_page", {"url": TASKS_URL})
    assert not is_error and task["text"].startswith("Task 1 of 4.") and made == ["Monthly renewal"] * 2


AIRWALLEX = ROOT / "scenarios" / "airwallex-demo.json"


def test_the_server_runs_the_airwallex_step_without_an_agent_itself(tmp_path):
    shown = []
    run = Run(Scenario.load(AIRWALLEX), Ledger(tmp_path / "l.jsonl"), "run1", show=shown.append)
    server = Server(run, tmp_path / "journal.jsonl")
    task, _ = call(server, "read_page", {"url": TASKS_URL})
    assert task["text"].startswith("Task 1 of 3.")
    paid, _ = propose(server, "49.00")
    assert paid["decision"] == "allow" and paid["payment"]["ok"]

    task, _ = call(server, "read_page", {"url": TASKS_URL})
    assert task["text"].startswith("Task 2 of 3.")
    pushed, _ = propose(server, "470.00", "annual")
    assert pushed["reason"] == "per_payment_limit_exceeded"

    # Reading the next task runs step 3, the vendor's own charge on the step 1 card, and opens step 4.
    task, _ = call(server, "read_page", {"url": TASKS_URL})
    assert task["text"].startswith("Task 3 of 3.") and "Pay it again" in task["text"]
    assert "No agent" not in task["text"]
    vendor = "\n".join(shown)
    assert "Step 3 of the scenario, no agent" in vendor and "LIMIT_EXCEEDED" in vendor
    assert "receipt #3 per_payment_limit_exceeded" in vendor
    again, _ = propose(server, "49.00")
    assert again["reason"] == "duplicate_request_id"

    done, _ = call(server, "read_page", {"url": TASKS_URL})
    assert done["text"].startswith("All 3 tasks are done")
    assert run.ledger.verify() == 5  # decision, card, refusal, the vendor charge, duplicate


def test_the_vendor_step_runs_once_even_when_the_next_task_cannot_open(tmp_path):
    made = []
    def fails_on_step_4(scenario, step, ledger, sandbox, record=None):
        made.append(step["title"])
        if step["title"] == "The same request again" and made.count(step["title"]) == 1:
            raise OSError("no recording")
        return mcp_gateway(scenario, step, ledger, sandbox, record=record)
    run = Run(Scenario.load(AIRWALLEX), Ledger(tmp_path / "l.jsonl"), "run1", gateway_for=fails_on_step_4)
    run.call("read_page", {"url": TASKS_URL})
    run.call("propose_payment", {"amount": "49.00", "currency": "USD", "merchant_category": "7372",
                                 "purpose": "Acme Analytics", "billing": "monthly"})
    run.call("read_page", {"url": TASKS_URL})
    with pytest.raises(OSError):
        run.call("read_page", {"url": TASKS_URL})
    task, _ = run.call("read_page", {"url": TASKS_URL})
    assert task["text"].startswith("Task 3 of 3.")
    assert made.count("The vendor charges the card anyway") == 1


def test_a_scenario_with_no_step_for_an_agent_is_refused(tmp_path):
    data = json.loads(AIRWALLEX.read_text(encoding="utf-8"))
    data["steps"] = [s for s in data["steps"] if s.get("vendor_charge")]
    data["policy"] = str(AIRWALLEX.parent / data["policy"])
    path = tmp_path / "vendor-only.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AgentError, match="no step for an agent"):
        Run(Scenario.load(path), Ledger(tmp_path / "l.jsonl"), "r")


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


def test_an_unsupported_protocol_version_header_is_refused(endpoint):
    path = f"/mcp/{TOKEN}"
    assert post(endpoint, path, PING, {"MCP-Protocol-Version": "2025-06-18"})[0] == 200
    status, answer = post(endpoint, path, PING, {"MCP-Protocol-Version": "2099-01-01"})
    assert status == 400 and answer["error"]["code"] == -32600 and answer["id"] == 1
    init = {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}}
    status, answer = post(endpoint, path, init, {"MCP-Protocol-Version": "2099-01-01"})
    assert status == 200 and answer["result"]["protocolVersion"] == PROTOCOL_VERSIONS[0]


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


def raw(port, *requests):
    """Sends the requests on one connection, back to back, and returns all it gets until the server closes."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"".join(requests))
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    return data


def request(method, path, body=b"", headers=()):
    head = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1", f"Content-Length: {len(body)}", *headers]
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


PING_BODY = json.dumps(PING).encode()


@pytest.mark.parametrize("refused", [
    request("POST", "/mcp/wrong", PING_BODY),
    request("POST", f"/mcp/{TOKEN}", PING_BODY, ["Origin: https://evil.example"]),
    request("GET", f"/mcp/{TOKEN}", PING_BODY),
    request("DELETE", f"/mcp/{TOKEN}", PING_BODY),
], ids=["wrong path", "foreign origin", "get", "delete"])
def test_a_refusal_closes_the_connection_so_its_body_is_not_read_as_the_next_request(endpoint, refused):
    # Through a tunnel, the next request on a kept-alive connection may be another client's.
    data = raw(endpoint, refused, request("POST", f"/mcp/{TOKEN}", PING_BODY))
    assert data.count(b"HTTP/1.1 ") == 1 and b"Connection: close" in data
    assert b" 501 " not in data and b" 400 " not in data


def test_a_body_too_large_to_read_closes_the_connection(endpoint):
    conn = http.client.HTTPConnection("127.0.0.1", endpoint, timeout=5)
    conn.request("POST", f"/mcp/{TOKEN}", b"x" * (64 * 1024 + 1), {"Content-Type": "application/json"})
    response = conn.getresponse()
    response.read()
    conn.close()
    assert response.status == 413 and response.getheader("Connection") == "close"


def test_an_answered_request_keeps_the_connection_open(endpoint):
    with socket.create_connection(("127.0.0.1", endpoint), timeout=5) as sock:
        sock.sendall(request("POST", f"/mcp/{TOKEN}", PING_BODY) * 2)
        data = b""
        while data.count(b'"result"') < 2:
            data += sock.recv(65536)
    assert data.count(b"HTTP/1.1 200") == 2 and b"Connection: close" not in data


def test_refusals_are_logged_with_code_and_method_but_not_the_path(endpoint, capsys):
    post(endpoint, "/mcp/wrong-path", PING)
    post(endpoint, f"/mcp/{TOKEN}", PING, {"Origin": "https://evil.example"})
    post(endpoint, f"/mcp/{TOKEN}", [PING])
    err = capsys.readouterr().err
    assert "refused 404 POST" in err and "refused 403 POST origin 'https://evil.example'" in err
    assert "refused 400 POST" in err
    assert TOKEN not in err and "wrong-path" not in err and "/mcp" not in err


def test_recordings_name_the_program_that_made_them(tmp_path):
    scenario = Scenario.load(PAYPAL)
    env = {"PAYPAL_CLIENT_ID": "id", "PAYPAL_CLIENT_SECRET": SECRET, "PAYPAL_CARD_VAULT_ID": "vault"}
    def source(make):
        gateway = make(scenario, scenario.step(0, "run1"), Ledger(tmp_path / "l.jsonl"), True,
                       record=tmp_path / "c.json", env=env)
        return gateway.payer.client.transport.cassette["_source"]
    assert "by python -m intent_pay.mcp --record," in source(mcp_gateway)
    assert "by python -m intent_pay.agent --record," in source(sandbox_gateway)
    assert Run(scenario, Ledger(tmp_path / "l.jsonl"), "run1").gateway_for is mcp_gateway
