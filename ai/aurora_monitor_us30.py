"""
aurora_monitor_us30.py
=======================
Monitoring Aurora pe US30 (si optional XAUUSD pentru comparatie).
Fara trades. Doar observatie si loguri CSV.

Output:
  - aurora_monitor_US30_YYYYMMDD.csv
  - aurora_monitor_XAUUSD_YYYYMMDD.csv  (daca COMPARE_XAUUSD = True)
  - print in consola la fiecare bara noua

Ruleaza continuu pana la Ctrl+C.
"""

import time
import csv
import os
import sys
import numpy as np
import pandas as pd
from datetime import datetime

# ── Incearca sa importeze MT5 ─────────────────────────────────────
try:
    import MetaTrader5 as mt5
except ImportError:
    print("[ERROR] MetaTrader5 nu e instalat. Ruleaza: pip install MetaTrader5")
    sys.exit(1)

# ── Importa Aurora ───────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_signal_bridge_v54 import get_aurora_signal, reset_hysteresis

# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════
SYMBOLS         = ["US30"]          # adauga "XAUUSD" pentru comparatie
TIMEFRAME       = mt5.TIMEFRAME_M5
BARS_NEEDED     = 60                # bare necesare pentru Aurora + ATR
LOOP_SLEEP_SEC  = 5                 # verifica la fiecare 5 secunde
LOG_DIR         = "."               # folderul unde se salveaza CSV-urile

# ══════════════════════════════════════════════════════════════════
# ANALIZA PRAGURI HARDCODATE IN AURORA
# ══════════════════════════════════════════════════════════════════
AURORA_HARDCODED = {
    "vol_threshold_low":    0.2,    # signal_ratio < 1.1 → NOISE (bridge)
    "vol_low_vol":          0.2,    # volatility < 0.2 → LOW_VOL
    "trend_filter_min":     3.0,    # max(3.0, volatility*1.5) in bridge
    "trend_filter_min_core":5.0,    # max(5.0, volatility*1.5) in core
    "norm_target":          1.5,    # 1.5 / daily_vol → norm_factor
    "norm_clip_min":        0.5,
    "norm_clip_max":        2.0,
    "hysteresis_n":         3,
    "fake_rev_cooldown_sec":60,
}


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════
def connect_mt5():
    if not mt5.initialize():
        print(f"[ERROR] MT5 initialize() failed: {mt5.last_error()}")
        return False
    print(f"[OK] MT5 conectat — versiune {mt5.version()}")
    return True


def get_bars(symbol, n=BARS_NEEDED):
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, n)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.rename(columns={"close": "close", "open": "open",
                        "high": "high",  "low":  "low"}, inplace=True)
    return df


def calc_atr(df, period=14):
    high = df["high"].values
    low  = df["low"].values
    close = df["close"].values
    tr = np.maximum(high - low,
         np.maximum(abs(high - np.roll(close, 1)),
                    abs(low  - np.roll(close, 1))))
    tr[0] = high[0] - low[0]
    atr = np.convolve(tr, np.ones(period)/period, mode="valid")
    return round(float(atr[-1]), 2)


def calc_move(df, n=1):
    """Move din ultimele n bare (close[-1] - close[-(n+1)])."""
    prices = df["close"].values
    if len(prices) < n + 1:
        return 0.0
    return round(float(prices[-1] - prices[-(n+1)]), 4)


def str_bucket(s):
    """Clasificare str in bucket pentru distributie."""
    if s == 0:   return "0"
    if s < 3:    return "1-2"
    if s < 5:    return "3-4"
    if s < 7:    return "5-6"
    if s < 9:    return "7-8"
    return "9+"


def analyze_aurora_for_symbol(symbol, df):
    """Ruleaza Aurora si returneaza dict cu toate campurile."""
    sig = get_aurora_signal(df)
    if sig is None:
        return None

    prices = df["close"].values
    atr    = calc_atr(df)
    move1  = calc_move(df, 1)
    move3  = calc_move(df, 3)

    # Calcule interne pentru debugging praguri
    diffs      = np.diff(prices[-5:])
    volatility = float(np.std(diffs)) if len(diffs) > 0 else 0.0
    noise      = float(np.mean(np.abs(diffs))) + 1e-6
    trend_str  = float(np.sum(diffs))
    signal_ratio = abs(trend_str) / noise

    daily_vol  = float(np.std(np.diff(prices[-50:]))) if len(prices) >= 51 else 0.0
    norm_factor = float(np.clip(1.5 / daily_vol, 0.5, 2.0)) if daily_vol > 0.01 else 1.0
    trend_filter = max(3.0, volatility * 1.5)

    return {
        "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol":        symbol,
        "price":         round(float(prices[-1]), 2),
        "bias":          sig["bias"],
        "mode":          sig["mode"],
        "strength":      round(float(sig["strength"]), 2),
        "str_bucket":    str_bucket(sig["strength"]),
        "fast_bias":     sig["fast_bias"],
        "fast_mode":     sig["fast_mode"],
        "slow_bias":     sig["slow_bias"],
        "slow_mode":     sig["slow_mode"],
        "confidence":    sig["confidence"],
        "atr":           atr,
        "move1":         move1,
        "move3":         move3,
        # Internals pentru analiza praguri
        "volatility_5b": round(volatility, 4),
        "signal_ratio":  round(signal_ratio, 3),
        "trend_str_5b":  round(trend_str, 4),
        "trend_filter":  round(trend_filter, 3),
        "daily_vol_50b": round(daily_vol, 4),
        "norm_factor":   round(norm_factor, 3),
        "str_raw_prenorm": round(float(sig.get("fast_str", 0)), 2),
    }


