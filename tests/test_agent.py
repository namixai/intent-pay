"""The agent side. The scripted agent runs the demo scenario on recorded sandbox answers; Claude
is replaced by a stand-in client, so no test makes a paid model call."""

import json
import os
import pathlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

from intent_pay.adapters.http import ReplayTransport
from intent_pay.adapters.paypal import CardSource, PayPalClient, PayPalConfig, PayPalPayer
from intent_pay.agent import (DEFAULT_MODEL, FALLBACK_BETA, MAX_TURNS, AgentError, ClaudeAgent, Scenario, ScriptedAgent,
                              StepTools, fresh_run, make_agent, run_step, sandbox_gateway, step_request_id, system_for,
                              tools_for)
from intent_pay.gateway import Gateway
from intent_pay.intent import Intent
from intent_pay.ledger import Ledger
from intent_pay.policy import Engine, Policy

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "scenarios" / "paypal-demo.json"
SECRET = "client-secret-never-printed"
CARD_NUMBER = "4012888888881881"
CLEAN_ENV = {**{k: v for k, v in os.environ.items()
                if not k.startswith(("PAYPAL_", "AIRWALLEX_", "INTENT_PAY_", "ANTHROPIC_"))},
             "PYTHONPATH": str(ROOT / "src")}


@pytest.fixture
def scenario():
    return Scenario.load(SCENARIO)


def watched_gateway(transports):
    """A gateway on the step's cassette with real-looking keys, keeping each transport."""
    def build(scenario, step, ledger, sandbox, record=None):
        transport = ReplayTransport(scenario.resolve(step["replay"]))
        transports.append(transport)
        config = PayPalConfig(client_id="id", client_secret=SECRET,
                              card=CardSource(number=CARD_NUMBER, expiry="2030-12", security_code="123"))
        return Gateway(Engine(Policy.load(scenario.policy), ledger), PayPalPayer(PayPalClient(config, transport)))
    return build


def decisions(events):
    return [e["agent_sees"] for e in events if e["kind"] == "decision"]


