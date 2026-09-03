"""
aurora_core.py — v2
====================
Core Aurora analysis engine.
Used directly when feeding raw price windows (not DataFrames).

v2 upgrade: SLOW_TREND mode added.

Identical logic to aurora_signal_bridge.analyze_window() —
kept in sync so both files produce consistent classifications.

Mode hierarchy (top = highest priority):
  REVERSAL      — strong counter-trend after prior trend
  SLOW_TREND    — [NEW v2] consistent grind, low volatility
  EARLY_TREND   — trend building, last bar confirms
  BREAKOUT      — trend confirmed + last bar momentum
  NORMAL        — trend confirmed, no last-bar momentum
  FAKE_REVERSAL — 2-bar flip with momentum
  WEAK_TREND    — fallback directional
  NEUTRAL       — no signal
"""

import numpy as np


def analyze_window(window):
    """
    Analyzes a price window dict {"price": array} or direct array.
    Returns (bias, mode, strength).
    """

    # ── PROTECT ──────────────────────────────────────────────────
    if window is None:
        return "NEUTRAL", "NONE", 0

    # Accept both dict {"price": array} and raw array
    if isinstance(window, dict):
        prices = window["price"].values if hasattr(window.get("price"), "values") else np.array(window["price"])
    else:
        prices = np.array(window)

    if len(prices) < 5:
        return "NEUTRAL", "NONE", 0

    # ── RECENT — foloseste toata fereastra primita ────────────────
    recent = prices

    last  = recent[-1]
    prev  = recent[-2]
    prev2 = recent[-3]

    move1 = last  - prev
    move2 = prev  - prev2

    total_move = last - recent[0]

    # ── TREND METRICS ────────────────────────────────────────────
    diffs = np.diff(recent)

    trend_strength = float(np.sum(diffs))
    trend_up       = int(np.sum(diffs > 0))
    trend_down     = int(np.sum(diffs < 0))

    # ── VOLATILITY ───────────────────────────────────────────────
    volatility = float(np.std(diffs))

    # Prag volatilitate adaptat la dimensiunea ferestrei
    vol_threshold = 0.02 if len(prices) <= 5 else 0.15 if len(prices) <= 10 else 0.3
    if volatility < vol_threshold:
        return "NEUTRAL", "NONE", 0

    trend_filter = max(5.0, volatility * 1.5)

    # ── MOMENTUM ─────────────────────────────────────────────────
    momentum = float(move1 + move2)

    # ── REVERSAL ─────────────────────────────────────────────────
    recent_short = prices[-5:]
    recent_move  = float(recent_short[-1] - recent_short[0])
    prev_trend   = float(np.sum(np.diff(prices[:-5]))) if len(prices) > 10 else 0.0
    recent_diffs = np.diff(recent_short)
    recent_up    = int(np.sum(recent_diffs > 0))
    recent_down  = int(np.sum(recent_diffs < 0))

    if (recent_move > volatility * 6 and
            prev_trend < -trend_filter and
            move1 > 0 and move2 > 0 and
            recent_up >= 3):
        return "LONG", "REVERSAL", round(abs(recent_move), 2)

    if (recent_move < -volatility * 6 and
            prev_trend > trend_filter and
            move1 < 0 and move2 < 0 and
            recent_down >= 3):
        return "SHORT", "REVERSAL", round(abs(recent_move), 2)

    # ── [NEW v2] SLOW_TREND ───────────────────────────────────────
    # Consistent directional grind:
    #   - trend crosses 50% of filter (earlier detection than EARLY_TREND)
    #   - high bar consistency (>= 7 of 10 same direction)
    #   - low volatility (< 1.5) — distinguishes from explosive moves
    #   - last bar confirms direction
    #
    # Why before EARLY_TREND:
    #   SLOW_TREND is more specific (requires consistency + low vol).
    #   EARLY_TREND is the fallback for building trends.
    if volatility < 1.5:
        if (trend_strength > trend_filter * 0.5 and
                trend_up >= 7 and move1 > 0):
            return "LONG", "SLOW_TREND", round(abs(trend_strength), 2)

        if (trend_strength < -trend_filter * 0.5 and
                trend_down >= 7 and move1 < 0):
            return "SHORT", "SLOW_TREND", round(abs(trend_strength), 2)

    # ── TREND UP ─────────────────────────────────────────────────
    if trend_strength > trend_filter and trend_up >= trend_down:
        if move1 > 0:
            return "LONG", "BREAKOUT", round(abs(trend_strength), 2)
        return "LONG", "NORMAL", round(abs(trend_strength), 2)

    # ── TREND DOWN ───────────────────────────────────────────────
    if trend_strength < -trend_filter and trend_down >= trend_up:
        if move1 < 0:
            return "SHORT", "BREAKOUT", round(abs(trend_strength), 2)
        return "SHORT", "NORMAL", round(abs(trend_strength), 2)

    # ── FAKE REVERSAL ────────────────────────────────────────────
    # Cooldown NOT applied here — pure analysis only.
    # Cooldown lives in get_aurora_signal() in the bridge.
    if move2 < 0 and move1 > 0 and abs(momentum) > 1:
        return "LONG", "FAKE_REVERSAL", round(abs(momentum), 2)

    if move2 > 0 and move1 < 0 and abs(momentum) > 1:
        return "SHORT", "FAKE_REVERSAL", round(abs(momentum), 2)

    # ── FALLBACK ─────────────────────────────────────────────────
    if total_move > volatility * 2:
        return "LONG", "NORMAL", round(abs(total_move), 2)

    if total_move < -volatility * 2:
        return "SHORT", "NORMAL", round(abs(total_move), 2)

    return "NEUTRAL", "NONE", 0
