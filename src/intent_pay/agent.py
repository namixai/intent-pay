"""The agent side: it reads a vendor page and proposes a payment. It never pays.

An agent gets two tools. `read_page` returns a page from the scenario file, and
`propose_payment` hands one intent to the gateway and returns `Outcome.for_agent()`: the
decision, the rule behind it and what the sandbox said. Nothing the agent can call reaches a
key, the card or the policy file.

There are two agents. The scripted agent (the default) follows the tool calls written in the
scenario file, so the demo runs the same way every time and needs no model key. With
INTENT_PAY_AGENT=claude, Claude gets the same task, page and tools and decides for itself; the
model is INTENT_PAY_MODEL (default claude-sonnet-5-5) and the key is ANTHROPIC_API_KEY. Those
calls are paid, so the tests never make one.

    python -m intent_pay.agent --scenario scenarios/paypal-demo.json             # recorded answers
    python -m intent_pay.agent --scenario scenarios/paypal-demo.json --sandbox   # PayPal sandbox
    python -m intent_pay.agent --scenario scenarios/airwallex-demo.json --sandbox
    INTENT_PAY_AGENT=claude python -m intent_pay.agent --scenario scenarios/paypal-demo.json

A step can also have no agent at all: `vendor_charge` makes the vendor charge an earlier
payment's card directly, without our policy (Airwallex issuing simulation, sandbox only), to show
that the card refuses the charge on its own and for the same reason as our receipt.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from intent_pay.adapters import airwallex, paypal
from intent_pay.adapters.http import RecordingTransport, ReplayTransport, UrllibTransport
from intent_pay.gateway import Gateway, GatewayError
from intent_pay.ledger import Ledger, LedgerError
from intent_pay.policy import Engine, Policy

CONFIG_ERRORS = (paypal.ConfigError, airwallex.ConfigError)
PROVIDER_NAMES = {"paypal": "PayPal", "airwallex": "Airwallex"}

DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_TURNS = 8
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM = """You are the purchasing agent of a small company. You pay its software bills.

You can read vendor pages and propose payments. You never see the card or any key: a payment
gateway checks each proposal against the owner's policy and pays only what the policy allows.
Its answer tells you the decision, the rule behind it and what {provider} said.

Propose at most one payment per task, with the request ID the task gives you. Copy the amount
and the merchant category from the vendor page. When a payment is refused or fails, say why in
one or two sentences and stop; the owner decides what happens next."""

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_page",
        "description": "Read a vendor web page. Returns the page text.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The page URL, as given in the task."}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_payment",
        "description": ("Propose one payment. The gateway checks it against the owner's policy, pays it in "
                        "the {provider} sandbox if allowed, and returns the decision and {provider}'s answer."),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "The request ID from the task."},
                "amount": {"type": "string", "description": "Decimal amount as a string, for example \"49.00\"."},
                "currency": {"type": "string", "description": "Three-letter ISO 4217 code, for example USD."},
                "merchant_category": {"type": "string", "description": "Four-digit merchant category code."},
                "purpose": {"type": "string", "description": "What the payment is for, in a few words."},
                "billing": {"type": "string", "enum": ["one_time", "monthly", "annual"]},
            },
            "required": ["request_id", "amount", "currency", "merchant_category", "purpose", "billing"],
            "additionalProperties": False,
        },
    },
]


def system_for(provider: str) -> str:
    return SYSTEM.format(provider=PROVIDER_NAMES[provider])


def tools_for(provider: str) -> list[dict[str, Any]]:
    return _fill(_TOOLS, PROVIDER_NAMES[provider], "{provider}")


class AgentError(Exception):
    """The agent cannot run (no SDK, no key, a broken scenario)."""


def run_tag(now: datetime | None = None) -> str:
    """A tag for one run, put into the scenario's request IDs: the sandbox account blocks an
    invoice ID it has been paid for, so a second live run must not reuse the first one's."""
    return (now or datetime.now(timezone.utc)).strftime("%m%d%H%M%S")


