"""The demo page's server, on recorded sandbox answers, on a free local port."""

import http.client
import json
import pathlib
import threading
from http.server import HTTPServer

import pytest

from intent_pay.agent import Scenario, ScriptedAgent
from intent_pay.web import Demo, handler_for

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def server(tmp_path):
    demo = Demo(Scenario.load(ROOT / "scenarios" / "paypal-demo.json"), ScriptedAgent(), False, tmp_path)
    httpd = HTTPServer(("127.0.0.1", 0), handler_for(demo))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1], demo
    httpd.shutdown()
    httpd.server_close()


def request(port, method, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body, resp.getheader("Content-Type")


RUN = {"X-Intent-Pay": "1"}


def test_state_lists_the_steps(server):
    port, _ = server
    status, body, kind = request(port, "GET", "/api/state")
    state = json.loads(body)
    assert status == 200 and kind.startswith("application/json")
    assert [s["title"] for s in state["steps"]][0] == "Monthly renewal"
    assert all(s["events"] is None for s in state["steps"]) and state["ledger"]["records"] == []


def test_running_a_step_pays_and_records(server):
    port, _ = server
    status, body, _ = request(port, "POST", "/api/steps/1", RUN)
    state = json.loads(body)
    assert status == 200 and state["steps"][0]["events"]
    assert [r["kind"] for r in state["ledger"]["records"]] == ["decision", "execution"]
    assert state["ledger"]["chain"] == {"ok": True, "records": 2}


def test_steps_run_only_with_the_page_header(server):
    port, demo = server
    assert request(port, "POST", "/api/steps/1")[0] == 403
    assert request(port, "POST", "/api/reset")[0] == 403
    assert demo.ledger.records() == []


def test_another_host_name_is_refused(server):
    port, _ = server
    assert request(port, "GET", "/api/state", {"Host": "evil.example"})[0] == 403
    assert request(port, "POST", "/api/steps/1", {**RUN, "Host": f"evil.example:{port}"})[0] == 403


def test_only_the_page_files_are_served(server):
    port, _ = server
    assert request(port, "GET", "/")[0] == 200
    assert request(port, "GET", "/app.js")[2].startswith("text/javascript")
    assert request(port, "GET", "/../pyproject.toml")[0] == 404
    assert request(port, "GET", "/state/")[0] == 404
    assert request(port, "POST", "/api/steps/9", RUN)[0] == 404


def test_reset_starts_a_new_ledger_and_run(server):
    port, demo = server
    request(port, "POST", "/api/steps/1", RUN)
    first_run, first_path = demo.run, demo.ledger.path
    state = json.loads(request(port, "POST", "/api/reset", RUN)[1])
    assert state["run"] != first_run and state["ledger"]["records"] == []
    assert demo.ledger.path != first_path and first_path.exists()


def test_airwallex_demo_names_its_provider_and_shows_the_vendor_step(tmp_path):
    demo = Demo(Scenario.load(ROOT / "scenarios" / "airwallex-demo.json"), ScriptedAgent(), False, tmp_path)
    for number in range(1, 5):
        demo.run_step(number)
    view = demo.view()
    assert view["provider"] == "Airwallex" and view["mode"]["payments"] == "recorded Airwallex sandbox answers"
    charge = [e for e in view["steps"][2]["events"] if e["kind"] == "vendor_charge"][0]
    assert charge["compare"]["match"] is True
    assert [r.get("operation") for r in view["ledger"]["records"] if r["kind"] == "execution"] == ["pay", "forced_charge"]
