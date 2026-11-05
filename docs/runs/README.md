# Run records

What each run left behind and how to check it yourself. Every file is kept as it was written. Commands run from the repository folder with the virtual environment from [Run the demo](../../README.md#run-the-demo) active.

| File | What it is | How to check it |
|---|---|---|
| [2026-10-28-paypal-sandbox-ledger.txt](2026-10-28-paypal-sandbox-ledger.txt) | The ledger of the four PayPal demo steps run with `--sandbox` on 2026-10-28: six records. Step 1 was allowed and PayPal answered COMPLETED. Steps 2 and 3 were refused (`per_payment_limit_exceeded`, `duplicate_request_id`) and made no PayPal call. Step 4 was allowed and PayPal answered DECLINED. The file opens with a one-line note and a blank line. | Strip the note, then check the chain (below). |
| [2026-11-02-airwallex-sandbox.txt](2026-11-02-airwallex-sandbox.txt) | The terminal output of the four Airwallex demo steps run against the Airwallex sandbox on 2026-11-02 with the scripted agent. The answers it recorded are in `tests/fixtures/airwallex/`, and the demo replays them. | `python -m intent_pay.agent --scenario scenarios/airwallex-demo.json` prints the same steps from those recordings. The request IDs differ, because every run puts its own tag into them, and the `compare` line now ends in `limit rejected` where this file says `same rule`: `LIMIT_EXCEEDED` does not name the limit. |
| [2026-11-04-agent.txt](2026-11-04-agent.txt) | The four PayPal demo steps printed by `python -m intent_pay.agent` on recorded answers, from a fresh clone of commit f98216b. | Run the same command. |
| [2026-11-04-tests.txt](2026-11-04-tests.txt) | The test run from the same fresh clone: 297 passed. | `python -m pip install -e ".[dev]"`, then `python -m pytest`. |
| [2026-11-04-mutations.txt](2026-11-04-mutations.txt) | The mutation check from the same fresh clone. It breaks the code 130 ways, one at a time, and names the change each time: 130 mutations, 0 not caught. | `python tools/mutate.py` after the dev install. |
| [outside-agent/](outside-agent/) | The run of 2026-11-04 in which an outside agent, a Grok bot, did the four PayPal tasks through the MCP server: the server's journal, the ledger and two PayPal sandbox exchanges. Its own [README](outside-agent/README.md) describes the run. | Check the ledger's chain (below): 4 records. |

The 2026-11-04 files name their commit, Python version and environment on their first line. The 2026-10-28 and 2026-11-02 files do not name a commit.

## Checking a ledger's chain

`verify` writes a `.lock` file next to the ledger it reads, so check a copy in `state/`, which git ignores:

```bash
mkdir -p state
tail -n +3 docs/runs/2026-10-28-paypal-sandbox-ledger.txt > state/check-2026-10-28.jsonl
python -m intent_pay --ledger state/check-2026-10-28.jsonl verify
cp docs/runs/outside-agent/2026-11-04-ledger.jsonl state/check-outside-agent.jsonl
python -m intent_pay --ledger state/check-outside-agent.jsonl verify
```

The first check prints `"records": 6` and the second `"records": 4`, both with `"chain": "ok"`. Edit any record and `verify` fails, naming the line. Deleting or reordering records fails it too, except deleting the last one: the chain has nothing after it to check against.