def fresh_run(directory: pathlib.Path, prefix: str = "") -> tuple[str, Ledger]:
    """A run tag and a ledger file no earlier run on this machine has used, even within the
    same second."""
    base = run_tag()
    tag, n = base, 1
    while (directory / f"{prefix}{tag}.jsonl").exists():
        n += 1
        tag = f"{base}-{n}"
    return tag, Ledger(directory / f"{prefix}{tag}.jsonl")


def _fill(value: Any, run: str, marker: str = "{run}") -> Any:
    if isinstance(value, str):
        return value.replace(marker, run)
    if isinstance(value, list):
        return [_fill(v, run, marker) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, run, marker) for k, v in value.items()}
    return value


@dataclass(frozen=True)
class Scenario:
    path: pathlib.Path
    name: str
    policy: pathlib.Path
    provider: str
    steps: tuple[dict[str, Any], ...]

    @classmethod
    def load(cls, path: str | pathlib.Path) -> Scenario:
        path = pathlib.Path(path).resolve()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            steps = tuple(data["steps"])
            for step in steps:
                if not step.get("task") or not (isinstance(step.get("script"), list) or step.get("vendor_charge")):
                    raise AgentError(f"{path.name}: every step needs a task and a script or a vendor_charge")
            provider = data.get("provider", "paypal")
            if provider not in PROVIDER_NAMES:
                raise AgentError(f"{path.name}: provider must be one of {', '.join(PROVIDER_NAMES)}")
            return cls(path=path, name=data["name"], policy=path.parent / data["policy"],
                       provider=provider, steps=steps)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AgentError(f"cannot read scenario {path}: {exc}") from exc

    def step(self, index: int, run: str) -> dict[str, Any]:
        if not 0 <= index < len(self.steps):
            raise AgentError(f"{self.path.name} has steps 1 to {len(self.steps)}")
        return _fill(self.steps[index], run)

    def resolve(self, relative: str) -> pathlib.Path:
        return self.path.parent / relative

    @property
    def provider_name(self) -> str:
        return PROVIDER_NAMES[self.provider]

    def replay_label(self, indexes: Iterable[int] | None = None) -> str:
        """What the answers are when the run is not live, for the steps that run (all of them by
        default): recorded from the sandbox, or written by hand from the provider's reference (a
        cassette that says "recorded": false). A step with no recording makes no sandbox call."""
        chosen = [self.steps[i] for i in (range(len(self.steps)) if indexes is None else indexes)]
        replays = [step["replay"] for step in chosen if step.get("replay")]
        if not replays:
            return f"no {self.provider_name} sandbox call"
        for replay in replays:
            try:
                cassette = json.loads(self.resolve(replay).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(cassette, dict) and cassette.get("recorded") is False:
                return f"{self.provider_name} sandbox answers written from its API reference, not recorded"
        return f"recorded {self.provider_name} sandbox answers"


def sandbox_gateway(scenario: Scenario, step: Mapping[str, Any], ledger: Ledger, sandbox: bool,
                    record: pathlib.Path | None = None, env: Mapping[str, str] = os.environ) -> Gateway:
    """The gateway for one step: the provider's sandbox with keys from the environment, or the
    step's recorded sandbox answers with stand-in keys that never leave this process. A step
    with no `replay` gets an empty recording, so any call it makes fails the step."""
    env = {**env, **step.get("sandbox_env", {})}
    transport: Any
    if sandbox:
        transport = UrllibTransport()
        if record is not None:
            transport = RecordingTransport(transport, record, source=(
                f"recorded from the {scenario.provider_name} sandbox on {datetime.now(timezone.utc):%Y-%m-%d} "
                f"by python -m intent_pay.agent --record, {scenario.path.name}, step {step['title']!r}"))
    elif step.get("replay"):
        transport = ReplayTransport(scenario.resolve(step["replay"]))
    else:
        transport = ReplayTransport({"exchanges": []})
    policy = Policy.load(scenario.policy)
    if scenario.provider == "airwallex":
        config = airwallex.AirwallexConfig.from_env(env) if sandbox else airwallex.AirwallexConfig("replay", "replay")
        payer = airwallex.AirwallexPayer(airwallex.AirwallexClient(config, transport), policy,
                                         sleep=time.sleep if sandbox else (lambda _s: None))
        return Gateway(Engine(policy, ledger, balance=payer.available_balance), payer)
    if sandbox:
        pp_config = paypal.PayPalConfig.from_env(env)
    else:
        pp_config = paypal.PayPalConfig(client_id="replay", client_secret="replay",
                                        card=paypal.CardSource(vault_id="replay"))
    return Gateway(Engine(policy, ledger), paypal.PayPalPayer(paypal.PayPalClient(pp_config, transport)))


class StepTools:
    """The two tools for one step. `call` returns what the agent reads back, whether it is an
    error, and the events to show."""

    def __init__(self, step: Mapping[str, Any], gateway: Gateway, provider: str = "paypal"):
        self.step = step
        self.gateway = gateway
        self.provider = provider

    def call(self, name: str, args: Any) -> tuple[dict[str, Any], bool, list[dict[str, Any]]]:
        if not isinstance(args, dict):
            return {"error": "tool input must be an object"}, True, []
        if name == "read_page":
            page = self.step.get("page") or {}
            if args.get("url") != page.get("url"):
                return {"error": f"no page at {args.get('url')!r}"}, True, [
                    {"kind": "read_page", "url": args.get("url"), "text": None}]
            return {"url": page["url"], "text": page["text"]}, False, [
                {"kind": "read_page", "url": page["url"], "text": page["text"], "highlight": page.get("highlight")}]
        if name == "propose_payment":
            outcome = self.gateway.submit(args)
            seen = outcome.for_agent()
            return seen, False, [{"kind": "proposal", "input": args},
                                 {"kind": "decision", "agent_sees": seen, "outcome": outcome.to_dict()}]
        return {"error": f"unknown tool {name!r}"}, True, []


def summary(seen: Mapping[str, Any], provider: str = "paypal") -> str:
    """What the scripted agent says after a proposal, built from the gateway's answer."""
    name = PROVIDER_NAMES[provider]
    if seen.get("decision") != "allow":
        return f"Refused by the owner's policy ({seen.get('reason')}): {seen.get('detail')}. Nothing was sent to {name}."
    payment = seen.get("payment") or {}
    if payment.get("ok"):
        what = "capture" if provider == "paypal" else "card"
        return f"Paid. {name} {what} {payment.get('reference')}, status {payment.get('status')}."
    return f"The policy allowed it, but {name} answered {payment.get('status')}. Nothing was paid; the attempt is in the ledger."


class ScriptedAgent:
    label = "scripted agent (scenario file)"

    def run(self, step: Mapping[str, Any], tools: StepTools) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = [{"kind": "task", "text": step["task"]}]
        for item in step["script"]:
            if "say" in item:
                events.append({"kind": "say", "text": item["say"]})
                continue
            seen, _, shown = tools.call(item["tool"], item["input"])
            events.extend(shown)
            if item["tool"] == "propose_payment":
                events.append({"kind": "say", "text": summary(seen, tools.provider)})
        return events


class ClaudeAgent:
    """Claude as the agent, in a plain tool-use loop. Only the gateway acts on its proposals."""

    def __init__(self, model: str | None = None, client: Any = None, env: Mapping[str, str] = os.environ):
        self.model = model or env.get("INTENT_PAY_MODEL") or DEFAULT_MODEL
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise AgentError("INTENT_PAY_AGENT=claude needs the Anthropic SDK: pip install -e '.[agent]'") from exc
            if not env.get("ANTHROPIC_API_KEY"):
                raise AgentError("INTENT_PAY_AGENT=claude needs ANTHROPIC_API_KEY")
            client = anthropic.Anthropic()
        self.client = client
        self.label = f"Claude ({self.model})"

    def run(self, step: Mapping[str, Any], tools: StepTools) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = [{"kind": "task", "text": step["task"]}]
        messages: list[dict[str, Any]] = [{"role": "user", "content": step["task"]}]
        for _ in range(MAX_TURNS):
            response = self.client.beta.messages.create(
                model=self.model, max_tokens=16000, system=system_for(tools.provider),
                tools=tools_for(tools.provider), messages=messages,
                output_config={"effort": "medium"},
                # If a classifier declines the request, the API retries it on another model in the same call.
                betas=[FALLBACK_BETA], fallbacks="default")
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    events.append({"kind": "say", "text": block.text.strip()})
            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                events.append({"kind": "error", "text": f"the model declined ({getattr(details, 'category', None)})"})
                return events
            if response.stop_reason != "tool_use":
                if response.stop_reason not in ("end_turn", "stop_sequence"):
                    events.append({"kind": "error", "text": f"the model stopped: {response.stop_reason}"})
                return events
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                seen, is_error, shown = tools.call(block.name, block.input)
                events.extend(shown)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(seen), "is_error": is_error})
            messages.append({"role": "user", "content": results})
        events.append({"kind": "error", "text": f"stopped after {MAX_TURNS} model turns"})
        return events


