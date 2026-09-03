"""
trade_journal_us30.py
======================
Logheaza detalii complete despre fiecare trade si semnal Aurora pe US30.

Doua fisiere CSV output:
  1. trades_journal_US30_YYYYMMDD.csv  — un rand per trade (POS1/POS2)
  2. signals_journal_US30_YYYYMMDD.csv — un rand per bara (semnal + context)

Ruleaza in paralel cu live_bot_us30.py — citeste pozitiile din MT5.
"""

import time
import csv
import os
import sys
import numpy as np
import pandas as pd
from datetime import datetime

try:
    import MetaTrader5 as mt5
except ImportError:
    print("[ERROR] pip install MetaTrader5")
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai"))

from ai.aurora_signal_bridge import get_aurora_signal, reset_hysteresis

# ══════════════════════════════════════════════════════════════════
SYMBOL    = "US30"
TIMEFRAME = mt5.TIMEFRAME_M5
BARS      = 100
LOG_DIR   = "."

# Praguri entry (trebuie sa fie identice cu live_bot_us30.py)
ENTRY_MIN_STR    = 4.5
ENTRY_MODES      = {"EARLY_TREND", "NORMAL", "MOMENTUM", "SLOW_TREND"}
ENTRY_MAX_SPREAD = 5.0
# ══════════════════════════════════════════════════════════════════

TRADE_HEADERS = [
    "timestamp_open", "timestamp_close",
    "ticket", "pos_type", "signal",
    "price_open", "price_close",
    "sl", "lot",
    "entry_str", "entry_mode", "entry_fast", "entry_slow",
    "exit_reason", "exit_profit", "peak_profit",
    "bars_in_trade",
    "MFE", "MAE",
    "atr_at_entry", "spread_at_entry",
]

SIGNAL_HEADERS = [
    "timestamp", "price",
    "fast_bias", "slow_bias",
    "m5_dir", "mode", "str",
    "entry_allowed", "reason_blocked",
    "atr", "spread",
    # Signal tracking
    "signal_direction", "signal_start_time", "signal_start_price",
    "signal_bars_active", "max_str_seen",
    "max_price_reached", "min_price_reached",
    # Active trade context
    "pos1_open", "pos1_profit", "pos1_peak",
    "pos2_open", "pos2_profit", "pos2_peak",
]


def get_csv_path(prefix):
    date_str = datetime.now().strftime("%Y%m%d")
    return os.path.join(LOG_DIR, f"{prefix}_US30_{date_str}.csv")


def write_csv(path, headers, row):
    is_new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if is_new:
            writer.writeheader()
        writer.writerow({h: row.get(h, "") for h in headers})


def update_trade_row(path, ticket, updates):
    """Update an existing trade row by ticket."""
    if not os.path.exists(path):
        return
    rows = []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r["ticket"] == str(ticket):
                r.update({k: str(v) for k, v in updates.items()})
            rows.append(r)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def get_bars():
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, BARS)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df


def calc_atr(df, period=14):
    high  = df["high"].values
    low   = df["low"].values
    close = df["close"].values
    tr = np.maximum(high - low,
         np.maximum(abs(high - np.roll(close, 1)),
                    abs(low  - np.roll(close, 1))))
    tr[0] = high[0] - low[0]
    return round(float(np.mean(tr[-period:])), 2)


def get_spread():
    tick = mt5.symbol_info_tick(SYMBOL)
    return round(abs(tick.ask - tick.bid), 2) if tick else 999.0


def get_price():
    tick = mt5.symbol_info_tick(SYMBOL)
    return round((tick.ask + tick.bid) / 2, 1) if tick else None


def check_entry_allowed(sig, atr, spread):
    """Reproduce logica _can_enter() din bot."""
    if sig is None:
        return False, "NO_SIGNAL"
    if sig["bias"] == "NEUTRAL":
        return False, "NEUTRAL_BIAS"
    if sig["mode"] not in ENTRY_MODES:
        return False, f"MODE_{sig['mode']}"
    if sig["strength"] < ENTRY_MIN_STR:
        return False, f"STR_LOW({round(sig['strength'],1)})"
    if spread > ENTRY_MAX_SPREAD:
        return False, f"SPREAD_HIGH({spread})"
    if atr <= 0:
        return False, "ATR_ZERO"
    return True, ""


def get_positions():
    """Returneaza pozitiile deschise pe SYMBOL."""
    positions = mt5.positions_get(symbol=SYMBOL)
    if positions is None:
        return []
    return list(positions)


def get_closed_deals(from_time, to_time):
    """Returneaza deal-urile inchise intr-un interval."""
    history = mt5.history_deals_get(from_time, to_time)
    if history is None:
        return []
    return [d for d in history if d.symbol == SYMBOL and d.entry == mt5.DEAL_ENTRY_OUT]


