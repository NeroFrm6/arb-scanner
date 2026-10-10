---
name: crypto-arb-brain
description: Use when reviewing the local paper arb scanner. Hermes is the sole brain.
---

# Crypto arb brain

Hermes is the only brain. The scanner finds cross-exchange gaps. Hermes decides `look` / `watch` / `skip`. No orders are sent.

## Where it lives

- Page and API: `http://127.0.0.1:8765`
- Board file: `arb_dashboard.html`
- State: `GET /api/state`
- Journal: `GET /api/journal`
- Call: `GET /api/brain` and `POST /api/brain`
- Strategy: `GET /api/strategy`
- Chat: `GET /api/chat` and `POST /api/chat`
- Bots: `GET /api/bots` from local `bots.json`

If the API is down, start `python arb.py` from the scanner folder. Do not start a second copy if port 8765 already answers. Do not bind another port.

## How to judge

Read state, journal, and the current brain before writing.

- `call` is `skip`, `watch`, or `look`. Use `look` only when a row is recommendable: same coin, open rails, paper inventory, live-checked, not sticky, not a prior blocked death.
- `same_coin` false is a skip. `movable` false is a skip. `inventory_ok` false is watch, not look.
- Journal `death=blocked` means it stayed open. `death=fast` means the prices were real and vanished.
- If POST would not change call, summary, picks, or skips, do not POST.
- Bitcoin, Ethereum, Solana, XRP, Dogecoin, and Chainlink lose to the fee. Never call those trades.
- Two routes that need the same coin on the same exchange are one chance.
- Do not put dollar amounts or machine paths in the summary, skips, lessons, strategy, or chat.
- Do not add MEV, funding, a second API, live orders, or API keys.
- Bots stay `off`, `paper`, or `paused`. Hermes still owns the call.

## Board

Nav is Call, Routes, Strategy, and Bots. Routes shows the plain cards and the full numbers together. There is no separate Simple / All the numbers switch.

## Write-back

POST only `call`, `summary`, `picks`, `skips`, and `lessons`. Then GET `/api/brain` and confirm `summary` before saying the page updated.
