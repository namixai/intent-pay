# Airwallex Agentic Banking Hackathon: submission text

Track 02, Intent-Bound Purchase Agent. One block per field, ready to paste. The build-phase form may ask for other fields; the idea form had only Title and Description. Sources for every number are in [the table below](#where-the-numbers-come-from).

## Title

```text
Policy-bound purchase agent: the agent proposes, the policy decides, the card enforces
```

## Description

```markdown
Track 02, Intent-Bound Purchase Agent. Team Usenami.

Repository: https://github.com/namixai/intent-pay
Video: VIDEO_URL

## The problem

An agent that can buy things is as risky as the credential it holds. Give it an API key or a card number, and one wrong step or one prompt injection spends money. Guardrails written into the prompt sit exactly where an attacker can reach them.

## What we built

A purchase agent for the kit 02 scenario, SaaS billing against a cash reserve, that holds no Airwallex key and no card. The agent fills in a typed intent: amount, currency, merchant category, billing choice and purpose. The request ID comes from the task and is set by code, so the agent cannot choose it. A policy engine, plain Python and not a model, checks it against rules the account owner wrote in a TOML file: USD only, two merchant categories, 60.00 per payment, 100.00 a day, a wallet that keeps at least 1,000.00 after the payment, and no request ID twice.

Only when the intent passes does the gateway issue a virtual card for that one payment. The card's authorization controls repeat the rules: allowed currency, allowed merchant categories, per-transaction and daily limits, and an all-time limit equal to the amount the policy allowed. Each rule a card can hold is written down twice, in our engine and at Airwallex. Every decision goes into a hash-chained ledger: a refusal names the rule that failed, and an allowed one records that all rules passed.

## What ran in the Airwallex sandbox

All four steps from our idea ran on 2026-11-02. Steps 1 and 3 called Airwallex, and the demo replays those recordings. Steps 2 and 4 stop at our policy and make no Airwallex call.

1. The agent reads an invoice and proposes the monthly 49.00 USD. The policy allows it. The gateway issues the card, and a simulated authorization and capture are approved.
2. A vendor page tells AI assistants that monthly billing is over. The agent follows it and proposes 470.00 USD, annual. The policy refuses it (per_payment_limit_exceeded). No card is issued and nothing reaches Airwallex.
3. No agent and no policy here. The vendor charges 470.00 USD straight to the card from step 1, through an issuing simulation. Airwallex declines it with LIMIT_EXCEEDED, and the page sets that reason beside the receipt from step 2.
4. The agent retries the first payment with the same request ID. The policy refuses it (duplicate_request_id).

Airwallex's answer to the card request carried back the controls we sent.

## Airwallex tools used, all in sandbox

Issuing: cardholders and cards with authorization controls. Issuing simulations: cardholder review, authorization and capture. Balances: the reserve rule reads the wallet's available balance before any card exists.

## Run it

Python 3.11 or newer. No keys are needed: the demo replays the recorded answers for steps 1 and 3.

    python -m venv .venv
    source .venv/bin/activate
    python -m pip install -e .
    python -m intent_pay.web --scenario scenarios/airwallex-demo.json

Then open http://127.0.0.1:8765 and run the four steps. The README explains how to run them against your own sandbox with --sandbox. There are 219 tests, and a mutation check breaks the rules 77 ways and finds a failing test for each.

## What differs from our idea, and what we did not do

Our idea named a Rust gateway. We built it in Python, with the policy design taken from the Rust Signer. Card freeze and simulated refunds are not built.

LIMIT_EXCEEDED does not say which limit applied: 470.00 is over all three on the card. We sent the currency and merchant category controls and saw them come back, but did not run a decline on them.

The agent and the gateway run in one Python process in this demo, so the line between them is drawn in code and not by the operating system: the agent gets two tools, and nothing they return holds a key. The video and the recorded run use a scripted agent. The Claude mode is tested against a stand-in client and has not run against the API yet.

## Team

Oleksandr (Alex) Semenets, founder of Usenami. Claude Code wrote part of the code as a coding agent.
```

Before pasting, replace `VIDEO_URL` with the uploaded video's link.

## Where the numbers come from

| Number | Source |
|---|---|
| USD, two categories, 60.00, 100.00, 1,000.00 | [policies/airwallex-sandbox.toml](../policies/airwallex-sandbox.toml) |
| 49.00 and 470.00 USD | [scenarios/airwallex-demo.json](../scenarios/airwallex-demo.json) |
| The four steps, LIMIT_EXCEEDED, 2026-11-02 | [docs/runs/2026-11-02-airwallex-sandbox.txt](runs/2026-11-02-airwallex-sandbox.txt) |
| The controls in the card answer | [tests/fixtures/airwallex/pay-cleared.json](../tests/fixtures/airwallex/pay-cleared.json) |
| 219 tests | [docs/runs/2026-11-02-tests.txt](runs/2026-11-02-tests.txt) |
| 77 mutations, none uncaught | [docs/runs/2026-11-02-mutations.txt](runs/2026-11-02-mutations.txt) |
