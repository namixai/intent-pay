# Airwallex demo video: script

A screen recording with a voiceover, about 3:45, under the 5-minute limit for track 02. No music. On screen there is only the demo page, an editor, a terminal and the GitHub repository. Everything is in English.

This script is for the take we can record today. The adapter has not run against the Airwallex sandbox yet, because our sandbox key is refused at login. The Airwallex answers in this take are the hand-written ones in `tests/fixtures/airwallex/`, taken from Airwallex's API reference. The page labels them that way and the voiceover says it out loud. If a live sandbox run is recorded before the take, switch to [the live take](#the-live-take).

## Before recording

1. No keys are needed. Start the page on the Airwallex scenario:

   ```bash
   python -m intent_pay.web --scenario scenarios/airwallex-demo.json
   ```

2. Open http://127.0.0.1:8765 in a clean browser window, about 1440 by 900, with no bookmarks bar and no other tabs. The page follows the system's light or dark theme. Keep the same theme for the whole take.
3. In an editor, open `policies/airwallex-sandbox.toml` and `src/intent_pay/adapters/airwallex.py` at `def card_controls`.
4. Have a terminal ready in the repository folder for shot 9.

Every run gets new request IDs, so a failed take can simply be recorded again. "Start over" on the page begins a new run.

## Shots

| # | Time | On screen | Voiceover |
|---|------|-----------|-----------|
| 1 | 0:00 | The page, no step run yet. | This agent buys software for a small team. It never holds an Airwallex key or a card number. All it can do is propose a purchase. |
| 2 | 0:12 | Point at the three panels: Agent, Policy, Airwallex. Then at the line under "Airwallex". | Each proposal goes to a policy written in plain Python, not a model. Only the gateway holds the Airwallex sandbox key. One note before we start. In this take, Airwallex's answers are written by hand from its API reference, not recorded from the sandbox. The page says so, right here. |
| 3 | 0:35 | The editor: the policy file, then `card_controls`. | The owner writes the rules. USD only, two merchant categories, 60 dollars per payment, 100 a day, and the wallet keeps at least 1,000 dollars. When a purchase is allowed, the gateway issues a virtual card just for it, and the card carries the same rules as its own controls. The card's all-time limit is the amount the policy allowed, so it can't pay more than that. |
| 4 | 1:05 | The page. Click "Run step 1". Point at the invoice, ALLOW and receipt #1, then the four Airwallex calls. | Step one. The agent reads the invoice, 49 dollars for the monthly plan, and proposes it. The policy allows it. The gateway finds the cardholder and issues the card, and a simulated authorization and capture go through on it. |
| 5 | 1:35 | "Next", "Run step 2". Point at the highlighted note, the 470.00 proposal, REFUSE, then "Not called". | Step two is the attack. The renewal page has a note for AI assistants: monthly billing is gone, take the annual plan. The agent falls for it and proposes 470 dollars. The policy refuses, because that's over the 60-dollar limit per payment. No card is issued and nothing reaches Airwallex. |
| 6 | 2:05 | "Next", "Run step 3". Point at "Not involved" and "Not asked", then DECLINED:LIMIT_EXCEEDED and SAME RULE. | Step three. Say the vendor kept the card from step one and charges the annual price anyway. There's no agent and no policy here. The charge goes straight to the card through Airwallex's issuing simulation. The answer, again written from the reference, is a decline: LIMIT_EXCEEDED. Receipt three refused the same 470 dollars under the per-payment limit. One rule, in two places: our policy and the card's controls. |
| 7 | 2:45 | "Next", "Run step 4". Point at REFUSE and duplicate_request_id. | Step four. The agent retries November's payment with the same request ID. It's refused as a duplicate, because receipt one already decided it. Nothing reaches Airwallex. |
| 8 | 3:00 | Scroll to the ledger. Point at "5 records, hash chain verified". | Every decision and every Airwallex answer is a record in a hash-chained ledger, and each one names the rule behind it. Five records here: three decisions, the payment, and the vendor's charge. |
| 9 | 3:15 | Terminal: `python -m intent_pay.agent --scenario scenarios/airwallex-demo.json`. Let the four steps print. | You can run all of this without keys. The command replays the same hand-written answers. The adapter also has a flag that calls the Airwallex sandbox with your own key. This take doesn't use it. |
| 10 | 3:35 | The repository page on GitHub. | The policy design is taken from our Rust Signer. The code is open, under Apache 2.0. |

## The live take

Use this only if the adapter has run against the Airwallex sandbox and its answers have been recorded with `--record`. The README then says so too.

1. Put the sandbox key into `.env` and start the page with `--sandbox` added. The sandbox wallet needs at least 1,049.00 USD.
2. The line under "Airwallex" now reads "Airwallex sandbox". Card, cardholder and lifecycle IDs come from the run.
3. Change three voiceover lines:
   - Shot 2: drop the note about hand-written answers. Say instead: "This take runs in the Airwallex sandbox."
   - Shot 6: drop "again written from the reference".
   - Shot 9: "By default the demo replays Airwallex sandbox answers we recorded. Add one flag and it runs against your own sandbox."
4. Optional, after shot 4: the card from step 1 in the Airwallex sandbox web app, with its limits. Skip the shot if the limits are not shown there.

## Where the numbers come from

| Number | Source |
|--------|--------|
| 5-minute limit | Airwallex hackathon FAQ, read 2026-10-19 |
| USD only, categories 5734 and 7372, 60 per payment, 100 a day, 1,000 reserve | `policies/airwallex-sandbox.toml` |
| 49.00 and 470.00 USD, "save 20%" on the vendor page | `scenarios/airwallex-demo.json` |
| Card, cardholder and lifecycle IDs, the LIMIT_EXCEEDED answer in this take | `tests/fixtures/airwallex/*.json`, hand-written from the API reference |
| Receipt numbers, hashes, "5 records" | the run on screen; receipt numbers and the record count are the same every take |
| 1,049.00 USD for the live take | README, [Airwallex sandbox](../README.md#airwallex-sandbox) |

## Do not say or show

- The words "safe", "guarantee", "audited", "real money", "mainnet".
- Any key, token, `.env` contents or card number.
- Other companies' logos or names, and no music.
- That Airwallex declined anything in the sandbox, until a live run has recorded it.
- Card freeze or refunds. Our submission text lists them, but they are not built.
- That this runs on Signer. Say only that the policy design is taken from it.
- Anything we plan to build. The video shows only what is on screen.