def main():
    print("=" * 70)
    print("  TRADE JOURNAL US30")
    print(f"  Entry conditions: str>={ENTRY_MIN_STR} | modes={ENTRY_MODES}")
    print("=" * 70)

    if not mt5.initialize():
        print(f"[ERROR] MT5: {mt5.last_error()}")
        return

    info = mt5.symbol_info(SYMBOL)
    if info is None:
        print(f"[ERROR] {SYMBOL} nu e disponibil")
        mt5.shutdown()
        return

    print(f"[OK] {SYMBOL} | digits={info.digits}")
    print(f"[LOG] Trades: {get_csv_path('trades_journal')}")
    print(f"[LOG] Signals: {get_csv_path('signals_journal')}\n")

    reset_hysteresis()

    last_bar_time     = None
    known_tickets     = set()
    closed_tickets    = set()

    # Signal tracking state
    sig_state = {
        "direction":    None,
        "start_time":   None,
        "start_price":  None,
        "bars_active":  0,
        "max_str":      0.0,
        "max_price":    None,
        "min_price":    None,
    }

    # Trade tracking: ticket -> {entry data + running stats}
    active_trades = {}

    try:
        while True:
            df = get_bars()
            if df is None or len(df) < 20:
                time.sleep(5)
                continue

            last_time = df["time"].iloc[-1]
            if last_time == last_bar_time:
                # Intre bare — updateaza profit live pentru pozitii deschise
                positions = get_positions()
                for pos in positions:
                    t = pos.ticket
                    if t in active_trades:
                        profit = float(pos.profit)
                        if profit > active_trades[t]["peak_profit"]:
                            active_trades[t]["peak_profit"] = profit
                        if profit > active_trades[t]["MFE"]:
                            active_trades[t]["MFE"] = profit
                        if profit < active_trades[t]["MAE"]:
                            active_trades[t]["MAE"] = profit
                time.sleep(3)
                continue

            last_bar_time = last_time
            now      = datetime.now()
            ts       = now.strftime("%Y-%m-%d %H:%M:%S")
            atr      = calc_atr(df)
            spread   = get_spread()
            price    = get_price()
            sig      = get_aurora_signal(df)
            allowed, reason = check_entry_allowed(sig, atr, spread)

            if sig is None:
                time.sleep(5)
                continue

            # ── Signal tracking ───────────────────────────────────
            bias = sig["bias"]
            if bias != "NEUTRAL":
                if sig_state["direction"] != bias:
                    # Semnal nou
                    sig_state["direction"]   = bias
                    sig_state["start_time"]  = ts
                    sig_state["start_price"] = price
                    sig_state["bars_active"] = 1
                    sig_state["max_str"]     = sig["strength"]
                    sig_state["max_price"]   = price
                    sig_state["min_price"]   = price
                else:
                    sig_state["bars_active"] += 1
                    if sig["strength"] > sig_state["max_str"]:
                        sig_state["max_str"] = sig["strength"]
                    if price and price > sig_state["max_price"]:
                        sig_state["max_price"] = price
                    if price and price < sig_state["min_price"]:
                        sig_state["min_price"] = price
            else:
                sig_state["direction"] = None

            # ── Pozitii deschise ─────────────────────────────────
            positions = get_positions()
            pos_map = {p.ticket: p for p in positions}

            pos1 = next((p for p in positions if "POS1" in (p.comment or "")), None)
            pos2 = next((p for p in positions if "POS2" in (p.comment or "")), None)

            # ── Detecteaza trades NOI ─────────────────────────────
            for pos in positions:
                t = pos.ticket
                if t not in known_tickets:
                    known_tickets.add(t)
                    # Determina pos_type din comment
                    comment  = pos.comment or ""
                    pos_type = "POS1" if "POS1" in comment else ("POS2" if "POS2" in comment else "UNKNOWN")
                    signal   = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"

                    active_trades[t] = {
                        "ticket":       t,
                        "pos_type":     pos_type,
                        "signal":       signal,
                        "ts_open":      ts,
                        "price_open":   round(float(pos.price_open), 2),
                        "sl":           round(float(pos.sl), 2),
                        "lot":          float(pos.volume),
                        "entry_str":    sig["strength"],
                        "entry_mode":   sig["mode"],
                        "entry_fast":   sig["fast_bias"],
                        "entry_slow":   sig["slow_bias"],
                        "atr_entry":    atr,
                        "spread_entry": spread,
                        "bars":         0,
                        "peak_profit":  0.0,
                        "MFE":          0.0,
                        "MAE":          0.0,
                    }

                    # Scrie rand initial in trades journal
                    write_csv(get_csv_path("trades_journal"), TRADE_HEADERS, {
                        "timestamp_open":  ts,
                        "ticket":          t,
                        "pos_type":        pos_type,
                        "signal":          signal,
                        "price_open":      round(float(pos.price_open), 2),
                        "sl":              round(float(pos.sl), 2),
                        "lot":             float(pos.volume),
                        "entry_str":       sig["strength"],
                        "entry_mode":      sig["mode"],
                        "entry_fast":      sig["fast_bias"],
                        "entry_slow":      sig["slow_bias"],
                        "atr_at_entry":    atr,
                        "spread_at_entry": spread,
                    })
                    print(f"  [TRADE OPEN] {pos_type} {signal} @ {pos.price_open} str={sig['strength']} mode={sig['mode']}")

            # ── Updateaza bars_in_trade ───────────────────────────
            for t in list(active_trades.keys()):
                if t in pos_map:
                    active_trades[t]["bars"] += 1
                    profit = float(pos_map[t].profit)
                    if profit > active_trades[t]["peak_profit"]:
                        active_trades[t]["peak_profit"] = profit
                    if profit > active_trades[t]["MFE"]:
                        active_trades[t]["MFE"] = profit
                    if profit < active_trades[t]["MAE"]:
                        active_trades[t]["MAE"] = profit

            # ── Detecteaza trades INCHISE ─────────────────────────
            from_ts = int((now.timestamp()) - 3600 * 12)
            to_ts   = int(now.timestamp()) + 1
            from_dt = datetime.fromtimestamp(from_ts)
            to_dt   = datetime.fromtimestamp(to_ts)
            closed_deals = get_closed_deals(from_dt, to_dt)

            for deal in closed_deals:
                t_ref = deal.position_id
                if t_ref in closed_tickets:
                    continue
                if t_ref not in active_trades:
                    continue

                closed_tickets.add(t_ref)
                td = active_trades[t_ref]

                exit_profit = round(float(deal.profit), 2)
                exit_reason = deal.comment or "UNKNOWN"

                update_trade_row(get_csv_path("trades_journal"), t_ref, {
                    "timestamp_close": ts,
                    "price_close":     round(float(deal.price), 2),
                    "exit_reason":     exit_reason,
                    "exit_profit":     exit_profit,
                    "peak_profit":     round(td["peak_profit"], 2),
                    "bars_in_trade":   td["bars"],
                    "MFE":             round(td["MFE"], 2),
                    "MAE":             round(td["MAE"], 2),
                })
                print(f"  [TRADE CLOSE] #{t_ref} {td['pos_type']} profit={exit_profit} reason={exit_reason} bars={td['bars']} peak={round(td['peak_profit'],2)}")
                del active_trades[t_ref]

            # ── Scrie rand signal ─────────────────────────────────
            signal_row = {
                "timestamp":         ts,
                "price":             price,
                "fast_bias":         sig["fast_bias"],
                "slow_bias":         sig["slow_bias"],
                "m5_dir":            sig["bias"],
                "mode":              sig["mode"],
                "str":               round(float(sig["strength"]), 2),
                "entry_allowed":     allowed,
                "reason_blocked":    reason,
                "atr":               atr,
                "spread":            spread,
                "signal_direction":  sig_state["direction"] or "",
                "signal_start_time": sig_state["start_time"] or "",
                "signal_start_price":sig_state["start_price"] or "",
                "signal_bars_active":sig_state["bars_active"],
                "max_str_seen":      sig_state["max_str"],
                "max_price_reached": sig_state["max_price"] or "",
                "min_price_reached": sig_state["min_price"] or "",
                "pos1_open":         1 if pos1 else 0,
                "pos1_profit":       round(float(pos1.profit), 2) if pos1 else "",
                "pos1_peak":         round(active_trades.get(pos1.ticket if pos1 else 0, {}).get("peak_profit", 0), 2) if pos1 else "",
                "pos2_open":         1 if pos2 else 0,
                "pos2_profit":       round(float(pos2.profit), 2) if pos2 else "",
                "pos2_peak":         round(active_trades.get(pos2.ticket if pos2 else 0, {}).get("peak_profit", 0), 2) if pos2 else "",
            }
            write_csv(get_csv_path("signals_journal"), SIGNAL_HEADERS, signal_row)

            # Print consola
            arrow = {"LONG": "↑", "SHORT": "↓"}.get(sig["bias"], "—")
            lock  = "✓" if allowed else f"✗ {reason}"
            print(f"[{ts[11:]}] ${price:>9.1f} | {arrow} {sig['bias']:<7} {sig['mode']:<14} "
                  f"str={sig['strength']:>4.1f} | ATR={atr:>6.1f} | entry={lock}")

            time.sleep(5)

    except KeyboardInterrupt:
        print("\n[STOP] Oprit.")
        print(f"[LOG] Trades: {get_csv_path('trades_journal')}")
        print(f"[LOG] Signals: {get_csv_path('signals_journal')}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