def make_agent(env: Mapping[str, str] = os.environ) -> ScriptedAgent | ClaudeAgent:
    mode = (env.get("INTENT_PAY_AGENT") or "script").lower()
    if mode == "script":
        return ScriptedAgent()
    if mode == "claude":
        return ClaudeAgent(env=env)
    raise AgentError(f"INTENT_PAY_AGENT must be script or claude, not {mode!r}")


def card_reason(status: str | None) -> str | None:
    """LIMIT_EXCEEDED from DECLINED:LIMIT_EXCEEDED."""
    return status.split(":", 1)[1] if status and status.startswith("DECLINED:") else None


def vendor_charge(step: Mapping[str, Any], gateway: Gateway) -> list[dict[str, Any]]:
    """The vendor charges an earlier payment's card directly. No agent and no policy: the card's
    own controls answer, and the answer is set beside the receipt that refused the same charge."""
    charge = step["vendor_charge"]
    events: list[dict[str, Any]] = [{"kind": "task", "text": step["task"]}]
    try:
        outcome = gateway.force_charge(charge["card_of"], charge["amount"], charge["currency"],
                                       charge.get("merchant_category"))
    except GatewayError as exc:
        events.append({"kind": "error", "text": f"{exc}; run the step that pays for it first"})
        return events
    compare: dict[str, Any] = {"request_id": charge.get("refused_as")}
    with gateway.ledger.session() as state:
        seq = state.request_ids.get(charge.get("refused_as"))
        refused = state.record(seq) if seq is not None else None
    if refused is not None:
        compare.update(receipt=refused["seq"], reason=refused["reason"],
                       expected=airwallex.CARD_REASONS.get(refused["reason"]))
    payment = outcome.payment
    compare["card"] = card_reason(payment.status if payment else None)
    compare["match"] = compare.get("expected") is not None and compare["card"] == compare["expected"]
    events.append({"kind": "vendor_charge", "input": charge, "outcome": outcome.to_dict(), "compare": compare})
    return events


