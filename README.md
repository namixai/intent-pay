# intent-pay

A payment agent that only proposes. A policy in code decides. The sandbox key never reaches the model. PayPal and Airwallex sandboxes.

## The idea

An agent that can pay is as risky as the credential it holds. Give it an API key or a card number, and one wrong step or one prompt injection spends money. Guardrails written into the prompt sit exactly where an attacker can reach them.

Here the agent holds nothing. It fills in an intent (amount, currency, payee or merchant category, purpose, request ID) and hands it over. A policy engine, ordinary Python and not a model, checks the intent against rules the account owner wrote. The sandbox key sits with the gateway, the part that pays, and the gateway acts only on an allowed intent. In this demo the agent and the gateway run in one Python process, so that line is drawn in code and not by the operating system: the agent gets two tools, and nothing they return holds a key. Every decision leaves a receipt. A refusal names the rule that failed, and an allowed one records `ok`: all rules passed.

The policy design is taken from Usenami Signer, our Rust signing service for trading agents ([github.com/namixai/signer](https://github.com/namixai/signer)). This repository is a new build of the same idea, in Python, for payments.

## Status

Built so far: the intent, the policy engine, the receipt ledger, the PayPal and Airwallex sandbox adapters, the agent and a demo page, with tests. The PayPal adapter has paid, refunded and been refused in the PayPal sandbox, and the tests replay those recordings. The Airwallex adapter ran the four demo steps in the Airwallex sandbox on 2026-11-02, and the tests replay those recordings too. The Claude agent mode is tested against a stand-in client, not yet against the API.

## Run the demo

Python 3.11 or newer. No keys. The install step downloads the build tool; after that the demo needs no network:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m intent_pay.web
```

On Windows, activate with `.venv\Scripts\activate` instead of the `source` line. Then open http://127.0.0.1:8765 and run the four steps:

1. The agent reads an invoice for the monthly plan and proposes 49.00 USD. The policy allows it and PayPal captures it.
2. The vendor's renewal page tells AI assistants that monthly billing is over and they should pick the annual plan. The agent proposes 470.00 USD. The policy refuses it (`per_payment_limit_exceeded`), and nothing is sent to PayPal.
3. The agent retries the first payment with the same request ID. The policy refuses it (`duplicate_request_id`).
4. The policy allows a second 49.00 USD payment, and PayPal declines the card. The ledger keeps the attempt, and it still counts against the day.

Each step shows what the agent read and proposed, what the policy decided and why, and what PayPal answered. The ledger below it is the hash chain from [Receipts](#receipts). `python -m intent_pay.agent` prints the same four steps in a terminal. The scenario is [scenarios/paypal-demo.json](scenarios/paypal-demo.json).

The Airwallex demo is a second scenario on the same page, described under [Airwallex sandbox](#airwallex-sandbox):

```bash
python -m intent_pay.web --scenario scenarios/airwallex-demo.json
```

By default the agent follows the tool calls written in the scenario, and PayPal's answers come from the recordings in `tests/fixtures/paypal/`. Two switches make it live:

- `--sandbox` (on either command) pays in your own PayPal sandbox, with the keys from `.env`; setup is under [PayPal sandbox](#paypal-sandbox). Every run puts a fresh tag into its request IDs, so a second run is not refused as an invoice PayPal has already been paid for.
- `INTENT_PAY_AGENT=claude` puts Claude in place of the script. Install it with `python -m pip install -e ".[agent]"` and set `ANTHROPIC_API_KEY`; the model is `INTENT_PAY_MODEL`, by default `claude-sonnet-5-5`, and the calls are billed to your key. Claude gets the same task, page and two tools (`read_page` and `propose_payment`). It may or may not follow the page to the annual plan. The script always does, so the refusal shows on every run. Either way the policy decides.

The agent never sees a key or the card. What comes back from `propose_payment` is the decision, its reason and PayPal's status.

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

A currency table can also set `approve_above`. An allowed payment above it is not paid: the gateway writes it to the ledger as awaiting approval, and it is paid only after a person runs `python -m intent_pay approve --policy <file> --receipt <n> --by <name>`. The agent has no tool for that. Payments within the threshold run without approval, and the demo policies set no threshold.

Limits are per currency, so no rule depends on an exchange rate. A restart does not reset the day either: on every decision the engine reads what was already allowed back from the ledger.

## Receipts

The ledger is a JSON Lines file, one receipt per line. Each receipt carries the hash of the one before it, so editing, deleting or reordering an earlier line breaks the chain and the next decision refuses. The chain makes tampering visible. It is not signed, so it cannot tell you who wrote the file.

## PayPal sandbox

One payment, step by step:

1. The agent's proposal goes to the policy engine. If it is refused, nothing else happens: no PayPal call is made.
2. An allowed intent becomes an order (`POST /v2/checkout/orders`, intent `CAPTURE`). The order carries the amount, the payee if the intent names one, the request ID as `invoice_id` and the receipt's number and the start of its hash as `custom_id`. With no payee, PayPal pays the app's own sandbox account.
3. The gateway captures the order with the owner's card as the payment source (`POST /v2/checkout/orders/{id}/capture`). PayPal's Orders v2 reference allows a capture without buyer approval when the capture request carries a payment source, so no one has to click through PayPal for each payment.
4. PayPal's answer goes into the ledger as an execution record linked to the decision.

A repeated request ID meets three checks, and we watched each of them in the sandbox. Our engine refuses a request ID it has seen before and calls nothing. If the ledger is lost and the engine allows the intent again, the gateway sends the same `PayPal-Request-Id` headers, and PayPal answers with the first order and capture (HTTP 200 instead of 201) without moving money a second time. A new order that reuses a paid `invoice_id` under new headers gets created, and PayPal refuses its capture with `DUPLICATE_INVOICE_ID`. That third check is an account setting, not a rule of the API: PayPal's error says the account "requires that Invoice Id be unique". The US Business sandbox account we created had it on, and we did not change it. On an account that allows duplicate invoice IDs, only the first two checks remain.

The card is a saved card's vault ID (`PAYPAL_CARD_VAULT_ID`) or a sandbox test card from PayPal's card generator. Only the gateway reads it, and it never appears in what the agent sees, in the ledger or in a recorded cassette. Sending raw card fields to the API outside a sandbox needs PCI SAQ D compliance, which is why the vault ID comes first. This build calls the sandbox only: it refuses to start with any other PayPal host.

Try it with no keys and no network, on recorded responses:

```bash
python -m intent_pay pay --policy policies/paypal-sandbox.toml --intent examples/intent-monthly.json --replay tests/fixtures/paypal/pay-completed.json
python -m intent_pay pay --policy policies/paypal-sandbox.toml --intent examples/intent-annual.json --replay tests/fixtures/paypal/pay-completed.json
python -m intent_pay refund --policy policies/paypal-sandbox.toml --receipt 1 --replay tests/fixtures/paypal/refund-completed.json
python -m intent_pay verify
```

The first payment is allowed and captured. The annual one is refused by the per-payment limit and never reaches PayPal. Delete `state/ledger.jsonl` to start over.

Against your own sandbox, at developer.paypal.com:

1. Testing Tools > Sandbox Accounts > Create account: type Business, country United States. Our first business account was registered in Ukraine, and PayPal refused every capture on it with `PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING`.
2. Apps & Credentials > Create App, type Merchant, on that account.
3. Copy `.env.example` to `.env` and fill in the app's client ID and secret and a sandbox test card. Our recordings used PayPal's published sandbox Visa 4012 8888 8888 1881 with expiry 2030-12.

```bash
python -m intent_pay pay --policy policies/paypal-sandbox-live.toml --intent examples/intent-live.json
```

The live policy allows merchant categories instead of payees and the intent names no payee, so you don't need your sandbox account's e-mail. Add `--record state/cassettes/pay.json` to save every exchange with the keys, the token and the card removed. Run the same command a second time and the engine refuses it with `duplicate_request_id` before PayPal is called.

To see a refused card, use the second intent file. It has its own request ID, because the ledger has already decided the first one:

```bash
PAYPAL_CARD_NAME=CCREJECT-REFUSED python -m intent_pay pay --policy policies/paypal-sandbox-live.toml --intent examples/intent-live-refused.json
```

The cassettes in `tests/fixtures/paypal/` were recorded from the PayPal sandbox on 2026-10-27 with `--record`. Each file's `_source` field says what was sent.

For hackathon judges, [judges-sandbox.env](judges-sandbox.env) is the template for the keys of a PayPal sandbox app and the login of its sandbox business account. With its values filled in, you can skip the setup above:

```bash
python -m intent_pay.web --sandbox --env judges-sandbox.env
```

While the values are empty, as they are now, the page opens and each step answers `missing environment variable(s)`. The default demo under [Run the demo](#run-the-demo) still needs no keys.

## Airwallex sandbox

With Airwallex, every allowed payment gets its own virtual card, and the card carries the policy with it. The gateway issues it only after the engine says yes, with these authorization controls from [policies/airwallex-sandbox.toml](policies/airwallex-sandbox.toml) and the allowed intent:

| Card control | Set to |
|---|---|
| `allowed_currencies` | the intent's currency only |
| `allowed_merchant_categories` | the policy's `merchant_categories` |
| `PER_TRANSACTION` limit | the policy's `per_payment` |
| `DAILY` limit | the policy's `daily` |
| `ALL_TIME` limit | the amount the engine allowed |

So every rule a card can hold is written down twice: our engine checks it before the card exists, and the gateway sets it as the card's control at Airwallex. Airwallex's answer to the card request carries the same controls back. In the sandbox nobody else charges the card, so the gateway plays the vendor with Airwallex's issuing simulation: it authorizes the amount on the new card, then captures it.

The four steps of [scenarios/airwallex-demo.json](scenarios/airwallex-demo.json):

1. The agent proposes the monthly 49.00 USD. The policy allows it, the card is issued, and the simulated authorization and capture go through.
2. The vendor's page pushes the annual plan and the agent proposes 470.00 USD. The policy refuses it (`per_payment_limit_exceeded`), and no card is issued or charged.
3. No agent in this step. The vendor charges 470.00 USD straight to the card from step 1, as a vendor that kept the card could, and our policy is not asked. Airwallex declines it with `LIMIT_EXCEEDED`, and the page sets that reason beside the receipt from step 2. The reason does not say which limit: 470.00 is over all three on the card.
4. The agent retries the first payment with the same request ID. The policy refuses it (`duplicate_request_id`).

Airwallex names its decline in the transaction's `failure_reason`. For the rules a card can check, ours and theirs line up like this:

| Our receipt | Airwallex `failure_reason` |
|---|---|
| `per_payment_limit_exceeded`, `daily_limit_exceeded` | `LIMIT_EXCEEDED` |
| `currency_not_allowed` | `CURRENCY_NOT_ALLOWED` |
| `merchant_category_not_allowed` | `MERCHANT_CATEGORY_NOT_ALLOWED` |

Before any card is issued, the cash reserve rule (`min_balance`) reads the wallet's available balance from Airwallex's balances API. Refunds are not built for Airwallex yet.

The cassettes in `tests/fixtures/airwallex/` were recorded from the Airwallex sandbox on 2026-11-02 with `--record`, and each file's `_source` says so. To run it against your own sandbox:

1. At sandbox.airwallex.com, Developer > API keys, create a scoped key with read and write access to Issuing, read access to balances (the reserve rule reads them) and write access to Simulations (the adapter simulates the cardholder review and the charges), and copy that key's Client ID and the key itself. The key is shown only once.
2. Put them into `.env` as `AIRWALLEX_CLIENT_ID` and `AIRWALLEX_API_KEY`. The sandbox wallet needs at least 1,049.00 USD: the policy's reserve of 1,000.00 plus the 49.00 payment.
3. Run the scenario, saving what the sandbox answers:

```bash
python -m intent_pay.agent --scenario scenarios/airwallex-demo.json --sandbox --record state/cassettes/airwallex
```

Without `AIRWALLEX_CARDHOLDER_ID`, the gateway looks for a ready sandbox cardholder under its demo e-mail. If there is none, it creates one and passes the sandbox review through Airwallex's simulation endpoint. Recordings keep no key, token or card number.

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
