# Paper crypto arb scanner

Local, paper-only scanner. It looks at public prices across exchanges, then decides whether a gap is a **chance** or just a **number**.

A chance needs all of this:

1. Same coin (deposit contract / network, not just the ticker)
2. An open path to send it (withdraw on the cheap venue, deposit on the expensive one)
3. Paper inventory that could take it (the coin already on the sell venue, USDT already on the buy venue)
4. The live book still showing the gap after fees

If any of that fails, the board still shows the percentage as **Best number · not a chance**.

No orders are sent. There are no API keys.

## Install

Python 3.10+ (3.12/3.14 is fine).

```bash
git clone https://github.com/REPLACE_OWNER/paper-arb-scanner.git
cd paper-arb-scanner
python -m pip install -r requirements.txt
copy config.example.json config.json   # Windows
# cp config.example.json config.json   # macOS / Linux
python arb.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

`config.json` is local and gitignored. Put paper balances under `inventory` if you want a row to become a chance:

```json
"inventory": {
  "binance": { "USDT": 500 },
  "kucoin": { "LUNC": 1000000 }
}
```

That example means: cash on Binance, LUNC already on KuCoin. Empty `{}` means you hold nothing there.

## What you will see

- **Hermes** — the last call (`look` / `watch` / `skip`) if you wired the agent
- **Best chance** — takeable after rails + inventory. Often empty, and that is correct
- **Best number** — the biggest leftover after fees, with why it is not a chance
- **Simple / All the numbers** — beginner labels vs the raw table

The 2% Zilliqa-style rows are usually **same ticker, closed withdrawals**. The scanner is supposed to say that.

## Link it to Hermes Agent

Hermes is the reader. The scanner finds gaps. Hermes writes the call back to the page.

1. Install [Hermes Agent](https://hermes-agent.nousresearch.com/docs/).
2. Copy the skill into your profile:

```bash
# Windows
xcopy /E /I skills\crypto-arb-brain %LOCALAPPDATA%\hermes\skills\crypto-arb-brain

# macOS / Linux
mkdir -p ~/.hermes/skills
cp -R skills/crypto-arb-brain ~/.hermes/skills/
```

3. Start the scanner (`python arb.py`) so `http://127.0.0.1:8765` answers.
4. In Hermes, create a recurring job (every 15 minutes is enough) with this prompt:

```text
You are the brain for the local paper crypto-arb scanner. No orders. No dollar amounts.

Load the crypto-arb-brain skill and follow it.
Read http://127.0.0.1:8765/api/state, /api/journal, and /api/brain.
Judge current signals. POST the call to http://127.0.0.1:8765/api/brain
as JSON with call, summary, picks, skips, and lessons.
If the call would not change, do not POST.
If port 8765 is down, start python arb.py from this repo only if nothing is already listening.
Final message: one sentence, the call and why.
```

Set the job working directory to this repo. Deliver `local` unless you want a ping in chat.

The page reads `/api/brain`. If Hermes would repeat itself, the API returns `changed: false` and stays quiet.

## How a gap dies

When a live signal closes, the journal records:

- `fast` — gone in under two minutes. Prices were probably real
- `blocked` — stayed open. Usually cannot send the coin

Blocked routes stay out of Best chance.

## API

| Path | What |
|---|---|
| `GET /` | Dashboard |
| `GET /api/state` | Live scan (no dollar amounts) |
| `GET /api/brain` | Last Hermes call |
| `POST /api/brain` | Write the call |
| `GET /api/journal` | Opened / sticky / closed history |

Binds to `127.0.0.1` only.

## Safety

Paper only. Not a trading bot. Not financial advice. Exchange network data can be wrong or stale. A green rail is still not a fill.

## Layout

```
arb.py                 scanner + local HTTP
rails.py               same-coin / can-it-move / inventory / death memory
dashboard.html         UI (board-digest.html is the same file on older trees)
config.example.json    copy to config.json
skills/crypto-arb-brain/SKILL.md
```

Runtime files (`brain.json`, `journal.jsonl`, `route_memory.json`, `config.json`) stay on your machine.
