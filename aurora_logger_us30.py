"""
aurora_logger_us30.py
======================
Logheaza semnalele Aurora pe US30 si verifica acuratetea
dupa 15min, 30min, 60min.

CSV output: aurora_log_US30_YYYYMMDD.csv
Coloane:
  timestamp, price, bias, mode, str, fast_bias, slow_bias,
  price_15m, price_30m, price_60m,
  result_15m, result_30m, result_60m   (CORRECT/WRONG/NEUTRAL)
"""

import time
import csv
import os
import sys
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from collections import deque

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
BARS      = 60
LOG_DIR   = "."
# ══════════════════════════════════════════════════════════════════

CSV_HEADERS = [
    "timestamp", "price",
    "bias", "mode", "str",
    "fast_bias", "slow_bias",
    "price_15m", "price_30m", "price_60m",
    "result_15m", "result_30m", "result_60m",
]

# Coada de semnale asteptand completare
# fiecare entry: {row, due_15, due_30, due_60, done_15, done_30, done_60}
_pending = deque()


def get_csv_path():
    date_str = datetime.now().strftime("%Y%m%d")
    return os.path.join(LOG_DIR, f"aurora_log_US30_{date_str}.csv")


def write_row(row):
    path   = get_csv_path()
    is_new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def update_row(row):
    """Rescrie randuri existente cu price_15m/30m/60m completate."""
    path = get_csv_path()
    if not os.path.exists(path):
        return
    rows = []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r["timestamp"] == row["timestamp"]:
                rows.append(row)
            else:
                rows.append(r)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def get_current_price():
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick:
        return round((tick.ask + tick.bid) / 2, 1)
    return None


def get_bars():
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, BARS)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df


def result_label(bias, price_entry, price_future):
    """CORRECT daca pretul a mers in directia bias-ului."""
    if price_future is None or bias == "NEUTRAL":
        return "NEUTRAL"
    diff = price_future - price_entry
    if bias == "LONG":
        return "CORRECT" if diff > 0 else "WRONG"
    if bias == "SHORT":
        return "CORRECT" if diff < 0 else "WRONG"
    return "NEUTRAL"


def check_pending(now):
    """Verifica daca au trecut 15/30/60 min pentru semnalele in asteptare."""
    price_now = get_current_price()
    if price_now is None:
        return

    for entry in _pending:
        if entry["done_15"] and entry["done_30"] and entry["done_60"]:
            continue

        if not entry["done_15"] and now >= entry["due_15"]:
            entry["row"]["price_15m"]  = price_now
            entry["row"]["result_15m"] = result_label(
                entry["row"]["bias"], float(entry["row"]["price"]), price_now)
            entry["done_15"] = True
            print(f"  [+15m] {entry['row']['timestamp']} "
                  f"bias={entry['row']['bias']} "
                  f"entry={entry['row']['price']} now={price_now} "
                  f"→ {entry['row']['result_15m']}")

        if not entry["done_30"] and now >= entry["due_30"]:
            entry["row"]["price_30m"]  = price_now
            entry["row"]["result_30m"] = result_label(
                entry["row"]["bias"], float(entry["row"]["price"]), price_now)
            entry["done_30"] = True
            print(f"  [+30m] {entry['row']['timestamp']} "
                  f"→ {entry['row']['result_30m']}")

        if not entry["done_60"] and now >= entry["due_60"]:
            entry["row"]["price_60m"]  = price_now
            entry["row"]["result_60m"] = result_label(
                entry["row"]["bias"], float(entry["row"]["price"]), price_now)
            entry["done_60"] = True
            print(f"  [+60m] {entry['row']['timestamp']} "
                  f"→ {entry['row']['result_60m']}")
            # Complet — update CSV
            update_row(entry["row"])


def main():
    print("=" * 70)
    print("  AURORA LOGGER US30 — acuratete 15m/30m/60m")
    print("=" * 70)

    if not mt5.initialize():
        print(f"[ERROR] MT5 init failed: {mt5.last_error()}")
        return

    info = mt5.symbol_info(SYMBOL)
    if info is None:
        print(f"[ERROR] Simbolul {SYMBOL} nu e disponibil")
        mt5.shutdown()
        return

    print(f"[OK] {SYMBOL} conectat")
    print(f"[LOG] {get_csv_path()}\n")

    reset_hysteresis()

    last_bar_time = None

    try:
        while True:
            now = datetime.now()

            # Verifica completari 15/30/60m
            check_pending(now)

            # Citeste date
            df = get_bars()
            if df is None or len(df) < 20:
                time.sleep(5)
                continue

            last_time = df["time"].iloc[-1]
            if last_time == last_bar_time:
                time.sleep(5)
                continue
            last_bar_time = last_time

            # Aurora
            sig = get_aurora_signal(df)
            if sig is None:
                time.sleep(5)
                continue

            price = get_current_price()
            if price is None:
                time.sleep(5)
                continue

            ts = now.strftime("%Y-%m-%d %H:%M:%S")

            row = {
                "timestamp":  ts,
                "price":      price,
                "bias":       sig["bias"],
                "mode":       sig["mode"],
                "str":        round(float(sig["strength"]), 2),
                "fast_bias":  sig["fast_bias"],
                "slow_bias":  sig["slow_bias"],
                "price_15m":  "",
                "price_30m":  "",
                "price_60m":  "",
                "result_15m": "",
                "result_30m": "",
                "result_60m": "",
            }

            # Print consola
            arrow = {"LONG": "↑", "SHORT": "↓", "NEUTRAL": "—"}.get(sig["bias"], "?")
            print(f"[{ts[11:]}] ${price:>9.1f} | "
                  f"{arrow} {sig['bias']:<7} {sig['mode']:<14} "
                  f"str={sig['strength']:>4.1f} | "
                  f"fast={sig['fast_bias']:<7} slow={sig['slow_bias']}")

            # Salveaza in CSV
            write_row(row)

            # Adauga in pending pentru verificare viitoare
            _pending.append({
                "row":    row,
                "due_15": now + timedelta(minutes=15),
                "due_30": now + timedelta(minutes=30),
                "due_60": now + timedelta(minutes=60),
                "done_15": False,
                "done_30": False,
                "done_60": False,
            })

            # Curata pending vechi (>90 min)
            while _pending and (now - _pending[0]["due_60"]).total_seconds() > 30*60:
                _pending.popleft()

            time.sleep(5)

    except KeyboardInterrupt:
        print("\n[STOP] Oprit.")
        print(f"[LOG] Date salvate in {get_csv_path()}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
