"""Deposit rails: same coin, can it move, paper inventory, how the gap died."""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MEMORY_PATH = ROOT / "route_memory.json"
BINANCE_CAPITAL = "https://www.binance.com/bapi/capital/v1/public/capital/getNetworkCoinAll"

NET_ALIAS = {
    "bep20": "bsc",
    "bsc": "bsc",
    "bnb": "bsc",
    "binance": "bsc",
    "erc20": "eth",
    "eth": "eth",
    "ethereum": "eth",
    "trc20": "trx",
    "trx": "trx",
    "tron": "trx",
    "arb": "arbitrum",
    "arbitrum": "arbitrum",
    "arbitrumone": "arbitrum",
    "op": "optimism",
    "optimism": "optimism",
    "matic": "polygon",
    "polygon": "polygon",
    "sol": "sol",
    "solana": "sol",
    "zil": "zil",
    "zilliqa": "zil",
    "zilliqa2": "zil",
    "zilevm": "zilevm",
    "lunc": "lunc",
    "terra": "lunc",
    "luna": "lunc",
    "btc": "btc",
    "bitcoin": "btc",
    "bech32": "btc",
    "segwit": "btc",
    "kcc": "kcc",
    "base": "base",
    "lightning": "lightning",
    "btcln": "lightning",
}


def _norm_net(name: str) -> str:
    raw = "".join(ch for ch in str(name or "").lower() if ch.isalnum())
    return NET_ALIAS.get(raw, raw)


def _norm_addr(addr: str) -> str:
    text = str(addr or "").strip()
    if not text or text in ("0", "0x", "null", "none"):
        return ""
    if text.startswith("0x") and len(text) >= 10:
        return text.lower()
    return text.lower()


def _truth(value) -> bool | None:
    if value is True or value is False:
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "yes", "1", "enabled"):
        return True
    if text in ("false", "no", "0", "disabled"):
        return False
    return None


def _net_from_ccxt(code: str, payload: dict) -> dict:
    networks = []
    raw_nets = payload.get("networks") or {}
    if isinstance(raw_nets, dict):
        items = raw_nets.items()
    else:
        items = []
    for name, body in items:
        if not isinstance(body, dict):
            continue
        info = body.get("info") if isinstance(body.get("info"), dict) else {}
        addr = (
            body.get("contractAddress")
            or info.get("contractAddress")
            or info.get("contract")
            or info.get("address")
            or ""
        )
        dep = _truth(body.get("deposit"))
        wd = _truth(body.get("withdraw"))
        if dep is None:
            dep = _truth(info.get("isDepositEnabled") or info.get("rechargeable") or info.get("depositEnable"))
        if wd is None:
            wd = _truth(info.get("isWithdrawEnabled") or info.get("withdrawable") or info.get("withdrawEnable"))
        networks.append({
            "name": _norm_net(name or body.get("id") or info.get("chain") or info.get("chainName") or ""),
            "deposit": dep,
            "withdraw": wd,
            "addr": _norm_addr(addr),
        })
    coin_dep = _truth(payload.get("deposit"))
    coin_wd = _truth(payload.get("withdraw"))
    return {
        "code": code,
        "deposit": coin_dep,
        "withdraw": coin_wd,
        "networks": [n for n in networks if n["name"]],
        "source": "ccxt",
        "ts": time.time(),
    }


def currencies_from_exchange(ex) -> dict[str, dict]:
    out = {}
    for code, payload in (ex.currencies or {}).items():
        if not isinstance(payload, dict):
            continue
        parsed = _net_from_ccxt(code, payload)
        if parsed["networks"] or parsed["deposit"] is not None or parsed["withdraw"] is not None:
            out[code] = parsed
    return out