def get_csv_path(symbol):
    date_str = datetime.now().strftime("%Y%m%d")
    return os.path.join(LOG_DIR, f"aurora_monitor_{symbol}_{date_str}.csv")


CSV_HEADERS = [
    "timestamp", "symbol", "price",
    "bias", "mode", "strength", "str_bucket",
    "fast_bias", "fast_mode", "slow_bias", "slow_mode", "confidence",
    "atr", "move1", "move3",
    "volatility_5b", "signal_ratio", "trend_str_5b",
    "trend_filter", "daily_vol_50b", "norm_factor", "str_raw_prenorm",
]


def write_csv(path, row):
    is_new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


# ══════════════════════════════════════════════════════════════════
# DISTRIBUTIE STR — tracking in-memory
# ══════════════════════════════════════════════════════════════════
_str_dist: dict = {sym: {} for sym in SYMBOLS}
_total_bars: dict = {sym: 0 for sym in SYMBOLS}


def update_dist(symbol, row):
    bucket = row["str_bucket"]
    _str_dist[symbol][bucket] = _str_dist[symbol].get(bucket, 0) + 1
    _total_bars[symbol] += 1


def print_dist(symbol):
    total = _total_bars[symbol]
    if total == 0:
        return
    dist  = _str_dist[symbol]
    print(f"\n  [{symbol}] Distributie STR dupa {total} bare:")
    for bucket in ["0", "1-2", "3-4", "5-6", "7-8", "9+"]:
        cnt = dist.get(bucket, 0)
        pct = cnt / total * 100
        bar = "█" * int(pct / 5)
        print(f"    str {bucket:>4}: {cnt:>4} ({pct:>5.1f}%)  {bar}")


# ══════════════════════════════════════════════════════════════════
# PRINT ROW
# ══════════════════════════════════════════════════════════════════
def print_row(row):
    bias_color = {"LONG": "↑", "SHORT": "↓", "NEUTRAL": "—"}.get(row["bias"], "?")
    print(
        f"  [{row['timestamp'][11:]}] {row['symbol']:<6} "
        f"${row['price']:>9.2f} | "
        f"{bias_color} {row['bias']:<7} {row['mode']:<14} "
        f"str={row['strength']:>5.1f} ({row['str_bucket']:>3}) | "
        f"ATR={row['atr']:>6.2f} mv1={row['move1']:>+8.2f} mv3={row['move3']:>+8.2f} | "
        f"vol={row['volatility_5b']:>7.3f} sig={row['signal_ratio']:>5.2f} "
        f"nrm={row['norm_factor']:>4.2f}"
    )


# ══════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════
def main():
    print("=" * 80)
    print("  AURORA MONITOR — US30 vs XAUUSD")
    print(f"  Simboluri: {SYMBOLS}")
    print(f"  Timeframe: M5  |  Loop: {LOOP_SLEEP_SEC}s")
    print("=" * 80)

    # Afisare praguri hardcodate
    print("\n  [AURORA] Praguri hardcodate:")
    for k, v in AURORA_HARDCODED.items():
        print(f"    {k:<30} = {v}")
    print()

    if not connect_mt5():
        return

    # Verifica simbolurile
    for sym in SYMBOLS:
        info = mt5.symbol_info(sym)
        if info is None:
            print(f"[WARN] Simbolul {sym} nu e disponibil in MT5")
        else:
            print(f"[OK] {sym} — digits={info.digits} "
                  f"point={info.point} spread={info.spread}")

    print("\n  Pornit. Ctrl+C pentru oprire.\n")

    last_bar_time = {sym: None for sym in SYMBOLS}
    bar_count     = {sym: 0 for sym in SYMBOLS}

    reset_hysteresis()

    try:
        while True:
            for sym in SYMBOLS:
                df = get_bars(sym)
                if df is None or len(df) < 20:
                    print(f"  [{sym}] Nu am date suficiente")
                    continue

                # Proceseaza doar la bara noua
                last_time = df["time"].iloc[-1]
                if last_time == last_bar_time[sym]:
                    continue
                last_bar_time[sym] = last_time

                row = analyze_aurora_for_symbol(sym, df)
                if row is None:
                    continue

                bar_count[sym] += 1
                update_dist(sym, row)
                print_row(row)

                # Salveaza CSV
                csv_path = get_csv_path(sym)
                write_csv(csv_path, row)

                # Afiseaza distributie la fiecare 50 bare
                if bar_count[sym] % 50 == 0:
                    print_dist(sym)
                    print(f"  [LOG] Salvat in {csv_path}")

            time.sleep(LOOP_SLEEP_SEC)

    except KeyboardInterrupt:
        print("\n\n  [STOP] Oprit de utilizator.")
        for sym in SYMBOLS:
            print_dist(sym)
            csv_path = get_csv_path(sym)
            print(f"  [LOG] Date salvate in {csv_path}")

    finally:
        mt5.shutdown()
        print("  [MT5] Deconectat.")


if __name__ == "__main__":
    main()