def run_step(scenario: Scenario, index: int, agent: ScriptedAgent | ClaudeAgent, ledger: Ledger, run: str,
             sandbox: bool = False, gateway_for: Callable[..., Gateway] = sandbox_gateway,
             record: pathlib.Path | None = None) -> list[dict[str, Any]]:
    step = scenario.step(index, run)
    gateway = gateway_for(scenario, step, ledger, sandbox,
                          record=None if record is None else record / f"step-{index + 1}.json")
    if step.get("vendor_charge"):
        return vendor_charge(step, gateway)
    return agent.run(step, StepTools(step, gateway, scenario.provider))


def _line(event: Mapping[str, Any], provider: str = "paypal") -> str:
    kind = event["kind"]
    if kind in ("task", "say", "error"):
        return f"  {'agent' if kind == 'say' else kind:<9} {event['text']}"
    if kind == "read_page":
        return f"  {'reads':<9} {event['url']}" + ("" if event.get("text") else " (no such page)")
    if kind == "proposal":
        p = event["input"]
        return (f"  {'proposes':<9} {p.get('amount')} {p.get('currency')}, {p.get('billing')}, "
                f"category {p.get('merchant_category')}, request {p.get('request_id')}")
    if kind == "decision":
        seen = event["agent_sees"]
        text = f"  {'policy':<9} {str(seen.get('decision')).upper()} ({seen.get('reason')}), receipt #{seen.get('receipt')}"
        payment = event["outcome"].get("payment")
        if payment is None:
            return text + "\n  " + f"{provider:<9} not called"
        return text + "\n  " + f"{provider:<9} {_calls(payment)}"
    if kind == "vendor_charge":
        c, compare = event["input"], event["compare"]
        payment = event["outcome"].get("payment") or {}
        text = (f"  {'vendor':<9} charges {c.get('amount')} {c.get('currency')} to the card of {c.get('card_of')}, "
                f"category {c.get('merchant_category')}\n  {'policy':<9} not asked\n  {provider:<9} {_calls(payment)}")
        if compare.get("receipt") is not None:
            verdict = "same rule" if compare["match"] else "NOT the same rule"
            text += (f"\n  {'compare':<9} card {compare.get('card')}, receipt #{compare['receipt']} "
                     f"{compare.get('reason')} (card reason {compare.get('expected')}): {verdict}")
        return text
    return f"  {kind}"


