# PayPal AI Hackathon: what we show

The project in one line: a payment agent proposes, a policy in plain Python decides, and only the gateway process holds the PayPal sandbox key.

## Run it

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m intent_pay.web
```

Then open http://127.0.0.1:8765 and run the four steps. `python -m intent_pay.agent` prints the same steps in a terminal. Neither needs a key: both replay answers recorded from the PayPal sandbox.

Checked on 2026-11-01 in a fresh clone of commit b5cb1d2 with an empty environment, on macOS with Python 3.13 and 3.14. All four steps ran in the terminal on both versions and on the page on 3.13, the ledger had 6 records with the chain verified, the replay commands in the README's [PayPal sandbox](../README.md#paypal-sandbox) section ran, and all 217 tests passed. The install step needs the network: pip downloads the build tool. Python 3.11, Linux and Windows are covered by [the CI workflow](../.github/workflows/ci.yml), not by this check.

## The four steps

| Step | What happens | What it shows |
|---|---|---|
| 1 | The agent reads an invoice and proposes 49.00 USD. The policy allows it. PayPal creates the order and captures it. | The whole path, and PayPal at the end of it |
| 2 | A renewal page tells AI assistants to take the annual plan. The agent proposes 470.00 USD. The policy refuses it (`per_payment_limit_exceeded`). | A prompt injection reaches the agent and stops at the policy; PayPal is not called |
| 3 | The agent retries step 1 with the same request ID. The policy refuses it (`duplicate_request_id`). | A retry can't pay twice |
| 4 | The policy allows a second 49.00 USD payment and PayPal declines the card. | A provider's refusal goes back to the agent and into the ledger |

## Where each part lives

| Part | File |
|---|---|
| The scenario: tasks, pages, agent's tool calls | [scenarios/paypal-demo.json](../scenarios/paypal-demo.json) |
| The owner's rules | [policies/paypal-sandbox-live.toml](../policies/paypal-sandbox-live.toml) |
| The rule engine | [src/intent_pay/policy.py](../src/intent_pay/policy.py) |
| The typed proposal | [src/intent_pay/intent.py](../src/intent_pay/intent.py) |
| The process that holds the key and pays | [src/intent_pay/gateway.py](../src/intent_pay/gateway.py) |
| The PayPal Orders v2 calls | [src/intent_pay/adapters/paypal.py](../src/intent_pay/adapters/paypal.py) |
| The hash-chained receipts | [src/intent_pay/ledger.py](../src/intent_pay/ledger.py) |
| The agent, scripted or Claude | [src/intent_pay/agent.py](../src/intent_pay/agent.py) |
| The demo page | [src/intent_pay/web.py](../src/intent_pay/web.py), [web/](../web) |
| Answers recorded from the PayPal sandbox on 2026-10-27 | [tests/fixtures/paypal/](../tests/fixtures/paypal) |
| The tests | [tests/](../tests) |
| Mutation check of the tests | [tools/mutate.py](../tools/mutate.py) |

The four steps also ran against the live PayPal sandbox on 2026-10-28, with `--sandbox`. Claude in place of the scripted agent is tested against a stand-in client, not against the API.

## The video

The script is [docs/video-paypal.md](video-paypal.md): about 2:45, under the 3-minute limit in the rules, with no music and no third-party marks.

## For judges: the PayPal sandbox

[judges-sandbox.env](../judges-sandbox.env) has what the rules ask for: the keys of a sandbox app, a test card and the login of its sandbox business account. `python -m intent_pay.web --sandbox --env judges-sandbox.env` runs the four steps against the PayPal sandbox, and the login shows the same payments at sandbox.paypal.com. While the file's values are empty, the page opens and each step answers `missing environment variable(s)`.

## The form text

One block per field, ready to paste. Sources for every number are in [the table below](#where-the-numbers-come-from). The fields are Devpost's usual ones plus the "Tools" item from the hackathon's "What to submit" list; the form itself may name them differently.

### Project name

```text
intent-pay
```

### Elevator pitch

```text
A payment agent that only proposes. A policy in plain Python decides, and only the gateway holds the PayPal sandbox key. An injected page can't push a payment past the owner's rules.
```

### About the project

```markdown
## Inspiration

An agent that can pay is as risky as the credential it holds. Give it an API key or a card number, and one wrong step or one prompt injection spends money. Guardrails written into the prompt sit exactly where an attacker can reach them.

We had already worked on this for trading agents in Usenami Signer, our Rust signing service, where the agent proposes orders and policy decides. intent-pay takes that policy design and rebuilds it in Python for payments, with PayPal at the end of the path. The repository is new: its first commit is from 2026-10-27, inside the submission period.

## What it does

It is for a team that wants an agent to pay its recurring software bills without giving the agent anything it could overspend with.

The agent holds no key and no card. It fills in an intent (amount, currency, payee or merchant category, purpose, request ID) and hands it over. A policy engine, ordinary Python and not a model, checks the intent against rules the account owner wrote in a TOML file. Only the gateway process holds the PayPal key, and it acts only on an allowed intent. Every decision, allow or refuse, leaves a receipt that names the rule.

