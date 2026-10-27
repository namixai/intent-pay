# intent-pay

A payment agent that only proposes. A policy in code decides. The sandbox key never reaches the model. PayPal and Airwallex sandboxes.

## The idea

An agent that can pay is as risky as the credential it holds. Give it an API key or a card number, and one wrong step or one prompt injection spends money. Guardrails written into the prompt sit exactly where an attacker can reach them.

Here the agent holds nothing. It fills in an intent (amount, currency, payee or merchant category, purpose, request ID) and hands it over. A policy engine, ordinary Python and not a model, checks the intent against rules the account owner wrote. The sandbox key sits with the process that pays, and that process acts only on an allowed intent. Every decision, allow or refuse, leaves a receipt that names the rule.

The policy design is taken from Usenami Signer, our Rust signing service for trading agents ([github.com/namixai/signer](https://github.com/namixai/signer)). This repository is a new build of the same idea, in Python, for payments.

## Status

Built so far: the intent, the policy engine, the receipt ledger and the PayPal sandbox adapter, with tests. The adapter has run only against recorded responses so far (see below). Next: the agent, the demo page, then the Airwallex adapter.

## How the policy decides

The owner writes a TOML file ([policies/example.toml](policies/example.toml)):

```toml
payees = ["billing@acme.example"]
merchant_categories = ["5734", "7372"]

[currencies.USD]
per_payment = "60.00"
daily = "100.00"
min_balance = "1000.00"
```

The rules run in this order, and the first one that fails is the reason on the receipt:

| Rule | Refused as |
|---|---|
| the proposal parses as an intent, with no unknown fields | `malformed_intent` |
| its request ID was never decided before, allowed or refused | `duplicate_request_id` |
| its currency has a table in the policy | `currency_not_allowed` |
| its payee is listed, when the policy lists payees | `payee_not_allowed` |
| its merchant category is listed, when the policy lists categories | `merchant_category_not_allowed` |
| amount is at most `per_payment` | `per_payment_limit_exceeded` |
| amount plus what was already allowed this UTC day is at most `daily` | `daily_limit_exceeded` |
| balance minus amount stays at or above `min_balance` | `balance_unavailable`, `below_min_balance` |

When something breaks, the engine refuses. A ledger that cannot be read, does not verify or cannot be written gives `ledger_unavailable`, a rule that raises an error gives `policy_error`, and a balance that cannot be read gives `balance_unavailable`. No payment is allowed before its receipt is on disk.

Limits are per currency, so no rule depends on an exchange rate. A restart does not reset the day either: on every decision the engine reads what was already allowed back from the ledger.

## Receipts

The ledger is a JSON Lines file, one receipt per line. Each receipt carries the hash of the one before it, so editing, deleting or reordering an earlier line breaks the chain and the next decision refuses. The chain makes tampering visible. It is not signed, so it cannot tell you who wrote the file.

## PayPal sandbox

One payment, step by step:

1. The agent's proposal goes to the policy engine. If it is refused, nothing else happens: no PayPal call is made.
2. An allowed intent becomes an order (`POST /v2/checkout/orders`, intent `CAPTURE`). The order carries the amount, the payee, the request ID as `invoice_id` and the receipt number as `custom_id`.
3. The gateway captures the order with the owner's card as the payment source (`POST /v2/checkout/orders/{id}/capture`). PayPal's Orders v2 reference allows a capture without buyer approval when the capture request carries a payment source, so no one has to click through PayPal for each payment.
4. PayPal's answer goes into the ledger as an execution record linked to the decision.

The request ID is checked twice. Our engine refuses a request ID it has seen before. PayPal also refuses a second order with the same `invoice_id` (`DUPLICATE_INVOICE_ID` on its Orders v2 error page), so a replay fails even if someone goes around the engine.

The card is a saved card's vault ID (`PAYPAL_CARD_VAULT_ID`) or a sandbox test card from PayPal's card generator. Only the gateway process reads it, and it never appears in what the agent sees, in the ledger or in a recorded cassette. Sending raw card fields to the API outside a sandbox needs PCI SAQ D compliance, which is why the vault ID comes first. This build calls the sandbox only: it refuses to start with any other PayPal host.

Try it with no keys and no network, on recorded responses:

```bash
python -m intent_pay pay --policy policies/paypal-sandbox.toml --intent examples/intent-monthly.json --replay tests/fixtures/paypal/pay-completed.json
python -m intent_pay pay --policy policies/paypal-sandbox.toml --intent examples/intent-annual.json --replay tests/fixtures/paypal/pay-completed.json
python -m intent_pay refund --policy policies/paypal-sandbox.toml --receipt 1 --replay tests/fixtures/paypal/refund-completed.json
python -m intent_pay verify
```

The first payment is allowed and captured. The annual one is refused by the per-payment limit and never reaches PayPal. Delete `state/ledger.jsonl` to start over.

Against your own sandbox: create an app at developer.paypal.com, put its client ID and secret and a test card in `.env`, and write your sandbox business account's e-mail as the payee in the policy and the example intents. Then run the same commands without `--replay`. Add `--record state/cassettes/pay.json` to save every exchange with the keys, the token and the card removed.

The cassettes in `tests/fixtures/paypal/` are built from the examples in PayPal's published API reference, with IDs and amounts changed to fit the tests. They were not recorded from a sandbox. Each file says so in its `_source` field.

## Run the tests

Python 3.11 or newer. On macOS or Linux:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest
```

On Windows, activate with `.venv\Scripts\activate` instead of the `source` line.

Every refusal has its own test. `python tools/mutate.py` breaks the rules one at a time on a copy of the code and checks that some test fails for each.

## Keys

Copy `.env.example` to `.env` and fill it in. The code reads keys from the environment only, and `.env` is ignored by git. The default demo mode needs no keys at all.

## License

Apache-2.0, see [LICENSE](LICENSE).
