"""Hermes strategy assistant helpers — paper only, no dollar amounts."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CHAT_PATH = ROOT / "chat.jsonl"
_MONEY = re.compile(r"\$[\d,]+(?:\.\d+)?")
_PATH = re.compile(r"([A-Za-z]:\\[^\s\"']+|/[A-Za-z]/Users/[^\s\"']+)")


def public_text(value: str) -> str:
    text = _PATH.sub("", _MONEY.sub("", value or ""))
    return re.sub(r"\s{2,}", " ", text).strip()


def _pct(x: Any) -> str:
    try:
        n = float(x) * 100
    except (TypeError, ValueError):
        return "—"
    return f"{'+' if n > 0 else ''}{n:.2f}%"


def read_chat(limit: int = 80) -> list[dict]:
    if not CHAT_PATH.exists():
        return []
    out: list[dict] = []
    for line in CHAT_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("role") in ("user", "assistant", "system"):
            out.append(
                {
                    "role": row["role"],
                    "text": public_text(str(row.get("text") or ""))[:2000],
                    "ts": row.get("ts") or 0,
                    "source": row.get("source") or "",
                }
            )
    return out


def append_chat(role: str, text: str, source: str = "") -> dict:
    row = {
        "role": role,
        "text": public_text(text)[:2000],
        "ts": time.time(),
        "source": source,
    }
    with CHAT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def build_strategy(state: dict, brain: dict, journal: list | None = None) -> dict:
    """Live tips, recommendations, and edge notes from scanners + brain."""
    journal = journal or []
    trusted = state.get("trusted") or []
    recs = state.get("recommendations") or []
    numbers = state.get("best_numbers") or []
    majors = state.get("majors") or []
    episodes = state.get("episodes") or []
    call = brain.get("call") or "waiting"
    tips: list[str] = []
    recommendations: list[dict] = []
    edges: list[dict] = []
    watchlist: list[str] = []

    # Posture
    if call == "look" and recs:
        posture = "GO — Hermes call is look on a takeable ARB route."
    elif call == "skip":
        posture = "STAND DOWN — Hermes call is skip. Do not force size."
    else:
        posture = "WATCH — paper mode. Scanners run; Hermes has not cleared a take."

    tips.append(
        "Hermes is the only brain. Scanners surface numbers; Hermes decides look / watch / skip. Paper only — no orders."
    )

    if not recs:
        tips.append(
            "No Best chance right now. Big leftovers without same coin + open rails + paper inventory are Best number only."
        )
    else:
        for r in recs[:3]:
            left = r.get("book_net") if r.get("book_net") is not None else r.get("net")
            recommendations.append(
                {
                    "kind": "arb_chance",
                    "title": f"{r.get('pair')} · chance",
                    "detail": public_text(
                        f"Buy {r.get('buy')} · sell {r.get('sell')} · left {_pct(left)}. {r.get('reason') or 'Rails + inventory clear.'}"
                    ),
                    "priority": "high",
                }
            )
            tips.append(
                f"Focus inventory on the sell venue for {r.get('pair')} and quote on the buy venue before the gap dies."
            )

    for r in numbers[:4]:
        left = r.get("book_net") if r.get("book_net") is not None else r.get("net")
        why = r.get("reason") or "Not a chance."
        if r.get("same_coin") is False:
            why = "Different contract under the same ticker."
        elif r.get("movable") is False:
            why = "Cannot move the coin across venues."
        elif r.get("inventory_ok") is False:
            why = r.get("inventory_note") or "Paper inventory cannot take it."
        edges.append(
            {
                "engine": "arb",
                "pair": r.get("pair"),
                "label": "best number · not a chance",
                "left": _pct(left),
                "why": public_text(why)[:160],
            }
        )
        if r.get("same_coin") is False:
            tips.append(f"{r.get('pair')}: ticker match is not same coin — never treat as edge.")
        elif r.get("movable") is False or r.get("last_death") == "blocked":
            tips.append(f"{r.get('pair')}: blocked or closed rails pattern — wait for a fast death before trusting it.")
        elif r.get("inventory_ok") is False:
            watchlist.append(
                public_text(
                    f"Stage paper inventory for {r.get('pair')}: base on {r.get('sell')}, USDT on {r.get('buy')}."
                )
            )

    sticky = [e for e in episodes if e.get("sticky")]
    if sticky:
        tips.append(
            "A sticky open gap usually means the coin cannot move. Journal death=blocked stays out of Best chance."
        )

    fee_losers = [
        m
        for m in majors
        if (m.get("net") is not None and m.get("net") <= 0)
        or (m.get("book_net") is not None and m.get("book_net") <= 0)
    ]
    if fee_losers:
        tips.append("Majors (BTC ETH SOL etc.) are inside the fee on these venues — never look on fee losers.")

    # Brain skips as strategy notes
    for s in (brain.get("skips") or [])[:4]:
        recommendations.append(
            {
                "kind": "arb_skip",
                "title": f"Skip {s.get('pair')}",
                "detail": public_text(s.get("why") or ""),
                "priority": "med",
            }
        )

    for lesson in (brain.get("lessons") or [])[:6]:
        t = public_text(str(lesson))
        if t and t not in tips:
            tips.append(t)

    # Journal deaths
    deaths = [j for j in journal if j.get("event") == "closed" or j.get("death")]
    blocked = sum(1 for j in deaths[-20:] if j.get("death") == "blocked")
    fast = sum(1 for j in deaths[-20:] if j.get("death") == "fast")
    if blocked or fast:
        tips.append(
            f"Recent closes: {fast} fast (prices were probably real), {blocked} blocked (usually cannot send)."
        )

    recommendations.append(
        {
            "kind": "ops",
            "title": "Next edge lever",
            "detail": (
                "Fill paper inventory on venues that repeatedly show movable same-coin routes, "
                "or keep empty inventory and treat the board as a rails trainer."
            ),
            "priority": "med",
        }
    )

    # de-dupe tips
    seen_t: set[str] = set()
    clean_tips = []
    for t in tips:
        t = public_text(t)[:220]
        if t and t not in seen_t:
            seen_t.add(t)
            clean_tips.append(t)

    return {
        "role": "strategy_assistant",
        "engines": ["arb"],
        "posture": posture,
        "call": call,
        "summary": public_text(brain.get("summary") or "")[:500],
        "tips": clean_tips[:12],
        "recommendations": recommendations[:12],
        "edges": edges[:10],
        "watchlist": [w for w in watchlist if w][:8],
        "quick_prompts": [
            "Why is there no Best chance right now?",
            "What would flip the call to look?",
            "Which sticky pairs should I ignore?",
            "Give me a rails checklist for the top Best number.",
            "What inventory would unlock a chance?",
        ],
        "updated_at": time.time(),
    }


def local_reply(message: str, pack: dict) -> str:
    """Fast fallback if Hermes CLI is unavailable."""
    m = (message or "").lower()
    parts = [
        f"Posture: {pack.get('posture')}",
        f"Call: {pack.get('call')}. {pack.get('summary') or ''}",
    ]
    if "look" in m or "chance" in m or "trade" in m:
        tips = pack.get("tips") or []
        parts.append("Chance filter: same coin + open rails + paper inventory + live book after fees + not sticky.")
        parts.extend(tips[:4])
    elif "invent" in m or "balance" in m:
        wl = pack.get("watchlist") or []
        parts.append("Inventory unlocks Best chance when rails already work.")
        parts.extend(wl[:4] or ["Empty inventory means no takeable spatial chance by design."])
    elif "tip" in m or "edge" in m or "strateg" in m:
        parts.extend((pack.get("tips") or [])[:6])
        for r in (pack.get("recommendations") or [])[:4]:
            parts.append(f"{r.get('title')}: {r.get('detail')}")
    else:
        parts.extend((pack.get("tips") or [])[:5])
        parts.append("Ask about chance, inventory, rails, or what would flip to look.")
    return public_text(" ".join(parts))[:1500]


def hermes_reply(message: str, pack: dict, timeout_s: int = 75) -> tuple[str, str]:
    """Call Hermes CLI as strategy brain. Returns (text, source)."""
    hermes = shutil.which("hermes")
    if not hermes:
        # common Windows install path
        cand = Path.home() / "AppData/Local/hermes/hermes-agent/.hermes/bin/hermes"
        if cand.exists():
            hermes = str(cand)
    if not hermes:
        return local_reply(message, pack), "local"

    compact = {
        "call": pack.get("call"),
        "posture": pack.get("posture"),
        "summary": pack.get("summary"),
        "tips": (pack.get("tips") or [])[:6],
        "recommendations": (pack.get("recommendations") or [])[:6],
        "edges": (pack.get("edges") or [])[:6],
        "watchlist": (pack.get("watchlist") or [])[:4],
    }
    prompt = (
        "You are Hermes, the paper strategy assistant for a local cross-exchange ARB dashboard. "
        "Paper only. No orders. No dollar amounts. No machine paths. "
        "Be direct, tactical, and useful. Use the live pack. User message follows.\n\n"
        f"LIVE_PACK={json.dumps(compact, ensure_ascii=False)}\n\n"
        f"USER: {public_text(message)[:800]}\n\n"
        "Reply in plain text under 180 words. Give a clear posture, 2-4 concrete tips, and what would change the call."
    )
    try:
        proc = subprocess.run(
            [
                hermes,
                "-z",
                prompt,
                "--skills",
                "crypto-arb-brain",
                "--in",
                str(ROOT),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(ROOT),
        )
        out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        out = public_text(out)
        if not out:
            return local_reply(message, pack), "local_fallback"
        return out[:2000], "hermes"
    except (subprocess.TimeoutExpired, OSError, Exception):
        return local_reply(message, pack), "local_timeout"