def test_scripted_demo_runs_the_four_steps(scenario, tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl")
    seen = [decisions(run_step(scenario, i, ScriptedAgent(), ledger, "t"))[0] for i in range(4)]
    assert [s["reason"] for s in seen] == ["ok", "per_payment_limit_exceeded", "duplicate_request_id", "ok"]
    assert [(s.get("payment") or {}).get("status") for s in seen] == ["COMPLETED", None, None, "DECLINED"]
    assert ledger.verify() == 6


def test_refused_steps_make_no_paypal_call(scenario, tmp_path):
    ledger, transports = Ledger(tmp_path / "l.jsonl"), []
    for i in range(3):
        run_step(scenario, i, ScriptedAgent(), ledger, "t", gateway_for=watched_gateway(transports))
    assert len(transports[0].calls) == 3  # token, create, capture
    assert transports[1].calls == [] and transports[2].calls == []


def test_events_carry_no_key_or_card(scenario, tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl")
    events = run_step(scenario, 0, ScriptedAgent(), ledger, "t", gateway_for=watched_gateway([]))
    text = json.dumps(events)
    assert SECRET not in text and CARD_NUMBER not in text and "Bearer" not in text


def test_run_tag_goes_into_every_request_id(scenario):
    step = scenario.step(0, "x1")
    assert "acme-nov-x1" in step["task"]
    assert step["request_id"] == "acme-nov-x1"
    assert all("request_id" not in i.get("input", {}) for i in step["script"])
    assert "{run}" not in json.dumps(scenario.step(3, "x1"))


def test_scripted_agent_reports_what_came_back(scenario, tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl")
    last = [run_step(scenario, i, ScriptedAgent(), ledger, "t")[-1]["text"] for i in range(4)]
    assert last[0].startswith("Paid.") and "5S461376DW338105P" in last[0]
    assert "per_payment_limit_exceeded" in last[1] and "Nothing was sent to PayPal" in last[1]
    assert "duplicate_request_id" in last[2]
    assert "DECLINED" in last[3] and "Paid" not in last[3]


def test_every_scripted_proposal_is_a_valid_intent_and_every_replay_exists(scenario):
    for i in range(len(scenario.steps)):
        step = scenario.step(i, "t")
        assert scenario.resolve(step["replay"]).is_file()
        for item in step["script"]:
            if item.get("tool") == "propose_payment":
                Intent.parse({**item["input"], "request_id": step["request_id"]})
            if item.get("tool") == "read_page":
                assert item["input"]["url"] == step["page"]["url"]


def test_the_agent_cannot_pick_the_request_id(scenario, tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl")
    step = scenario.step(0, "t")
    tools = StepTools(step, sandbox_gateway(scenario, step, ledger, False))
    proposal = next(i["input"] for i in step["script"] if i.get("tool") == "propose_payment")
    seen, is_error, events = tools.call("propose_payment", {**proposal, "request_id": "attacker-chosen"})
    assert not is_error and seen["decision"] == "allow"
    assert events[0]["input"]["request_id"] == "acme-nov-t"
    assert [r["request_id"] for r in ledger.records() if r["kind"] == "decision"] == ["acme-nov-t"]


def test_a_step_without_a_request_id_proposes_nothing(scenario, tmp_path):
    step = {k: v for k, v in scenario.step(0, "t").items() if k != "request_id"}
    tools = StepTools(step, sandbox_gateway(scenario, scenario.step(0, "t"), Ledger(tmp_path / "l.jsonl"), False))
    seen, is_error, events = tools.call("propose_payment", {"amount": "49.00"})
    assert is_error and "no request ID" in seen["error"] and events == []


def scenario_with(tmp_path, request_id):
    """The demo scenario with the first step's request ID replaced."""
    data = json.loads(SCENARIO.read_text(encoding="utf-8"))
    data["steps"][0]["request_id"] = request_id
    path = tmp_path / SCENARIO.name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("request_id", [123, None, "", "   "])
def test_a_request_id_that_is_not_text_stops_the_scenario_load(tmp_path, request_id):
    with pytest.raises(AgentError, match="needs its request_id"):
        Scenario.load(scenario_with(tmp_path, request_id))


def test_a_request_id_with_spaces_is_loaded_trimmed(tmp_path):
    assert Scenario.load(scenario_with(tmp_path, "  acme-nov-{run} ")).step(0, "t")["request_id"] == "acme-nov-t"


AIRWALLEX_SCENARIO = ROOT / "scenarios" / "airwallex-demo.json"


def vendor_charge_with(tmp_path, **refs):
    """The Airwallex scenario with its vendor charge's references replaced."""
    data = json.loads(AIRWALLEX_SCENARIO.read_text(encoding="utf-8"))
    step = next(s for s in data["steps"] if "vendor_charge" in s)
    step["vendor_charge"].update(refs)
    path = tmp_path / AIRWALLEX_SCENARIO.name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_vendor_charge_references_are_loaded_trimmed(tmp_path):
    loaded = Scenario.load(vendor_charge_with(tmp_path, card_of=" acme-nov-{run} ", refused_as="acme-dec-{run}  "))
    charge = next(loaded.step(i, "t") for i in range(len(loaded.steps)) if "vendor_charge" in loaded.steps[i])["vendor_charge"]
    assert (charge["card_of"], charge["refused_as"]) == ("acme-nov-t", "acme-dec-t")


@pytest.mark.parametrize("refs", [{"card_of": 7}, {"card_of": " "}, {"refused_as": None}])
def test_a_vendor_charge_reference_that_is_not_text_stops_the_load(tmp_path, refs):
    with pytest.raises(AgentError, match="vendor_charge"):
        Scenario.load(vendor_charge_with(tmp_path, **refs))


def test_the_step_records_and_compares_the_trimmed_request_id(scenario, tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl")
    step = scenario.step(0, "t")
    gateway = sandbox_gateway(scenario, step, ledger, False)
    proposal = next(i["input"] for i in step["script"] if i.get("tool") == "propose_payment")
    first, _, _ = StepTools({**step, "request_id": " acme-nov-t  "}, gateway).call("propose_payment", proposal)
    again, _, _ = StepTools(step, gateway).call("propose_payment", proposal)
    assert first["decision"] == "allow" and again["reason"] == "duplicate_request_id"
    assert [r["request_id"] for r in ledger.records() if r["kind"] == "decision"] == ["acme-nov-t"] * 2


def test_a_blank_step_request_id_proposes_nothing():
    assert step_request_id({"request_id": "  "}) is None and step_request_id({"request_id": 7}) is None
    seen, is_error, events = StepTools({"request_id": "  "}, gateway=None).call("propose_payment", {"amount": "49.00"})
    assert is_error and "no request ID" in seen["error"] and events == []


def test_the_claude_tool_has_no_request_id_field():
    schema = next(t for t in tools_for("paypal") if t["name"] == "propose_payment")["input_schema"]
    assert "request_id" not in schema["properties"] and "request_id" not in schema["required"]


def test_a_step_without_an_order_proposes_nothing(scenario, tmp_path):
    step = {k: v for k, v in scenario.step(0, "t").items() if k != "order"}
    proposal = next(i["input"] for i in step["script"] if i.get("tool") == "propose_payment")
    seen, is_error, events = StepTools(step, gateway=None).call("propose_payment", proposal)
    assert is_error and "no order" in seen["error"] and events == []


def test_a_proposal_off_the_order_is_refused(scenario, tmp_path):
    step = scenario.step(0, "t")
    tools = StepTools(step, sandbox_gateway(scenario, step, Ledger(tmp_path / "l.jsonl"), False))
    proposal = next(i["input"] for i in step["script"] if i.get("tool") == "propose_payment")
    seen, _, _ = tools.call("propose_payment", {**proposal, "amount": "55.00"})
    assert seen["decision"] == "refuse" and seen["reason"] == "amount_mismatch"


def test_read_page_answers_only_the_step_page(scenario, tmp_path):
    step = scenario.step(1, "t")
    tools = StepTools(step, gateway=None)
    seen, is_error, shown = tools.call("read_page", {"url": "https://billing.acme.example/renew"})
    assert not is_error and "annual" in seen["text"] and shown[0]["highlight"] in seen["text"]
    seen, is_error, _ = tools.call("read_page", {"url": "https://evil.example/"})
    assert is_error and "no page" in seen["error"]
    assert tools.call("pay_now", {})[1] is True


def test_fresh_run_never_reuses_a_ledger(tmp_path):
    tag1, ledger1 = fresh_run(tmp_path)
    ledger1.path.write_text("")
    tag2, ledger2 = fresh_run(tmp_path)
    assert tag1 != tag2 and ledger1.path != ledger2.path


# Claude, replaced by a stand-in that answers with the blocks the SDK would return.

def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(name, args, id_="tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


def reply(stop, *blocks):
    return SimpleNamespace(stop_reason=stop, content=list(blocks), stop_details=None)


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.replies.pop(0)


MONTHLY = {"request_id": "acme-nov-t", "amount": "49.00", "currency": "USD", "merchant_category": "7372",
           "purpose": "Acme Analytics, November", "billing": "monthly"}


def test_claude_agent_loop_runs_tools_through_the_gateway(scenario, tmp_path):
    client = FakeClient([
        reply("tool_use", text("Reading the invoice."), tool_use("read_page", {"url": "https://billing.acme.example/invoice"}, "a")),
        reply("tool_use", tool_use("propose_payment", MONTHLY, "b")),
        reply("end_turn", text("Paid 49.00 USD.")),
    ])
    agent = ClaudeAgent(client=client, env={})
    events = run_step(scenario, 0, agent, Ledger(tmp_path / "l.jsonl"), "t")
    assert [e["kind"] for e in events] == ["task", "say", "read_page", "proposal", "decision", "say"]
    assert decisions(events)[0]["payment"]["status"] == "COMPLETED"

    first = client.calls[0]
    assert first["model"] == DEFAULT_MODEL and first["tools"] == tools_for("paypal") and "tool_choice" not in first
    assert first["system"] == system_for("paypal") and "what PayPal said" in first["system"]
    assert first["fallbacks"] == "default" and first["betas"] == [FALLBACK_BETA]
    result = client.calls[2]["messages"][-1]["content"][0]
    assert result["tool_use_id"] == "b" and result["is_error"] is False
    assert json.loads(result["content"])["decision"] == "allow"


def test_claude_gets_a_failed_tool_call_back_as_an_error(scenario, tmp_path):
    client = FakeClient([reply("tool_use", tool_use("read_page", {"url": "https://evil.example/"}, "x")),
                         reply("end_turn", text("I could not read the page."))])
    run_step(scenario, 0, ClaudeAgent(client=client, env={}), Ledger(tmp_path / "l.jsonl"), "t")
    result = client.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "no page" in json.loads(result["content"])["error"]


def test_claude_agent_model_comes_from_the_environment():
    assert ClaudeAgent(client=FakeClient([]), env={"INTENT_PAY_MODEL": "claude-haiku-5-5"}).model == "claude-haiku-5-5"


def test_claude_refusal_stops_the_step(scenario, tmp_path):
    client = FakeClient([reply("refusal")])
    events = run_step(scenario, 0, ClaudeAgent(client=client, env={}), Ledger(tmp_path / "l.jsonl"), "t")
    assert events[-1]["kind"] == "error" and "declined" in events[-1]["text"] and len(client.calls) == 1


def test_claude_loop_has_a_turn_limit(scenario, tmp_path):
    looping = [reply("tool_use", tool_use("read_page", {"url": "https://billing.acme.example/invoice"}, f"t{i}"))
               for i in range(MAX_TURNS)]
    events = run_step(scenario, 0, ClaudeAgent(client=FakeClient(looping), env={}), Ledger(tmp_path / "l.jsonl"), "t")
    assert events[-1] == {"kind": "error", "text": f"stopped after {MAX_TURNS} model turns"}


def test_make_agent():
    assert isinstance(make_agent({}), ScriptedAgent)
    with pytest.raises(AgentError, match="script or claude"):
        make_agent({"INTENT_PAY_AGENT": "gpt"})
    with pytest.raises(AgentError):  # no SDK installed, or no key: either way it says what is missing
        make_agent({"INTENT_PAY_AGENT": "claude"})


def test_cli_runs_the_scenario_on_recordings(tmp_path):
    out = subprocess.run([sys.executable, "-m", "intent_pay.agent", "--env", str(tmp_path / "none"),
                          "--ledger", str(tmp_path / "l.jsonl"), "--run", "t", "--json"],
                         cwd=ROOT, capture_output=True, text=True, env=CLEAN_ENV)
    assert out.returncode == 0, out.stdout + out.stderr
    report = json.loads(out.stdout)
    assert len(report["steps"]) == 4 and report["ledger"] == {"path": str(tmp_path / "l.jsonl"), "records": 6, "chain": "ok"}
    assert report["mode"]["payments"] == "recorded PayPal sandbox answers"


def test_cli_sandbox_without_keys_names_the_missing_variables(tmp_path):
    out = subprocess.run([sys.executable, "-m", "intent_pay.agent", "--env", str(tmp_path / "none"),
                          "--ledger", str(tmp_path / "l.jsonl"), "--sandbox", "--step", "1"],
                         cwd=ROOT, capture_output=True, text=True, env=CLEAN_ENV)
    assert out.returncode == 2 and "PAYPAL_CLIENT_ID" in json.loads(out.stdout)["detail"]


def test_cli_runs_the_airwallex_scenario_on_recordings(tmp_path):
    out = subprocess.run([sys.executable, "-m", "intent_pay.agent", "--scenario", "scenarios/airwallex-demo.json",
                          "--env", str(tmp_path / "none"), "--ledger", str(tmp_path / "l.jsonl"), "--run", "t"],
                         cwd=ROOT, capture_output=True, text=True, env=CLEAN_ENV)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "DECLINED:LIMIT_EXCEEDED" in out.stdout and "): limit rejected" in out.stdout
    assert "same rule" not in out.stdout
    assert "airwallex not called" in out.stdout and "5 records, chain ok" in out.stdout


def test_cli_labels_the_answers_of_the_chosen_step_only(tmp_path):
    out = subprocess.run([sys.executable, "-m", "intent_pay.agent", "--scenario", "scenarios/airwallex-demo.json",
                          "--env", str(tmp_path / "none"), "--ledger", str(tmp_path / "l.jsonl"), "--run", "t",
                          "--step", "2", "--json"],
                         cwd=ROOT, capture_output=True, text=True, env=CLEAN_ENV)
    assert out.returncode == 0, out.stdout + out.stderr
    assert json.loads(out.stdout)["mode"]["payments"] == "no Airwallex sandbox call"


def test_cli_airwallex_sandbox_without_keys_names_the_missing_variables(tmp_path):
    out = subprocess.run([sys.executable, "-m", "intent_pay.agent", "--scenario", "scenarios/airwallex-demo.json",
                          "--env", str(tmp_path / "none"), "--ledger", str(tmp_path / "l.jsonl"), "--sandbox", "--step", "1"],
                         cwd=ROOT, capture_output=True, text=True, env=CLEAN_ENV)
    assert out.returncode == 2 and "AIRWALLEX_CLIENT_ID" in json.loads(out.stdout)["detail"]


def test_cli_record_needs_the_sandbox(tmp_path):
    out = subprocess.run([sys.executable, "-m", "intent_pay.agent", "--env", str(tmp_path / "none"),
                          "--ledger", str(tmp_path / "l.jsonl"), "--record", str(tmp_path / "rec")],
                         cwd=ROOT, capture_output=True, text=True, env=CLEAN_ENV)
    assert out.returncode == 2 and "--sandbox" in json.loads(out.stdout)["detail"]


def test_record_saves_each_live_step_without_keys(scenario, tmp_path):
    from intent_pay.adapters.http import Response
    from intent_pay.agent import sandbox_gateway

    def live(method, url, headers, body):
        if url.endswith("/v1/oauth2/token"):
            return Response(200, json.dumps({"access_token": "A21AA-live", "expires_in": 3600}).encode(), {})
        if url.endswith("/capture"):
            return Response(201, json.dumps({"id": "O1", "status": "COMPLETED", "purchase_units": [
                {"payments": {"captures": [{"id": "C1", "status": "COMPLETED"}]}}]}).encode(), {})
        return Response(201, json.dumps({"id": "O1", "status": "CREATED"}).encode(), {})

    env = {"PAYPAL_CLIENT_ID": "id", "PAYPAL_CLIENT_SECRET": SECRET, "PAYPAL_CARD_NUMBER": CARD_NUMBER,
           "PAYPAL_CARD_EXPIRY": "2030-12"}
    import intent_pay.agent as agent_module
    real = agent_module.UrllibTransport
    agent_module.UrllibTransport = lambda: live
    try:
        def build(scenario, step, ledger, sandbox, record=None):
            return sandbox_gateway(scenario, step, ledger, sandbox, record=record, env=env)
        ledger = Ledger(tmp_path / "l.jsonl")
        for i in range(2):
            run_step(scenario, i, ScriptedAgent(), ledger, "t", sandbox=True, gateway_for=build, record=tmp_path / "rec")
    finally:
        agent_module.UrllibTransport = real
    saved = sorted(p.name for p in (tmp_path / "rec").iterdir())
    assert saved == ["step-1.json"]  # step 2 was refused and made no call
    text = (tmp_path / "rec" / "step-1.json").read_text()
    assert SECRET not in text and CARD_NUMBER not in text and "A21AA-live" not in text
    assert "recorded from the PayPal sandbox" in json.loads(text)["_source"]
    assert json.loads(text)["recorded"] is True
