# A payment held for approval, Airwallex sandbox, 2026-11-05

In the other recorded runs the policy sets no approval threshold, so no payment waits for a person. Here the policy sets `approve_above = "40.00"` for USD. The 49.00 USD monthly renewal from step 1 of the Airwallex demo is allowed by the rules and then held until someone approves it.

The approval in this run is an operator approval in sandbox. No person reviewed the payment: the run script approved it right after the hold, under the name `test operator (operator approval in sandbox)`, less than a second later by the ledger's timestamps. What the run shows is the path: the policy allows the payment, the gateway holds it and creates no card, a named approval goes into the ledger, and only then is a card created and charged.

## How it was run

From a fresh clone of commit dd15e97 with Python 3.13.1, against the Airwallex sandbox, with the keys in a file outside the repository. The run worked in `state/approval/`; its inputs and outputs are copied here unchanged.

- [policy.toml](policy.toml) is [policies/airwallex-sandbox.toml](../../../policies/airwallex-sandbox.toml) with `approve_above = "40.00"` added and its comments changed. Its hash, `6f91b378…`, is the `policy_hash` in record 1.
- [scenario.json](scenario.json) is step 1 of [scenarios/airwallex-demo.json](../../../scenarios/airwallex-demo.json), unchanged except for `replay`, which names this run's recording. Its policy is `policy.toml`. With `--sandbox` the agent ignores `replay`.

The commands, in order:

```bash
python -m intent_pay.agent --scenario state/approval/scenario.json --sandbox --env <key file> --ledger state/approval/ledger.jsonl --record state/approval/recorded
python -m intent_pay --ledger state/approval/ledger.jsonl verify
python -m intent_pay --env <key file> --ledger state/approval/ledger.jsonl approve --policy state/approval/policy.toml --receipt 1 --by "test operator (operator approval in sandbox)" --record state/approval/recorded/approve.json
python -m intent_pay --env <key file> --ledger state/approval/ledger.jsonl approve --policy state/approval/policy.toml --receipt 1 --by "test operator (operator approval in sandbox)" --record state/approval/recorded/approve-again.json
python -m intent_pay --ledger state/approval/ledger.jsonl verify
```

[terminal.txt](terminal.txt) is their output. It shows the key file's path as `<key file>`, and its command lines lost the quotes around the `--by` value when the run script printed them.

## What happened

| Record | What the ledger says | Time (UTC) |
|---|---|---|
| 1, decision | `allow`, `ok`: 49.00 USD, monthly, merchant category 7372, request ID `acme-nov-1105123102` | 12:31:02 |
| 2, execution | `airwallex`, `AWAITING_APPROVAL`: "49.00 USD is above the approval threshold of 40.00 USD; nothing is paid until a person approves receipt #1" | 12:31:04 |
| 3, execution | `person`, `APPROVED`: "approved by test operator (operator approval in sandbox)" | 12:31:04 |
| 4, execution | `airwallex`, `CLEARED`: a card with the policy's controls, 49.00 USD authorized and captured | 12:31:17 |

While the payment was held, the sandbox got two calls ([recorded/step-1.json](recorded/step-1.json)): the login and the balance read for the `min_balance` rule. No card was created. The approval made six ([recorded/approve.json](recorded/approve.json)): the login, the balance read again for the second check, the cardholder lookup, the card, and the issuing simulation's authorization and capture.

The agent's answer while the payment was held: "The policy allowed it, and it now waits for a person to approve it. Nothing was paid yet." The agent cannot approve. It has two tools, `read_page` and `propose_payment`, and `approve` is a command the gateway's operator runs.

The second `approve` exited with code 2 and `receipt #1 was already approved and paid`. It wrote nothing to the ledger, and it made no sandbox call: `approve-again.json` was never written, because a recording is written on the first call. The last `verify` still counted 4 records.

## The files

| File | What it is |
|---|---|
| [ledger.jsonl](ledger.jsonl) | The gateway's hash-chained ledger: the four records above. |
| [policy.toml](policy.toml), [scenario.json](scenario.json) | The run's inputs. |
| [recorded/step-1.json](recorded/step-1.json) | The sandbox exchanges of the agent's step, with the access token removed. |
| [recorded/approve.json](recorded/approve.json) | The sandbox exchanges of the approval, with the access token and the card number removed. |
| [terminal.txt](terminal.txt) | The terminal output of the five commands. |

## How to check

The chain and the policy hash, from the repository folder with the virtual environment from [Run the demo](../../../README.md#run-the-demo) active:

```bash
mkdir -p state
cp docs/runs/2026-11-05-airwallex-approval/ledger.jsonl state/check-approval.jsonl
python -m intent_pay --ledger state/check-approval.jsonl verify
python -c "from intent_pay.policy import Policy; print(Policy.load('docs/runs/2026-11-05-airwallex-approval/policy.toml').hash)"
```

`verify` prints `"records": 4` and `"chain": "ok"`. The hash it prints is the `policy_hash` of record 1.

The same run on the recorded answers, with no keys and no network:

```bash
python -m intent_pay.agent --scenario docs/runs/2026-11-05-airwallex-approval/scenario.json --ledger state/replay-approval.jsonl
python -m intent_pay --ledger state/replay-approval.jsonl approve --policy docs/runs/2026-11-05-airwallex-approval/policy.toml --receipt 1 --by "test operator (operator approval in sandbox)" --replay docs/runs/2026-11-05-airwallex-approval/recorded/approve.json
python -m intent_pay --ledger state/replay-approval.jsonl verify
```

The agent's step ends in `AWAITING_APPROVAL`, the approval in `CLEARED`, and `verify` counts 4 records. The request ID differs, because every run puts its own tag into it. Delete `state/replay-approval.jsonl` before running it again.
