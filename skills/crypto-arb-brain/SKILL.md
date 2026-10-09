---
name: crypto-arb-brain
description: Use when reviewing the local arb scanner.
---

# Crypto arb brain

Hermes is the reader. The scanner finds price gaps. Hermes decides. The page shows that decision. No orders are sent.

## Where it lives

- Page and API: `http://127.0.0.1:8765`
- State: `GET /api/state`
- Past outcomes: `GET /api/journal`
- Current call: `GET /api/brain`
- Write the call: `POST /api/brain` with JSON
- Files in the scanner folder: `brain.json` and `journal.jsonl`

If the API is down, start it with `python arb.py` from the scanner folder. Do not start a second copy if port 8765 already answers.

## How to judge

Read state, journal, and the current brain before writing.

- `call` is one of `skip`, `watch`, `look`. Use `look` only when `recommendable` is true: same coin, open rails, paper inventory, live-checked, not sticky, not a prior blocked death.
- `same_coin` false is a skip. `movable` false is a skip. `inventory_ok` false is watch, not look.
- Journal `death=blocked` means it stayed open. `death=fast` means the prices were real and vanished. Only fast deaths can later become look.
- If POST would not change call/summary/picks/skips, do not POST. The API returns `changed: false` and the page should stay quiet.
- Bitcoin, Ethereum, Solana, XRP, Dogecoin, and Chainlink lose to the fee. Never call those trades.
- Two routes that need the same coin on the same exchange are one chance.
- If the journal shows this pair and route went `sticky` or `closed` after staying open, treat a repeat as a skip unless it disappears within a minute.
- Add a lesson only when the journal shows a new mistake. Do not duplicate lessons. Do not put dollar amounts in the summary, skips, or lessons.
- Write plain sentences a beginner can read.

## Write-back

POST only `call`, `summary`, `picks`, `skips`, and `lessons`. Then GET `/api/brain` and confirm `summary` matches what you sent before telling the user the page updated.