The demo page runs four steps and shows what the agent read and proposed, what the policy decided and why, and what PayPal answered:

1. The agent reads an invoice and proposes 49.00 USD. The policy allows it, and PayPal creates the order and captures it.
2. The vendor's renewal page tells AI assistants that monthly billing is over. The agent follows it and proposes 470.00 USD. The policy refuses it (per_payment_limit_exceeded), and nothing is sent to PayPal.
3. The agent retries the first payment with the same request ID. The policy refuses it (duplicate_request_id).
4. The policy allows a second 49.00 USD payment, and PayPal declines the card. The agent reports the decline, and the attempt stays in the ledger and counts against the day.

## How we built it

The core is Python with no runtime dependencies. The rules run in a fixed order and the first failure is the reason on the receipt: a proposal that doesn't parse, request ID, currency, payee, merchant category, per-payment limit, daily limit, minimum balance. When something breaks, the engine refuses: a ledger that can't be read or verified, a rule that raises an error, a balance that can't be read. No payment is allowed before its receipt is on disk.

The ledger is a JSON Lines file in which each receipt carries the hash of the one before it. Editing, deleting or reordering a line breaks the chain, and the next decision refuses.

The PayPal adapter uses Orders v2. An allowed intent becomes an order, with the request ID as invoice_id and the receipt number as custom_id, and the gateway captures it with the owner's card as the payment source. PayPal's answer goes into the ledger, linked to its decision. A refund takes a receipt number and lands in the ledger the same way.

The default demo replays answers we recorded from the PayPal sandbox, with keys, tokens and card fields removed, so a judge can run it with no keys. One flag, --sandbox, runs the same steps against a live sandbox app.

Every refusal has its own test. tools/mutate.py breaks the rules one at a time on a copy of the code and checks that a test fails for each: 217 tests, 76 mutations, none uncaught.

## Challenges we ran into

Our first sandbox business account was registered in Ukraine, and PayPal refused every capture on it with PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING. A US Business sandbox account fixed it.

A double charge has three checks in its way, and we watched each one in the sandbox. Our engine refuses a request ID it has seen. If the ledger is lost, the same PayPal-Request-Id headers bring back the first order and capture without moving funds again. A new order that reuses a paid invoice_id gets refused with DUPLICATE_INVOICE_ID, but PayPal's error shows that this one is an account setting, not a rule of the API, so on another account only the first two checks remain.

## Accomplishments that we're proud of

The refusal in step 2 happens after the agent has already been fooled. The agent proposes the annual plan, and that is fine: the attack ends at the policy, in code the page can't reach. The whole path ran against the PayPal sandbox on 2026-10-28 with the same four outcomes.

## What we learned

Keep the model's judgment and the permission to spend apart and the design gets simpler: the agent's prompt no longer has to carry the limits.

## What's next

The Claude mode (below, under tools) has run only against a stand-in client. Running it against the API is the next step. The repository also holds an Airwallex adapter, written from Airwallex's API reference and tested on answers we wrote by hand. It has not run against the Airwallex sandbox yet.
```

### Tools

```text
Claude, through the Anthropic Python SDK, default model claude-sonnet-5-5. With INTENT_PAY_AGENT=claude, Claude takes the place of the scripted agent: it gets the same task and page and two tools, read_page and propose_payment, and may or may not follow the page to the annual plan. Either way the policy decides. This mode is tested against a stand-in client and has not run against the API yet, and the video uses the scripted agent. It needs your own ANTHROPIC_API_KEY.

Claude Code wrote part of the code as a coding agent, and CodeRabbit reviewed the pull requests.

No sponsor tools are used.
```

### Built with

```text
python, paypal-orders-api, paypal-sandbox, claude, anthropic-sdk, javascript, html, css, toml
```

### Try it out

```text
https://github.com/namixai/intent-pay
```

Video link: the YouTube link, once it is uploaded.

### Where the numbers come from

| Number | Source |
|---|---|
| 49.00 and 470.00 USD, the four outcomes | [scenarios/paypal-demo.json](../scenarios/paypal-demo.json), run in [docs/runs/2026-11-02-agent.txt](runs/2026-11-02-agent.txt) |
| 217 tests | [docs/runs/2026-11-02-tests.txt](runs/2026-11-02-tests.txt) |
| 76 mutations, none uncaught | [docs/runs/2026-11-02-mutations.txt](runs/2026-11-02-mutations.txt) |
| The run against the PayPal sandbox, 2026-10-28 | [docs/runs/2026-10-28-paypal-sandbox-ledger.txt](runs/2026-10-28-paypal-sandbox-ledger.txt) |
| First commit, 2026-10-27 | `git log --reverse` |
| PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING, DUPLICATE_INVOICE_ID | [README](../README.md#paypal-sandbox) |