def fetch_binance_public() -> dict[str, dict]:
    req = urllib.request.Request(BINANCE_CAPITAL, headers={"User-Agent": "Mozilla/5.0 arb-scanner"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    rows = payload.get("data") or []
    out = {}
    for row in rows:
        code = row.get("coin")
        if not code:
            continue
        networks = []
        for net in row.get("networkList") or []:
            networks.append({
                "name": _norm_net(net.get("network") or net.get("networkDisplay") or ""),
                "deposit": _truth(net.get("depositEnable")),
                "withdraw": _truth(net.get("withdrawEnable")),
                "addr": _norm_addr(net.get("contractAddress") or ""),
            })
        out[code] = {
            "code": code,
            "deposit": _truth(row.get("depositAllEnable")),
            "withdraw": _truth(row.get("withdrawAllEnable")),
            "networks": [n for n in networks if n["name"]],
            "source": "binance-public",
            "ts": time.time(),
        }
    return out


def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return {}
    try:
        return json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_memory(memory: dict) -> None:
    MEMORY_PATH.write_text(json.dumps(memory, indent=2), encoding="utf-8")


def remember_death(memory: dict, key: str, open_for_s: int, sticky: bool) -> str:
    death = "blocked" if sticky or open_for_s >= 120 else "fast"
    slot = memory.setdefault(key, {"fast": 0, "blocked": 0, "last": None})
    slot[death] = int(slot.get(death) or 0) + 1
    slot["last"] = death
    slot["last_open_s"] = int(open_for_s)
    return death


def inventory_for(config: dict, buy: str, sell: str, pair: str) -> dict:
    inv = config.get("inventory") or {}
    base = pair.split("/")[0]
    quote = pair.split("/")[1] if "/" in pair else "USDT"
    buy_cash = float((inv.get(buy) or {}).get(quote) or 0)
    sell_coin = float((inv.get(sell) or {}).get(base) or 0)
    ok = buy_cash > 0 and sell_coin > 0
    if ok:
        note = f"Paper inventory can take this: {quote} on {buy}, {base} on {sell}."
    else:
        note = f"You would need {base} already on {sell}, and {quote} already on {buy}."
    return {"ok": ok, "note": note, "buy_cash": buy_cash, "sell_coin": sell_coin}


def classify_route(buy_cur: dict | None, sell_cur: dict | None) -> dict:
    if not buy_cur or not sell_cur:
        return {
            "same_coin": None,
            "movable": None,
            "network": None,
            "why": "No deposit rails on one of the exchanges. Cannot confirm it is the same coin.",
        }
    buy_nets = buy_cur.get("networks") or []
    sell_nets = sell_cur.get("networks") or []
    if not buy_nets or not sell_nets:
        return {
            "same_coin": None,
            "movable": None,
            "network": None,
            "why": "No network list. Treat as unknown, not a chance.",
        }

    buy_addr = {n["addr"]: n for n in buy_nets if n.get("addr")}
    sell_addr = {n["addr"]: n for n in sell_nets if n.get("addr")}
    shared_addr = set(buy_addr) & set(sell_addr)

    buy_by_net = {}
    for n in buy_nets:
        buy_by_net.setdefault(n["name"], []).append(n)
    sell_by_net = {}
    for n in sell_nets:
        sell_by_net.setdefault(n["name"], []).append(n)
    shared_net = set(buy_by_net) & set(sell_by_net)

    same = bool(shared_addr) or bool(shared_net)
    if not same:
        return {
            "same_coin": False,
            "movable": False,
            "network": None,
            "why": "Different coin. Tickers match, deposit contracts do not.",
        }

    # Prefer a live rail: withdraw on buy, deposit on sell.
    def live(n_buy, n_sell) -> bool:
        return n_buy.get("withdraw") is True and n_sell.get("deposit") is True

    for addr in shared_addr:
        if live(buy_addr[addr], sell_addr[addr]):
            return {
                "same_coin": True,
                "movable": True,
                "network": buy_addr[addr]["name"],
                "why": "Same contract, and that network can move.",
            }
    for name in shared_net:
        for nb in buy_by_net[name]:
            for ns in sell_by_net[name]:
                if live(nb, ns):
                    return {
                        "same_coin": True,
                        "movable": True,
                        "network": name,
                        "why": "Same network, and that network can move.",
                    }

    if buy_cur.get("withdraw") is False or sell_cur.get("deposit") is False:
        return {
            "same_coin": True,
            "movable": False,
            "network": next(iter(shared_net), None),
            "why": "Same coin, but withdrawals or deposits are closed.",
        }
    return {
        "same_coin": True,
        "movable": False,
        "network": next(iter(shared_net), None),
        "why": "Same coin, but no open path from the cheap exchange to the expensive one.",
    }
