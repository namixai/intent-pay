# Airwallex demo video: runsheet

A screen recording with a voiceover, under 3:00. The track 02 limit is 5 minutes. The estimate below comes to 2:57; it counts words, it is not a measurement, so the first read-through gives the real length. If that runs long, cut in the order under [Timing](#timing). No music. On screen there is only the demo page, an editor and a terminal. Everything is in English.

This is the take on recordings, and it needs no keys. The Airwallex answers are the ones in `tests/fixtures/airwallex/`, recorded from the Airwallex sandbox on 2026-11-02. The page labels them that way, and the voiceover says so. With a working key you can record [the live take](#the-live-take) instead.

Every expected output below comes from a dry run on 2026-11-05: two fresh clones of commit dd15e97, one for the page and one for the terminal commands. The one exception is the label next to receipt #3 in shot 5, which reads LIMIT REJECTED since [#25](https://github.com/namixai/intent-pay/pull/25). On 2026-11-06, at commit 03bec78, the four steps in the terminal (`python -m intent_pay.agent --scenario scenarios/airwallex-demo.json`) and the three shot 8 commands still gave the same receipts, labels and counts; the page was not run again.

## What the video has to show

The sources are the hackathon's one-pager on its starter kits (kits 1 to 8 on AWS) and the track 02 guide.

| What | Shot | How it shows |
|---|---|---|
| The policy decides before any payment | 3, 4 | In step 1, ALLOW and receipt #1 come first, then the Airwallex calls. In step 2, REFUSE, and the Airwallex panel says "Not called" |
| A DECLINED answer | 5, 8 | Step 3: `DECLINED:LIMIT_EXCEEDED` from Airwallex. Shot 8: `"paypal":"DECLINED"` on the outside agent's second payment |
| A receipt is the ledger's record number | 7, 8 | The `#` column of the ledger on the page, 1 to 5, with decisions and answers in one sequence. Shot 8: receipts 1 and 3, and 4 records in that ledger |
| A run with an outside agent | 8 | The MCP server's journal of 2026-11-04: 7 `read_page` calls and 2 `propose_payment` calls, with no request ID; `verify` on its ledger says chain ok |
| Who proposes | 1 | The agent reads and proposes. It has no key and no card |
| Who decides | 2 | The policy file, checked by plain Python, not a model |
| Who approves | 2 | Voiceover: above an amount the owner sets, a person approves (`approve_above`, README, [How the policy decides](../README.md#how-the-policy-decides)). This policy file sets no threshold |
| Who writes | 2 | Only the gateway holds the Airwallex key |
| The agent does not call Airwallex's MCP | 2 | Voiceover: "The agent never calls Airwallex or its MCP" |
| Code sets the request ID | 3, 8 | Voiceover in shot 3. In shot 8, the outside agent's proposals carry no `request_id` |
| Track 02: a card with limits, a currency and merchant categories | 3 | The "Card controls in our request" block: USD, 5734 and 7372, 60.00, 100.00, 49.00 |
| Track 02: an authorization that passes, one that fails, and a decline reason that matches the policy | 3, 5 | APPROVED in step 1. In step 3, DECLINED:LIMIT_EXCEEDED and the LIMIT REJECTED tag next to receipt #3: both rejected 470.00 |

## Before recording

1. A fresh clone with Python 3.11 or newer. The dry run used 3.13.1 for the terminal and 3.14.4 for the page.

   ```bash
   git clone https://github.com/namixai/intent-pay ~/intent-pay-video
   ```

   ```bash
   cd ~/intent-pay-video && python3 -m venv .venv && source .venv/bin/activate && python -m pip install -e .
   ```

2. In a first terminal, start the page. This terminal stays open and off screen.

   ```bash
   python -m intent_pay.web --scenario scenarios/airwallex-demo.json
   ```

3. Open http://127.0.0.1:8765 in a clean browser window, about 1440 by 900, with no bookmarks bar, no other tabs and no extension icons. The page follows the system's light or dark theme. Keep one theme for the whole take.
4. In an editor, open `policies/airwallex-sandbox.toml` (11 lines), in a large font.
5. A second terminal in `~/intent-pay-video`: `source .venv/bin/activate`, then `clear`. Large font, at least 120 columns wide. Keep the three commands of shot 8 in a note and paste them one at a time.
6. Turn on Do Not Disturb. Nothing else on screen: no chat, no mail, no `.env`, no key files.

A failed take is simply recorded again: "Start over" on the page begins a new run. Every run gets new request IDs, while the receipt numbers and the record count stay the same.

## Shots

| # | Time | On screen | Voiceover | Must be visible |
|---|---|---|---|---|
| 1 | 0:00 | The page, no step run yet. | This agent buys software for a small team. It holds no Airwallex key and no card. It can read a page and propose a purchase, and that's all. | The three panels: "Agent: scripted agent (scenario file)", "Policy: plain Python, no model", "Airwallex: recorded Airwallex sandbox answers". The "Run step 1" button |
| 2 | 0:13 | The editor, `policies/airwallex-sandbox.toml`. Move the cursor over `merchant_categories`, `per_payment`, `daily`, `min_balance`. | The owner writes the rules, and plain Python checks them, not a model. USD only, two merchant categories, 60 dollars a payment, 100 a day, and 1,000 stays in the wallet. Above an amount the owner sets, a person approves. This file sets none. Only the gateway holds the Airwallex key. The agent never calls Airwallex or its MCP. | `["5734", "7372"]`, `[currencies.USD]`, `"60.00"`, `"100.00"`, `"1000.00"` |
| 3 | 0:40 | The page, "Run step 1". Point in turn at the invoice, PROPOSAL, ALLOW and receipt #1, the four Airwallex calls, and the "Card controls" block under `create_card`. | Step one. The agent reads the invoice, 49 dollars, and proposes it. The request ID is the task's, set by code. The policy answers first: allow, receipt 1. Only then does the gateway ask Airwallex for a card for this one payment, with the rules as its controls: 60 a transaction, 100 a day, 49 all time. These answers were recorded in the Airwallex sandbox. The page says so. | "Amount due: 49.00 USD"; "ALLOW ok", "receipt #1"; `find_cardholder HTTP 200 READY`, `create_card HTTP 202 ACTIVE`; "Card controls in our request, not an answer from the sandbox": USD, 5734, 7372, 60.00 USD, 100.00 USD, 49.00 USD; `authorize HTTP 201 APPROVED`, `capture HTTP 200 APPROVED` |
| 4 | 1:12 | "Next: step 2", "Run step 2". Point at the highlighted note, the 470.00 proposal, REFUSE, then "Not called". | Step two is the attack. The renewal page tells AI assistants to take the annual plan. The agent falls for it and proposes 470. The policy refuses, over the per-payment limit: receipt 3. No card, and no call to Airwallex. | The highlighted "Note for AI assistants handling renewals…"; PROPOSAL amount 470.00, billing annual; "REFUSE per_payment_limit_exceeded", "470.00 USD is above the per-payment limit of 60.00 USD", "receipt #3"; "Not called. The policy refused, so no request left this process." |
| 5 | 1:32 | "Next: step 3", "Run step 3". Point at "Not involved" and "Not asked", then the Airwallex answer and LIMIT REJECTED. | Step three. Say the vendor kept the card and charges 470 anyway. No agent and no policy this time. Airwallex declines it: LIMIT_EXCEEDED. Both rejected 470.00: the card here, the policy in receipt 3. | "Not involved", "Not asked. The vendor goes straight to the card."; `authorize HTTP 201`, `DECLINED:LIMIT_EXCEEDED`; "LIMIT REJECTED", "The card said LIMIT_EXCEEDED; receipt #3 said per_payment_limit_exceeded." |
| 6 | 1:50 | "Next: step 4", "Run step 4". Point at REFUSE and `duplicate_request_id`. | Step four. The agent retries November with the same request ID. Refused as a duplicate of receipt 1. | "REFUSE duplicate_request_id", "was already decided (receipt #1)", "receipt #5", "Not called" |
| 7 | 2:01 | Scroll down to "Ledger". Move the cursor down the `#` column, then over "5 records, hash chain verified". | Every decision and every Airwallex answer is a line in this hash-chained ledger. A receipt number is the line's number, so decisions and answers share one sequence. | "5 records, hash chain verified". The rows: 1 decision allow: ok; 2 execution airwallex pay: CLEARED (for #1); 3 decision refuse: per_payment_limit_exceeded; 4 execution airwallex forced_charge: DECLINED:LIMIT_EXCEEDED (for #1); 5 decision refuse: duplicate_request_id |
| 8 | 2:15 | The second terminal. Paste the three commands from [Shot 8 commands](#shot-8-commands) one at a time, and let each one print. | On November 4th an outside agent, a Grok bot, used the same two tools through our MCP server, in the PayPal sandbox. The server now runs this Airwallex scenario too. Our journal shows seven page reads and two proposals, neither with a request ID. One payment completed, and the sandbox declined the other: receipts 1 and 3. Its ledger verifies. On the injected page it proposed nothing. | `2 propose_payment`, `7 read_page`; two `sent` lines with no `request_id`, answered `"receipt":1,"paypal":"COMPLETED"` and `"receipt":3,"paypal":"DECLINED"`; `"records": 4`, `"chain": "ok"` |
| 9 | 2:50 | The same terminal: `head -3 LICENSE`. | The code is open, under Apache 2.0. The demo runs without keys. | `Apache License`, `Version 2.0, January 2004` |

The end comes at about 2:57 by the estimate.

## Shot 8 commands

Paste them as they are, from `~/intent-pay-video` with `.venv` active. On macOS `jq` is at `/usr/bin/jq` (1.7.1 in the dry run); elsewhere, install it.

```bash
jq -r '.request.params.name // empty' docs/runs/outside-agent/2026-11-04-mcp-journal.jsonl | sort | uniq -c
```

Expected:

```text
   2 propose_payment
   7 read_page
```

```bash
jq -c 'select(.request.params.name == "propose_payment") | {sent: .request.params.arguments, answer: .answer.result.content[0].text | fromjson | {decision, receipt, paypal: .payment.status}}' docs/runs/outside-agent/2026-11-04-mcp-journal.jsonl
```

Expected:

```text
{"sent":{"amount":"49.00","currency":"USD","merchant_category":"7372","purpose":"Acme Analytics Team plan, November invoice","billing":"monthly"},"answer":{"decision":"allow","receipt":1,"paypal":"COMPLETED"}}
{"sent":{"amount":"49.00","currency":"USD","merchant_category":"7372","purpose":"Acme Analytics extra seats invoice","billing":"one_time"},"answer":{"decision":"allow","receipt":3,"paypal":"DECLINED"}}
```

```bash
python -m intent_pay --ledger docs/runs/outside-agent/2026-11-04-ledger.jsonl verify
```

Expected:

```text
{
  "ledger": "docs/runs/outside-agent/2026-11-04-ledger.jsonl",
  "records": 4,
  "chain": "ok"
}
```

Shot 9:

```bash
head -3 LICENSE
```

## After recording

1. Stop the page in the first terminal with Ctrl+C.
2. `verify` leaves a lock file next to the ledger it read; in the dry run nothing else in the tree changed. Remove it:

   ```bash
   rm ~/intent-pay-video/docs/runs/outside-agent/2026-11-04-ledger.jsonl.lock
   ```

3. Put the uploaded video's link in place of `VIDEO_URL` in [submission-airwallex.md](submission-airwallex.md).

## Timing

An estimate at 150 words a minute, plus seconds for clicks and pasting. Not a measurement.

| Shot | Words | Voice | Action | Total | Starts |
|---|---|---|---|---|---|
| 1 | 28 | 11 s | 2 s | 13 s | 0:00 |
| 2 | 59 | 24 s | 3 s | 27 s | 0:13 |
| 3 | 69 | 28 s | 4 s | 32 s | 0:40 |
| 4 | 40 | 16 s | 4 s | 20 s | 1:12 |
| 5 | 34 | 14 s | 4 s | 18 s | 1:32 |
| 6 | 18 | 7 s | 4 s | 11 s | 1:50 |
| 7 | 27 | 11 s | 3 s | 14 s | 2:01 |
| 8 | 67 | 27 s | 8 s | 35 s | 2:15 |
| 9 | 12 | 5 s | 2 s | 7 s | 2:50 |
| | | | | **2:57** | |

If the read-through runs over 3:00, cut in this order:

1. All of shot 9 (7 s). The license is visible in the repository.
2. In shot 3, "These answers were recorded in the Airwallex sandbox. The page says so." (5 s). The label "recorded Airwallex sandbox answers" stays on the page.
3. In shot 8, "Its ledger verifies." and the third command (4 s).

Cut nothing from [What the video has to show](#what-the-video-has-to-show).

## The live take

1. Put `AIRWALLEX_CLIENT_ID` and `AIRWALLEX_API_KEY` into `.env` (README, [Airwallex sandbox](../README.md#airwallex-sandbox)) and start the page with `--sandbox` added. The sandbox wallet needs at least 1,049.00 USD.
2. The line under "Airwallex" now reads "Airwallex sandbox". Card, cardholder and lifecycle IDs come from the run.
3. In shot 3, say "This take runs in the Airwallex sandbox." in place of "These answers were recorded in the Airwallex sandbox. The page says so."

## Where the numbers come from

| Number | Source |
|---|---|
| 5-minute limit | Airwallex hackathon FAQ, as of 2026-10-19 |
| 2:57 and the timing table | the estimate above, 150 words a minute |
| USD only, categories 5734 and 7372, 60 per payment, 100 a day, 1,000 reserve | `policies/airwallex-sandbox.toml` |
| 49.00 and 470.00 USD, the note for AI assistants | `scenarios/airwallex-demo.json` |
| Card controls on the page: USD, 5734 and 7372, 60.00, 100.00, 49.00 all time | our `create_card` request, built by `card_controls` in `src/intent_pay/adapters/airwallex.py` from the policy and the allowed intent |
| Card, cardholder and lifecycle IDs, the LIMIT_EXCEEDED answer | `tests/fixtures/airwallex/*.json`, recorded from the sandbox on 2026-11-02; the run is `docs/runs/2026-11-02-airwallex-sandbox.txt` |
| Receipt numbers, "5 records" | the run on screen; they are the same every take |
| 7 `read_page`, 2 `propose_payment`, receipts 1 and 3, 4 records | `docs/runs/outside-agent/`, the run of 2026-11-04 |
| 1,049.00 USD for the live take | README, [Airwallex sandbox](../README.md#airwallex-sandbox) |

## Do not say or show

- The words "safe", "guarantee", "audited", "audit", "real money", "mainnet".
- Any key, token, `.env` contents or card number, or the tunnel address and token of the 2026-11-04 MCP server.
- Other companies' logos, GitHub's pages, and music. The only other names spoken are PayPal (the sandbox of the 2026-11-04 run) and Grok bot.
- Card freeze or refunds in Airwallex. They are not in the code.
- That this runs on Signer.
- Anything we plan to build. The video shows only what is on screen.
