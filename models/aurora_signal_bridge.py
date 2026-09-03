import numpy as np


def analyze_window(prices):

    # ================= PROTECT =================
    if prices is None or len(prices) < 25:
        return "NEUTRAL", "NONE", 0

    recent = prices[-15:]

    last = recent[-1]
    prev = recent[-2]
    prev2 = recent[-3]

    move1 = last - prev
    move2 = prev - prev2

    total_move = last - recent[0]

    diffs = np.diff(recent)

    if len(diffs) == 0:
        return "NEUTRAL", "NONE", 0

    # ================= CORE =================
    trend_strength = np.sum(diffs[-10:])
    trend_up = np.sum(diffs[-10:] > 0)
    trend_down = np.sum(diffs[-10:] < 0)

    volatility = np.std(diffs)

    if volatility < 0.3:
        return "NEUTRAL", "LOW_VOL", 0

    trend_filter = max(5, volatility * 1.5)

    # ================= IMPROVED MOMENTUM =================
    momentum = np.sum(diffs[-3:])

    # ================= NOISE FILTER =================
    noise = np.mean(np.abs(diffs))
    signal_ratio = abs(trend_strength) / (noise + 1e-6)

    if signal_ratio < 1.5:
        return "NEUTRAL", "NOISE", 0

    # ================= SCORING SYSTEM =================
    score = 0

    if abs(trend_strength) > trend_filter:
        score += 3

    if abs(momentum) > volatility:
        score += 2

    if trend_up > trend_down or trend_down > trend_up:
        score += 2

    if volatility > 1:
        score += 1

    if signal_ratio > 2:
        score += 2

    # ================= REVERSAL =================
    recent_short = prices[-5:]
    recent_move = recent_short[-1] - recent_short[0]

    prev_trend = np.sum(np.diff(prices[-20:-5]))

    recent_diffs = np.diff(recent_short)
    recent_up = np.sum(recent_diffs > 0)
    recent_down = np.sum(recent_diffs < 0)

    if (
        recent_move > volatility * 6 and
        prev_trend < -trend_filter and
        move1 > 0 and move2 > 0 and
        recent_up >= 3
    ):
        return "LONG", "REVERSAL", score

    if (
        recent_move < -volatility * 6 and
        prev_trend > trend_filter and
        move1 < 0 and move2 < 0 and
        recent_down >= 3
    ):
        return "SHORT", "REVERSAL", score

    # ================= EARLY TREND =================
    if trend_strength > trend_filter * 0.7 and move1 > 0:
        return "LONG", "EARLY_TREND", score

    if trend_strength < -trend_filter * 0.7 and move1 < 0:
        return "SHORT", "EARLY_TREND", score

    # ================= TREND =================
    if trend_strength > trend_filter and trend_up >= trend_down:

        if move1 > 0:
            return "LONG", "BREAKOUT", score

        return "LONG", "NORMAL", score

    if trend_strength < -trend_filter and trend_down >= trend_up:

        if move1 < 0:
            return "SHORT", "BREAKOUT", score

        return "SHORT", "NORMAL", score

    # ================= FAKE REVERSAL =================
    if move2 < 0 and move1 > 0 and abs(momentum) > volatility:
        return "LONG", "FAKE_REVERSAL", score

    if move2 > 0 and move1 < 0 and abs(momentum) > volatility:
        return "SHORT", "FAKE_REVERSAL", score

    # ================= FALLBACK =================
    if total_move > volatility * 2:
        return "LONG", "WEAK_TREND", score

    if total_move < -volatility * 2:
        return "SHORT", "WEAK_TREND", score

    return "NEUTRAL", "NONE", 0


def get_aurora_signal(df_m5, df_m15):

    # ================= PROTECT M5 =================
    if df_m5 is None or "close" not in df_m5.columns:
        return None

    prices_m5 = df_m5["close"].values

    if len(prices_m5) < 25:
        return None

    # ================= PROTECT M15 =================
    if df_m15 is None or "close" not in getattr(df_m15, "columns", []):
        prices_m15 = prices_m5
    else:
        prices_m15 = df_m15["close"].values

    if len(prices_m15) < 25:
        prices_m15 = prices_m5

    # ================= ANALYSIS =================
    bias_m5, mode_m5, strength_m5 = analyze_window(prices_m5)
    bias_m15, mode_m15, strength_m15 = analyze_window(prices_m15)

    # ================= ALIGNMENT =================
    if bias_m5 == bias_m15 and bias_m5 != "NEUTRAL":
        final_bias = bias_m5
        confidence = "HIGH"
    elif bias_m5 != "NEUTRAL":
        final_bias = bias_m5
        confidence = "NORMAL"
    else:
        final_bias = "NEUTRAL"
        confidence = "WEAK"

    # ================= SAFE VOL =================
    try:
        volatility = float(np.std(np.diff(prices_m5)))
    except:
        volatility = 0

    return {
        "bias": final_bias,
        "mode": mode_m5,
        "strength": float(strength_m5),  # acum este SCORE real
        "confidence": confidence,
        "volatility": volatility,
        "speed": "FAST"
    }