"""Spatial crypto arbitrage scanner. Paper only. Binds to 127.0.0.1.

Buy the ask on one exchange, sell the bid on another. Ticker gaps are not
fills: a route becomes a signal only after two scans, a live order book, and
taker fees. No orders are sent.
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import ccxt.async_support as ccxt

from rails import (
    classify_route,
    currencies_from_exchange,
    fetch_binance_public,
    inventory_for,
    load_memory,
    remember_death,
    save_memory,
)

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
HTML_PATH = next(
    (p for p in (ROOT / "dashboard.html", ROOT / "board-digest.html") if p.exists()),
    ROOT / "dashboard.html",
)
JOURNAL_PATH = ROOT / "journal.jsonl"
BRAIN_PATH = ROOT / "brain.json"
PORT_PATH = ROOT / "port.txt"

FAST = ["binance", "bybit", "okx", "kucoin", "bitget", "kraken"]
SLOW = ["mexc", "gate"]
TRUSTED = {"binance", "bybit", "okx", "kucoin", "bitget", "kraken"}
MAJORS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT", "LINK/USDT"]
SKIP_BASES = {
    "USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "USDE", "USD1",
    "EUR", "USD", "XUSD", "USDD", "PYUSD", "USDS",
}
HOST = "127.0.0.1"
DEPTH_BAND = 0.0015
FRESH_S = {"fast": 30, "slow": 90}
STALE_TICKER_MS = 30_000

NOTE = (
    "A chance means the same coin can move, the book still showed the gap, and paper inventory could take it. "
    "A big number without those is not a chance. Majors on these venues are normally inside the fee."
)


_MONEY = re.compile(r"\$[\d,]+(?:\.\d+)?")
_PATH = re.compile(r"([A-Za-z]:\\[^\s\"']+|/[A-Za-z]/Users/[^\s\"']+)")


def public_text(value: str) -> str:
    text = _PATH.sub("", _MONEY.sub("", value))
    return re.sub(r"\s{2,}", " ", text).strip()


def public_episode(ep: dict, now: float) -> dict:
    age = max(0, int(now - ep["opened"]))
    stuck = bool(ep.get("sticky"))
    return {
        "pair": ep["pair"],
        "buy": ep["buy"],
        "sell": ep["sell"],
        "book_net": ep["book_net"],
        "sticky": stuck,
        "open_for_s": age,
        "same_coin": ep.get("same_coin"),
        "movable": ep.get("movable"),
        "reason": (
            "Stuck open. Often means you cannot move the coin."
            if stuck
            else "The book still shows this gap."
        ),
    }


def explain(row: dict, episodes: dict, used_sells: set) -> tuple[str, int | None]:
    key = f"{row['pair']}|{row['buy']}|{row['sell']}"
    ep = episodes.get(key)
    age = max(0, int(time.time() - ep["opened"])) if ep else None
    if row.get("same_coin") is False:
        return "Different coin. The ticker matches, the deposit contract does not.", age
    if row.get("movable") is False:
        return row.get("rail_why") or "The coin cannot be sent from the cheap exchange to the expensive one.", age
    if row.get("same_coin") is None or row.get("movable") is None:
        return row.get("rail_why") or "Cannot confirm it is the same coin, or that it can move.", age
    if not row.get("inventory_ok"):
        return row.get("inventory_note") or "Paper inventory cannot take this.", age
    if ep and ep.get("sticky"):
        return "Stuck open. That usually means the coin cannot move.", age
    if row.get("last_death") == "blocked":
        return "This route already stuck open before. Treat as blocked until it dies fast.", age
    if ep:
        return "The book still shows this gap. Paper inventory could take it.", age
    if row.get("book_net") is None:
        return "Book not checked yet.", None
    if row.get("book_net") <= 0:
        return "The gap was gone when we looked at the book.", None
    if (row["pair"], row["sell"]) in used_sells:
        return "Same coins already used by a better row.", None
    if row.get("net", 0) <= 0:
        return "The fee is bigger than the gap.", None
    return "Watching. Not taken.", None


def public_edge(row: dict, episodes: dict | None = None, used_sells: set | None = None) -> dict:
    reason, age = ("Watching.", None)
    if episodes is not None:
        reason, age = explain(row, episodes, used_sells or set())
    return {
        "pair": row["pair"],
        "buy": row["buy"],
        "sell": row["sell"],
        "gross": row["gross"],
        "net": row["net"],
        "book_net": row.get("book_net"),
        "trusted": row["trusted"],
        "signal": row["signal"],
        "sticky": row["sticky"],
        "reason": reason,
        "open_for_s": age,
        "same_coin": row.get("same_coin"),
        "movable": row.get("movable"),
        "network": row.get("network"),
        "inventory_ok": row.get("inventory_ok"),
        "inventory_note": row.get("inventory_note"),
        "last_death": row.get("last_death"),
        "recommendable": bool(row.get("recommendable")),
    }


def public_major(row: dict) -> dict:
    reason = "The fee is bigger than the gap." if row.get("net", 0) <= 0 else "This one beats the fee. Rare on the big coins."
    return {
        "pair": row["pair"],
        "buy": row["buy"],
        "sell": row["sell"],
        "gross": row["gross"],
        "net": row["net"],
        "reason": reason,
    }


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def append_journal(event: dict) -> None:
    keep = ("t", "event", "pair", "buy", "sell", "book_net", "open_for_s", "sticky", "death", "same_coin", "movable")
    event = {k: event.get(k) for k in keep}
    event["t"] = event.get("t") or time.time()
    with JOURNAL_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")


def read_journal(limit: int = 40) -> list[dict]:
    if not JOURNAL_PATH.exists():
        return []
    lines = JOURNAL_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def default_brain() -> dict:
    return {
        "updated_at": None,
        "call": "waiting",
        "summary": "Hermes has not reviewed this yet.",
        "picks": [],
        "skips": [],
        "lessons": [
            "A gap that stays open for more than two minutes is usually a coin you cannot send across. Do not call it a trade.",
            "Bitcoin and the other big coins lose to the fee. Do not call those trades.",
            "Two routes that need the same coins are one chance, not two.",
            "If the live book no longer shows the gap, the listed price was wrong.",
        ],
    }


def read_brain() -> dict:
    if not BRAIN_PATH.exists():
        return default_brain()
    try:
        data = json.loads(BRAIN_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default_brain()
    base = default_brain()
    base.update({k: data.get(k, base[k]) for k in base})
    return base


def write_brain(data: dict) -> dict:
    clean = default_brain()
    call = str(data.get("call") or "watch")
    if call not in ("skip", "watch", "look", "waiting"):
        call = "watch"
    clean["call"] = call
    clean["summary"] = public_text(str(data.get("summary") or ""))[:500]
    clean["updated_at"] = time.time()
    picks = []
    for item in data.get("picks") or []:
        if isinstance(item, dict) and item.get("pair"):
            picks.append({
                "pair": str(item.get("pair"))[:24],
                "buy": str(item.get("buy") or "")[:16],
                "sell": str(item.get("sell") or "")[:16],
                "why": public_text(str(item.get("why") or ""))[:180],
            })
    skips = []
    for item in data.get("skips") or []:
        if isinstance(item, dict) and item.get("pair"):
            skips.append({
                "pair": str(item.get("pair"))[:24],
                "why": public_text(str(item.get("why") or ""))[:180],
            })
    lessons = []
    for lesson in data.get("lessons") or clean["lessons"]:
        text = public_text(str(lesson))[:220]
        if text and text not in lessons:
            lessons.append(text)
    clean["picks"] = picks[:5]
    clean["skips"] = skips[:8]
    clean["lessons"] = lessons[:12]
    prev = read_brain()
    sig = json.dumps({"call": clean["call"], "summary": clean["summary"], "picks": clean["picks"], "skips": clean["skips"]}, sort_keys=True)
    prev_sig = json.dumps({"call": prev.get("call"), "summary": prev.get("summary"), "picks": prev.get("picks"), "skips": prev.get("skips")}, sort_keys=True)
    clean["changed"] = sig != prev_sig
    if not clean["changed"]:
        clean["updated_at"] = prev.get("updated_at")
        clean["summary"] = prev.get("summary") or clean["summary"]
        BRAIN_PATH.write_text(json.dumps({**prev, "changed": False}, indent=2), encoding="utf-8")
        out = read_brain()
        out["changed"] = False
        return out
    BRAIN_PATH.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    return clean


def leveraged(symbol: str) -> bool:
    base = symbol.split("/")[0]
    return base.endswith(("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR"))


def make_exchange(name: str):
    opts = {"enableRateLimit": True, "timeout": 20000}
    if name == "bybit":
        opts["options"] = {"defaultType": "spot"}
    return getattr(ccxt, name)(opts)


def ticker_quote(ticker: dict, now: float) -> dict | None:
    bid, ask = ticker.get("bid"), ticker.get("ask")
    if not bid or not ask:
        return None
    bid, ask = float(bid), float(ask)
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    if (ask - bid) / ask > 0.015:
        return None
    ts = ticker.get("timestamp")
    if ts:
        try:
            if (now * 1000) - float(ts) > STALE_TICKER_MS:
                return None
        except (TypeError, ValueError):
            pass
    qv = ticker.get("quoteVolume") or 0
    try:
        qv = float(qv or 0)
    except (TypeError, ValueError):
        qv = 0.0
    return {"bid": bid, "ask": ask, "qv": qv, "ts": now}


def level_px_amt(level) -> tuple[float, float]:
    return float(level[0]), float(level[1])


def depth_notional(levels) -> tuple[float, float | None]:
    if not levels:
        return 0.0, None
    touch, _ = level_px_amt(levels[0])
    if touch <= 0:
        return 0.0, None
    total = 0.0
    for level in levels:
        px, amt = level_px_amt(level)
        if abs(px - touch) / touch > DEPTH_BAND:
            break
        total += px * amt
    return total, touch


class Engine:
    def __init__(self, config: dict):
        self.config = config
        self.fees = config["fees"]
        self.lock = threading.RLock()
        self.quotes: dict[str, dict[str, dict]] = {}
        self.venue_meta: dict[str, dict] = {}
        self.depth: dict[tuple[str, str], dict] = {}
        self.episodes: dict[str, dict] = {}
        self.pending: dict[str, int] = {}
        self.misses: dict[str, int] = {}
        self.best_net_history: list[dict] = []
        self.log: list[str] = []
        self.scan_count = 0
        self.dropped = 0
        self.pairs = 0
        self.last_scan_at = 0.0
        self.started_at = time.time()
        self.trusted_rows: list[dict] = []
        self.other_rows: list[dict] = []
        self.major_rows: list[dict] = []
        self.rails: dict[str, dict[str, dict]] = {}
        self.route_memory: dict = load_memory()
        self._push_log("starting")

    def _push_log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            self.log.insert(0, f"{stamp}  {line}")
            del self.log[12:]

    def snapshot(self) -> dict:
        with self.lock:
            now = time.time()
            used_sells = {(e["pair"], e["sell"]) for e in self.episodes.values()}
            episodes = sorted(
                (public_episode(e, now) for e in self.episodes.values()),
                key=lambda e: e["book_net"],
                reverse=True,
            )
            trusted = [public_edge(r, self.episodes, used_sells) for r in self.trusted_rows]
            other = [public_edge(r, self.episodes, used_sells) for r in self.other_rows]
            best = None
            for row in trusted:
                if row.get("book_net") is None:
                    continue
                if best is None or row["book_net"] > best["book_net"]:
                    best = {
                        "pair": row["pair"],
                        "buy": row["buy"],
                        "sell": row["sell"],
                        "book_net": row["book_net"],
                        "sticky": row["sticky"],
                        "open_for_s": row["open_for_s"],
                        "reason": row["reason"],
                    }
            venues = []
            for name in FAST + SLOW:
                meta = self.venue_meta.get(name, {})
                err = meta.get("error")
                venues.append({
                    "name": name,
                    "ok": bool(meta.get("ok")),
                    "ms": meta.get("ms") or 0,
                    "error": public_text(err) if err else None,
                })
            return {
                "mode": "paper",
                "scan_count": self.scan_count,
                "last_scan_at": self.last_scan_at,
                "pairs": self.pairs,
                "dropped_divergent": self.dropped,
                "trusted": trusted,
                "other": other,
                "majors": [public_major(r) for r in self.major_rows],
                "episodes": episodes,
                "best_book": best,
                "recommendations": self._recommendations(trusted),
                "best_numbers": self._best_numbers(trusted),
                "best_net_history": self.best_net_history[-80:],
                "venues": venues,
                "log": [public_text(line) for line in self.log],
                "note": NOTE,
            }

    def merge_quotes(self, name: str, books: dict[str, dict], ms: int, error: str | None, tier: str) -> None:
        now = time.time()
        with self.lock:
            if error:
                self.venue_meta[name] = {"ok": False, "ms": ms, "pairs": 0, "error": error, "ts": now, "tier": tier}
                return
            slot = self.quotes.setdefault(name, {})
            slot.clear()
            slot.update(books)
            self.venue_meta[name] = {"ok": True, "ms": ms, "pairs": len(books), "error": None, "ts": now, "tier": tier}

    def recompute(self, allow_signals: bool) -> None:
        now = time.time()
        fees = self.fees
        band = float(self.config["collision_band"])
        min_qv = float(self.config["min_quote_volume"])
        fresh: dict[str, dict[str, dict]] = {}
        with self.lock:
            for ex, syms in self.quotes.items():
                tier = "slow" if ex in SLOW else "fast"
                max_age = FRESH_S[tier]
                for sym, q in syms.items():
                    if now - q["ts"] > max_age:
                        continue
                    fresh.setdefault(sym, {})[ex] = q

        dropped = 0
        edges = []
        majors = []
        used_pairs = 0

        for sym, books in fresh.items():
            if len(books) < 2:
                continue
            mids = [(q["bid"] + q["ask"]) / 2 for q in books.values()]
            med = statistics.median(mids)
            if med <= 0:
                continue
            clean = {}
            for ex, q in books.items():
                mid = (q["bid"] + q["ask"]) / 2
                if abs(mid - med) / med > band:
                    dropped += 1
                    continue
                clean[ex] = q
            if len(clean) < 2:
                continue
            used_pairs += 1
            if sym in MAJORS:
                trusted_books = {ex: q for ex, q in clean.items() if ex in TRUSTED}
                if len(trusted_books) >= 2:
                    majors.append(self._best_cross(sym, trusted_books, fees))
            for buy, bq in clean.items():
                for sell, sq in clean.items():
                    if buy == sell or sq["bid"] <= bq["ask"]:
                        continue
                    if bq["qv"] and sq["qv"] and min(bq["qv"], sq["qv"]) < min_qv:
                        continue
                    gross = (sq["bid"] - bq["ask"]) / bq["ask"]
                    fee = fees.get(buy, 0.001) + fees.get(sell, 0.001)
                    net = gross - fee
                    row = self._row(sym, buy, sell, bq["ask"], sq["bid"], gross, fee, net, now)
                    edges.append(row)

        edges.sort(key=lambda r: r["net"], reverse=True)
        trusted = [r for r in edges if r["trusted"]][:20]
        other = [r for r in edges if not r["trusted"]][:10]
        majors = [m for m in majors if m]
        majors.sort(key=lambda r: MAJORS.index(r["pair"]) if r["pair"] in MAJORS else 99)

        best_ticker_net = trusted[0]["net"] if trusted else (majors[0]["net"] if majors else 0.0)
        # Prefer a book-confirmed number for the sparkline once we have one.
        book_nets = [r["book_net"] for r in trusted if r.get("book_net") is not None]
        spark_net = max(book_nets) if book_nets else best_ticker_net

        with self.lock:
            self.dropped = dropped
            self.pairs = used_pairs
            self.trusted_rows = trusted
            self.other_rows = other
            self.major_rows = majors
            self.last_scan_at = now
            if allow_signals:
                self.scan_count += 1
                self.best_net_history.append({"t": now, "net": spark_net})
                del self.best_net_history[:-80]
                self._update_episodes_locked(trusted, now)
                best = trusted[0] if trusted else None
                if best:
                    self._push_log(
                        f"scan {self.scan_count}  {self.pairs} pairs  best {best['pair']} "
                        f"{best['buy']}->{best['sell']} ticker {best['net'] * 100:+.3f}%"
                    )
                else:
                    self._push_log(f"scan {self.scan_count}  {self.pairs} pairs  no gross-positive trusted edge")
            self._stamp_signals_locked(trusted)

    def _row(self, sym, buy, sell, ask, bid, gross, fee, net, now) -> dict:
        trusted = buy in TRUSTED and sell in TRUSTED
        book_buy = self.depth.get((buy, sym))
        book_sell = self.depth.get((sell, sym))
        book_net = None
        depth_usd = None
        book_ask = None
        book_bid = None
        if book_buy and book_sell and now - book_buy["ts"] < 25 and now - book_sell["ts"] < 25:
            book_ask = book_buy.get("ask")
            book_bid = book_sell.get("bid")
            if book_ask and book_bid and book_ask > 0:
                book_gross = (book_bid - book_ask) / book_ask
                book_net = book_gross - fee
                depth_usd = min(book_buy.get("ask_depth") or 0, book_sell.get("bid_depth") or 0)
        return {
            "pair": sym,
            "buy": buy,
            "sell": sell,
            "ask": ask,
            "bid": bid,
            "gross": gross,
            "fee": fee,
            "net": net,
            "book_net": book_net,
            "depth_usd": depth_usd,
            "trusted": trusted,
            "signal": False,
            "sticky": False,
            **self._rail_fields(sym, buy, sell),
        }

    def _rail_fields(self, pair: str, buy: str, sell: str) -> dict:
        base = pair.split("/")[0]
        rail = classify_route(self.rails.get(buy, {}).get(base), self.rails.get(sell, {}).get(base))
        inv = inventory_for(self.config, buy, sell, pair)
        key = f"{pair}|{buy}|{sell}"
        last_death = (self.route_memory.get(key) or {}).get("last")
        recommendable = (
            rail["same_coin"] is True
            and rail["movable"] is True
            and inv["ok"]
            and last_death != "blocked"
        )
        return {
            "same_coin": rail["same_coin"],
            "movable": rail["movable"],
            "network": rail["network"],
            "rail_why": rail["why"],
            "inventory_ok": inv["ok"],
            "inventory_note": inv["note"],
            "last_death": last_death,
            "recommendable": recommendable,
        }

    def _skip_keys(self) -> tuple[set, set]:
        brain = read_brain()
        pair_skips = set()
        route_skips = set()
        for item in brain.get("skips") or []:
            pair = item.get("pair")
            if not pair:
                continue
            if item.get("buy") and item.get("sell"):
                route_skips.add((pair, item["buy"], item["sell"]))
            else:
                pair_skips.add(pair)
        return pair_skips, route_skips

    def _recommendations(self, trusted: list[dict]) -> list[dict]:
        pair_skips, route_skips = self._skip_keys()
        ranked = [
            r for r in trusted
            if r.get("recommendable")
            and (r.get("book_net") or 0) > 0
            and not r.get("sticky")
            and r["pair"] not in pair_skips
            and (r["pair"], r["buy"], r["sell"]) not in route_skips
        ]
        ranked.sort(key=lambda r: r.get("book_net") or 0, reverse=True)
        used_sell, used_buy, out = set(), set(), []
        for row in ranked:
            sell_k = (row["pair"], row["sell"])
            buy_k = (row["pair"], row["buy"])
            if sell_k in used_sell or buy_k in used_buy:
                continue
            used_sell.add(sell_k)
            used_buy.add(buy_k)
            out.append(row)
            if len(out) == 3:
                break
        return out

    def _best_numbers(self, trusted: list[dict]) -> list[dict]:
        ranked = [r for r in trusted if (r.get("book_net") or r.get("net") or 0) > 0]
        ranked.sort(key=lambda r: r.get("book_net") if r.get("book_net") is not None else r.get("net") or 0, reverse=True)
        used_sell, used_buy, out = set(), set(), []
        rec_keys = {(r["pair"], r["buy"], r["sell"]) for r in self._recommendations(trusted)}
        for row in ranked:
            key = (row["pair"], row["buy"], row["sell"])
            if key in rec_keys:
                continue
            sell_k = (row["pair"], row["sell"])
            buy_k = (row["pair"], row["buy"])
            if sell_k in used_sell or buy_k in used_buy:
                continue
            used_sell.add(sell_k)
            used_buy.add(buy_k)
            out.append(row)
            if len(out) == 3:
                break
        return out

    def _best_cross(self, sym, clean, fees) -> dict | None:
        best = None
        for buy, bq in clean.items():
            for sell, sq in clean.items():
                if buy == sell:
                    continue
                gross = (sq["bid"] - bq["ask"]) / bq["ask"]
                fee = fees.get(buy, 0.001) + fees.get(sell, 0.001)
                net = gross - fee
                if best is None or net > best["net"]:
                    best = {
                        "pair": sym,
                        "buy": buy,
                        "sell": sell,
                        "ask": bq["ask"],
                        "bid": sq["bid"],
                        "gross": gross,
                        "net": net,
                    }
        return best

    def _stamp_signals_locked(self, trusted: list[dict]) -> None:
        for row in trusted:
            key = f"{row['pair']}|{row['buy']}|{row['sell']}"
            ep = self.episodes.get(key)
            row["signal"] = ep is not None
            row["sticky"] = bool(ep and ep.get("sticky"))

    def _update_episodes_locked(self, trusted: list[dict], now: float) -> None:
        min_net = float(self.config["min_net"])
        min_clip = float(self.config["min_clip_usd"])
        max_clip = float(self.config["max_clip_usd"])
        need = int(self.config["confirm_scans"])
        capital_left = float(self.config["capital_usd"])
        confirmed = []
        for row in trusted:
            key = f"{row['pair']}|{row['buy']}|{row['sell']}"
            book_net = row.get("book_net")
            depth = row.get("depth_usd")
            ok = (
                book_net is not None
                and book_net >= min_net
                and depth is not None
                and depth >= min_clip
            )
            if not ok:
                self.pending[key] = 0
                continue
            self.pending[key] = self.pending.get(key, 0) + 1
            if self.pending[key] < need:
                continue
            confirmed.append(row)

        confirmed.sort(key=lambda r: r["book_net"], reverse=True)
        used_sell = set()
        used_buy = set()
        selected = {}
        for row in confirmed:
            key = f"{row['pair']}|{row['buy']}|{row['sell']}"
            sell_k = (row["pair"], row["sell"])
            buy_k = (row["pair"], row["buy"])
            if sell_k in used_sell or buy_k in used_buy:
                continue
            clip = min(max_clip, row["depth_usd"] * 0.5, capital_left)
            if clip < min_clip:
                continue
            used_sell.add(sell_k)
            used_buy.add(buy_k)
            capital_left -= clip
            selected[key] = (row, clip)

        for key, (row, clip) in selected.items():
            book_net = row["book_net"]
            pnl = round(clip * book_net, 4)
            ep = self.episodes.get(key)
            if ep is None:
                self.episodes[key] = {
                    "pair": row["pair"],
                    "buy": row["buy"],
                    "sell": row["sell"],
                    "book_net": book_net,
                    "clip": round(clip, 2),
                    "pnl": pnl,
                    "opened": now,
                    "last_seen": now,
                    "sticky": False,
                    "same_coin": row.get("same_coin"),
                    "movable": row.get("movable"),
                }
                self._push_log(
                    f"signal {row['pair']} {row['buy']}->{row['sell']} "
                    f"book {book_net * 100:+.3f}%"
                )
                append_journal({
                    "t": now,
                    "event": "opened",
                    "pair": row["pair"],
                    "buy": row["buy"],
                    "sell": row["sell"],
                    "book_net": book_net,
                    "open_for_s": 0,
                    "sticky": False,
                })
            else:
                ep["last_seen"] = now
                ep["book_net"] = book_net
                ep["clip"] = round(clip, 2)
                ep["pnl"] = pnl
                was_sticky = bool(ep.get("sticky"))
                ep["sticky"] = (now - ep["opened"]) > 120
                if ep["sticky"] and not was_sticky:
                    append_journal({
                        "t": now,
                        "event": "sticky",
                        "pair": ep["pair"],
                        "buy": ep["buy"],
                        "sell": ep["sell"],
                        "book_net": book_net,
                        "open_for_s": int(now - ep["opened"]),
                        "sticky": True,
                    })

        confirmed_keys = {f"{r['pair']}|{r['buy']}|{r['sell']}" for r in confirmed}
        for key in list(self.episodes):
            if key in selected:
                self.misses[key] = 0
                continue
            if key in confirmed_keys:
                gone = self.episodes.pop(key)
                self.misses.pop(key, None)
                self._push_log(f"dropped overlap {gone['pair']} {gone['buy']}->{gone['sell']}")
                continue
            self.misses[key] = self.misses.get(key, 0) + 1
            if self.misses[key] >= 2:
                gone = self.episodes.pop(key)
                self.misses.pop(key, None)
                self.pending.pop(key, None)
                open_for = int(now - gone.get("opened", now))
                death = remember_death(self.route_memory, key, open_for, bool(gone.get("sticky")))
                save_memory(self.route_memory)
                self._push_log(f"closed {gone['pair']} {gone['buy']}->{gone['sell']} {death}")
                append_journal({
                    "t": now,
                    "event": "closed",
                    "pair": gone["pair"],
                    "buy": gone["buy"],
                    "sell": gone["sell"],
                    "book_net": gone.get("book_net"),
                    "open_for_s": open_for,
                    "sticky": bool(gone.get("sticky")),
                    "death": death,
                    "same_coin": gone.get("same_coin"),
                    "movable": gone.get("movable"),
                })

    def targets_for_books(self) -> list[tuple[str, str]]:
        with self.lock:
            rows = list(self.trusted_rows[:5])
        targets = []
        seen = set()
        for row in rows:
            if row["net"] <= 0:
                continue
            for ex in (row["buy"], row["sell"]):
                key = (ex, row["pair"])
                if key not in seen:
                    seen.add(key)
                    targets.append(key)
        # Always confirm BTC on the two fastest books so majors are not ticker-only.
        for ex in ("binance", "bybit"):
            key = (ex, "BTC/USDT")
            if key not in seen:
                seen.add(key)
                targets.append(key)
        return targets[:8]

    def store_depth(self, ex: str, sym: str, book: dict) -> None:
        with self.lock:
            self.depth[(ex, sym)] = book


ENGINE: Engine | None = None
HTTP_PORT = 8765


async def fetch_exchange(ex, name: str) -> tuple[str, dict, int, str | None]:
    started = time.time()
    try:
        params = {"category": "spot"} if name == "bybit" else {}
        tickers = await ex.fetch_tickers(params=params) if params else await ex.fetch_tickers()
        now = time.time()
        books = {}
        for sym, ticker in tickers.items():
            if not isinstance(sym, str) or not sym.endswith("/USDT") or ":" in sym:
                continue
            base = sym.split("/")[0]
            if base in SKIP_BASES or leveraged(sym):
                continue
            market = ex.markets.get(sym) or {}
            if market.get("spot") is False:
                continue
            quote = ticker_quote(ticker, now)
            if quote:
                books[sym] = quote
        return name, books, int((time.time() - started) * 1000), None
    except Exception as e:
        return name, {}, int((time.time() - started) * 1000), f"{type(e).__name__}: {e}"[:160]


async def refresh(exchanges: dict, tier: str) -> None:
    assert ENGINE is not None
    results = await asyncio.gather(
        *[fetch_exchange(ex, name) for name, ex in exchanges.items()],
        return_exceptions=True,
    )
    for item in results:
        if isinstance(item, Exception):
            ENGINE._push_log(f"{tier} fetch failed {type(item).__name__}")
            continue
        name, books, ms, error = item
        ENGINE.merge_quotes(name, books, ms, error, tier)


async def enrich(exchanges: dict) -> None:
    assert ENGINE is not None
    targets = ENGINE.targets_for_books()
    if not targets:
        return

    async def one(ex_name: str, sym: str):
        ex = exchanges.get(ex_name)
        if ex is None:
            return
        try:
            ob = await ex.fetch_order_book(sym, limit=20)
            bid_n, bid = depth_notional(ob.get("bids") or [])
            ask_n, ask = depth_notional(ob.get("asks") or [])
            ENGINE.store_depth(ex_name, sym, {
                "bid_depth": bid_n,
                "ask_depth": ask_n,
                "bid": bid,
                "ask": ask,
                "ts": time.time(),
            })
        except Exception as e:
            ENGINE._push_log(f"book {ex_name} {sym} {type(e).__name__}")

    await asyncio.gather(*[one(ex, sym) for ex, sym in targets], return_exceptions=True)


async def run() -> None:
    assert ENGINE is not None
    fast = {name: make_exchange(name) for name in FAST}
    slow = {name: make_exchange(name) for name in SLOW}
    try:
        await asyncio.gather(*[ex.load_markets() for ex in fast.values()])
        ENGINE._push_log("markets loaded")
        for name, ex in fast.items():
            ENGINE.rails[name] = currencies_from_exchange(ex)
        try:
            public = await asyncio.to_thread(fetch_binance_public)
            ENGINE.rails["binance"] = public
            ENGINE._push_log(f"binance rails {len(public)}")
        except Exception as e:
            ENGINE._push_log(f"binance rails {type(e).__name__}")
        await refresh(fast, "fast")
        ENGINE.recompute(allow_signals=False)
        await enrich(fast)
        ENGINE.recompute(allow_signals=True)

        async def slow_loop():
            await asyncio.gather(*[ex.load_markets() for ex in slow.values()], return_exceptions=True)
            for name, ex in slow.items():
                ENGINE.rails[name] = currencies_from_exchange(ex)
            while True:
                await refresh(slow, "slow")
                ENGINE.recompute(allow_signals=False)
                await asyncio.sleep(40)

        asyncio.create_task(slow_loop())
        while True:
            started = time.time()
            await refresh(fast, "fast")
            ENGINE.recompute(allow_signals=False)
            await enrich(fast)
            ENGINE.recompute(allow_signals=True)
            wait = max(1.0, 8.0 - (time.time() - started))
            await asyncio.sleep(wait)
    finally:
        await asyncio.gather(
            *[ex.close() for ex in list(fast.values()) + list(slow.values())],
            return_exceptions=True,
        )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            payload = ENGINE.snapshot() if ENGINE else {"mode": "paper", "scan_count": 0}
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if path == "/api/brain":
            self._send(200, json.dumps(read_brain()).encode(), "application/json")
            return
        if path == "/api/journal":
            self._send(200, json.dumps(read_journal()).encode(), "application/json")
            return
        if path in ("/", "/index.html"):
            html = HTML_PATH.read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/brain":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 20000:
            self._send(400, b"bad body", "text/plain")
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            self._send(400, b"bad json", "text/plain")
            return
        saved = write_brain(data if isinstance(data, dict) else {})
        self._send(200, json.dumps(saved).encode(), "application/json")


def serve() -> int:
    for port in range(8765, 8775):
        try:
            httpd = ThreadingHTTPServer((HOST, port), Handler)
        except OSError:
            continue
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return port
    raise SystemExit("no free port in 8765-8774")


def main() -> None:
    global ENGINE, HTTP_PORT
    ENGINE = Engine(load_config())
    HTTP_PORT = serve()
    PORT_PATH.write_text(str(HTTP_PORT), encoding="utf-8")
    print(f"ARB paper scanner  http://{HOST}:{HTTP_PORT}", flush=True)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("stopped", flush=True)


if __name__ == "__main__":
    main()
