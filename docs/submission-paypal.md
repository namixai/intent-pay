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
