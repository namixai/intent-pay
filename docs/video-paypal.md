# PayPal demo video: script

A screen recording with a voiceover, about 2:45, under the 3-minute limit. No music. Nothing on screen but the demo page, a terminal and PayPal's own sandbox pages. Everything is in English.

## Before recording

1. Put the PayPal sandbox keys and a sandbox test card into `.env` (see the README, [PayPal sandbox](../README.md#paypal-sandbox)). The card name stays empty: step 4 sets the decline trigger by itself. With the card in `.env`, its number never shows on screen.
2. Start the page against the sandbox:

   ```bash
   python -m intent_pay.web --sandbox
   ```

3. Open http://127.0.0.1:8765 in a clean browser window: no bookmarks bar, no other tabs, window about 1440 by 900. The page follows the system's light or dark theme; keep the same one for the whole take.
4. If you want shot 8, sign in at sandbox.paypal.com as the sandbox business account in a second tab.
5. Have a second terminal ready in the repository folder for shot 9.

Every run gets new request IDs, so a failed take can simply be recorded again. "Start over" on the page begins a new run.

## Shots

| # | Time | On screen | Voiceover |
|---|------|-----------|-----------|
| 1 | 0:00 | The page, no step run yet. | This agent pays our software bills. It never holds a card or a PayPal key. All it can do is propose a payment. |
| 2 | 0:12 | Point at the three panels: Agent, Policy, PayPal. | Each proposal goes to a policy written in plain Python, not a model. The owner sets the limits: here, 60 dollars per payment and 100 a day. Only the gateway holds the PayPal sandbox key, and it pays only what the policy allowed. |
| 3 | 0:32 | Click "Run step 1". Point at the invoice, ALLOW, then the two PayPal calls. | Step one. The agent reads the invoice, 49 dollars for the monthly plan, and proposes it. The policy allows it. The gateway creates a PayPal order and captures it with the owner's card. PayPal says COMPLETED, and this is the capture ID. |
| 4 | 0:57 | "Next", "Run step 2". Point at the highlighted note on the page, then REFUSE, then "Not called". | Step two is the attack. The vendor's renewal page has a note for AI assistants: monthly is over, take the annual plan. The agent falls for it and proposes 470 dollars. The policy refuses: it's over the per-payment limit. And on the PayPal side, nothing. The refused proposal never left this process. |
| 5 | 1:27 | "Next", "Run step 3". | Step three. The agent retries the first payment with the same request ID. Refused as a duplicate, so it can't pay twice. The gateway also sends PayPal's idempotency headers, so even a lost ledger wouldn't charge again. |
| 6 | 1:45 | "Next", "Run step 4". Point at DECLINED. | Step four. The policy allows a payment and PayPal declines the card. The agent says so plainly, and the attempt still counts against today's limit. |
| 7 | 1:58 | Scroll to the ledger. Point at "hash chain verified". | Every decision, allowed or refused, and every PayPal answer is a record in a hash-chained ledger, and each one names the rule behind it. |
| 8 | 2:10 | Optional. The sandbox.paypal.com tab: the 49.00 USD payment in the account's activity. Skip the shot if it is not there. | This ran in the PayPal sandbox. Here's the same payment in the sandbox account. |
| 9 | 2:20 | Terminal: `python -m intent_pay.agent`. Let the four steps print. | You can run all of this without keys. By default the demo replays PayPal sandbox answers we recorded. Add one flag and it runs against your own sandbox. |
| 10 | 2:35 | The repository page on GitHub. | The policy design is taken from our Rust Signer. The code is open, under Apache 2.0. |

## Where the numbers come from

| Number | Source |
|--------|--------|
| 60 dollars per payment, 100 a day | `policies/paypal-sandbox-live.toml` |
| 49.00 and 470.00 USD | `scenarios/paypal-demo.json` |
| Capture ID, receipt numbers, hashes | the run on screen; they change every take |

## Do not say or show

- The words "safe", "guarantee", "audited", "real money", "mainnet".
- Any key, token, `.env` contents or card number.
- Other companies' logos or names, and no music.
- That this runs on Signer. Say only that the policy design is taken from it.
