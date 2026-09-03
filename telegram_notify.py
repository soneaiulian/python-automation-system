"""
telegram_notify.py — notificari Telegram pentru US30 Bot
Citeste BOT_TOKEN si CHAT_ID din .env din acelasi folder.
"""

import os
import requests
from pathlib import Path
from datetime import datetime

# ── Load .env ─────────────────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
_TOKEN    = None
_CHAT_ID  = None

try:
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                _TOKEN = line.split("=", 1)[1].strip()
            elif line.startswith("TELEGRAM_CHAT_ID="):
                _CHAT_ID = line.split("=", 1)[1].strip()
except Exception:
    pass

def _send(text):
    if not _TOKEN or not _CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
            json={"chat_id": _CHAT_ID, "text": text,
                  "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass


def tg(text):
    _send(text)


def tg_open(pos_type, signal, entry, sl, lot, astr, mode):
    emoji = "🟢" if signal == "BUY" else "🔴"
    _send(
        f"{emoji} <b>OPEN {signal} [{pos_type}]</b>\n"
        f"Entry: <code>{entry}</code>  SL: <code>{sl}</code>\n"
        f"Lot: {lot}  |  str={astr}  mode={mode}"
    )


def tg_close(pos_type, signal, profit, peak, reason):
    emoji = "✅" if profit > 0 else "❌"
    _send(
        f"{emoji} <b>CLOSE [{pos_type}]</b>\n"
        f"Profit: <code>{round(profit,2)}$</code>  Peak: {round(peak,2)}$\n"
        f"Reason: {reason}"
    )


def tg_sl(signal, profit, reason):
    _send(
        f"🛑 <b>SL HIT {signal}</b>\n"
        f"Profit: <code>{round(profit,2)}$</code>\n"
        f"Reason: {reason}"
    )


def tg_decay(ticket, reason):
    _send(f"⚠️ <b>DECAY EXIT</b> #{ticket}\n{reason}")


def tg_daily_summary(balance, pnl, wr, total_trades):
    emoji = "📈" if pnl > 0 else "📉"
    _send(
        f"{emoji} <b>DAILY SUMMARY</b>\n"
        f"Balance: <code>{round(balance,2)}$</code>\n"
        f"PnL: <code>{round(pnl,2)}$</code>\n"
        f"WR: {wr}%  |  Trades: {total_trades}"
    )