def _calls(payment: Mapping[str, Any]) -> str:
    calls = ", ".join(f"{s['call']} {s['http']} {s.get('status') or ''}".rstrip() for s in payment.get("steps") or [])
    return calls or str(payment.get("status"))


def main(argv: list[str] | None = None) -> int:
    from intent_pay.__main__ import load_env_file

    parser = argparse.ArgumentParser(prog="python -m intent_pay.agent", description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenario", default="scenarios/paypal-demo.json")
    parser.add_argument("--step", type=int, help="run only this step (1-based); default: all of them")
    parser.add_argument("--sandbox", action="store_true", help="pay in the scenario's sandbox with keys from .env")
    parser.add_argument("--record", type=pathlib.Path,
                        help="with --sandbox: save each step's exchanges, keys removed, as DIR/step-N.json")
    parser.add_argument("--env", default=".env", help="file of KEY=VALUE lines to read first (default .env)")
    parser.add_argument("--ledger", help="ledger file (default: a new one under state/runs/)")
    parser.add_argument("--run", help="tag put into request IDs (default: the current UTC time)")
    parser.add_argument("--json", action="store_true", help="print the events as JSON")
    args = parser.parse_args(argv)
    load_env_file(pathlib.Path(args.env))

    if args.ledger:
        run, ledger = args.run or run_tag(), Ledger(args.ledger)
    else:
        run, ledger = fresh_run(pathlib.Path("state") / "runs")
        run = args.run or run
    try:
        scenario = Scenario.load(args.scenario)
        agent = make_agent()
        indexes = [args.step - 1] if args.step else range(len(scenario.steps))
        report = []
        if args.record and not args.sandbox:
            raise AgentError("--record saves live sandbox answers, so it needs --sandbox")
        for i in indexes:
            events = run_step(scenario, i, agent, ledger, run, sandbox=args.sandbox, record=args.record)
            report.append({"step": i + 1, "title": scenario.steps[i]["title"], "events": events})
            if not args.json:
                print(f"Step {i + 1}: {scenario.steps[i]['title']}")
                print("\n".join(_line(e, scenario.provider) for e in events))
                print()
        count = ledger.verify()
    except (AgentError, LedgerError, OSError, *CONFIG_ERRORS) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}, indent=2))
        return 2
    mode = {"agent": agent.label,
            "payments": f"{scenario.provider_name} sandbox" if args.sandbox else scenario.replay_label(indexes)}
    if args.json:
        print(json.dumps({"scenario": scenario.name, "mode": mode, "run": run, "steps": report,
                          "ledger": {"path": str(ledger.path), "records": count, "chain": "ok"}}, indent=2))
    else:
        print(f"{mode['agent']}; payments: {mode['payments']}")
        print(f"ledger {ledger.path}: {count} records, chain ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
