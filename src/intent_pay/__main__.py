"""Command line for the gateway. Every command prints one JSON object.

    python -m intent_pay pay --policy policies/paypal-sandbox.toml --intent examples/intent-monthly.json
    python -m intent_pay pay ... --replay tests/fixtures/paypal/pay-completed.json   # no network, no keys
    python -m intent_pay pay ... --record state/cassettes/pay.json                   # live, saves a redacted copy
    python -m intent_pay refund --policy policies/paypal-sandbox.toml --receipt 1
    python -m intent_pay verify

Keys come from the environment, or from a .env file next to where you run it (`--env`). The
ledger defaults to state/ledger.jsonl, or INTENT_PAY_LEDGER.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Any

from intent_pay.adapters.http import RecordingTransport, ReplayTransport, UrllibTransport
from intent_pay.adapters.paypal import CardSource, ConfigError, PayPalClient, PayPalConfig, PayPalPayer
from intent_pay.gateway import Gateway, GatewayError
from intent_pay.ledger import Ledger, LedgerError
from intent_pay.policy import Engine, Policy, PolicyError

DEFAULT_LEDGER = "state/ledger.jsonl"


def load_env_file(path: pathlib.Path) -> None:
    """KEY=VALUE lines; a variable already set in the environment wins. No shell expansion."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def build_gateway(args: argparse.Namespace) -> Gateway:
    policy = Policy.load(args.policy)
    ledger = Ledger(args.ledger)
    if args.replay:
        # A cassette answers every call, so the keys are stand-ins that never leave this process.
        config = PayPalConfig(client_id="replay", client_secret="replay", card=CardSource(vault_id="replay"))
        transport: Any = ReplayTransport(args.replay)
    else:
        config = PayPalConfig.from_env()
        transport = UrllibTransport()
        if args.record:
            transport = RecordingTransport(transport, args.record, source=f"recorded from {config.base_url}")
    payer = PayPalPayer(PayPalClient(config, transport))
    # PayPal has no balance call in this build, so a policy with min_balance refuses (balance_unavailable).
    return Gateway(Engine(policy, ledger), payer)


def cmd_pay(args: argparse.Namespace) -> dict[str, Any]:
    text = sys.stdin.read() if args.intent == "-" else pathlib.Path(args.intent).read_text(encoding="utf-8")
    proposal = json.loads(text)
    outcome = build_gateway(args).submit(proposal)
    return {"agent_sees": outcome.for_agent(), **outcome.to_dict()}


def cmd_refund(args: argparse.Namespace) -> dict[str, Any]:
    outcome = build_gateway(args).refund(args.receipt)
    return outcome.to_dict()


def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    count = Ledger(args.ledger).verify()
    return {"ledger": str(args.ledger), "records": count, "chain": "ok"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m intent_pay", description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", default=".env", help="file of KEY=VALUE lines to read first (default .env)")
    parser.add_argument("--ledger", help=f"ledger file (default INTENT_PAY_LEDGER or {DEFAULT_LEDGER})")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (("pay", "decide one intent and, if allowed, pay it in the PayPal sandbox"),
                            ("refund", "refund the payment made for an allowed receipt")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--policy", required=True)
        mode = p.add_mutually_exclusive_group()
        mode.add_argument("--replay", help="answer sandbox calls from this cassette; no network, no keys")
        mode.add_argument("--record", help="call the sandbox and save a redacted copy of every exchange here")
        if name == "pay":
            p.add_argument("--intent", required=True, help="JSON file with the agent's proposal, or - for stdin")
        else:
            p.add_argument("--receipt", type=int, required=True, help="number of the allowed decision receipt")
    sub.add_parser("verify", help="check the ledger's hash chain")

    args = parser.parse_args(argv)
    load_env_file(pathlib.Path(args.env))
    args.ledger = args.ledger or os.environ.get("INTENT_PAY_LEDGER") or DEFAULT_LEDGER
    try:
        result = {"pay": cmd_pay, "refund": cmd_refund, "verify": cmd_verify}[args.command](args)
    except (ConfigError, PolicyError, GatewayError, LedgerError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
