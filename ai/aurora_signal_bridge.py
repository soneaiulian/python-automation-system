"""
aurora_signal_bridge_us30.py — Fast/Slow M5 calibrat pentru US30
================================================================
Identic cu v54 dar cu ajustări pentru US30:

Problema Gold vs US30:
  Gold: o bară M5 = 0.5-2$  → volatility=1-3  → trend_filter=3-5
  US30: o bară M5 = 5-30$   → volatility=5-20 → trend_filter=8-30

Pe US30 când piața pleacă rapid (20-50$ în 1-2 bare):
  - noise = mean(|diffs|) crește la 15-30$
  - trend_strength pe 5 bare = 30-80$
  - signal_ratio = 30/15 = 2.0 → trece filtrul
  
Dar când mișcarea e distribuită (5 bare × 6$ = 30$):
  - noise = 6$, trend_strength = 30$, signal_ratio = 5 → OK
  - DAR trend_filter = max(3, 6*1.5) = 9$ → trend_strength 30 > 9 → OK

Problema REALĂ identificată:
  HYSTERESIS N=3 pe bias final — când FAST alternează 
  LONG/SHORT în consolidare, hysteresis îl ține NEUTRAL.
  Când trendul vine, are nevoie de 3 bare consecutive în 
  aceeași direcție înainte să schimbe bias → 15 minute întârziere.
  
Fix US30:
  1. HYSTERESIS N=2 în loc de N=3 (economisim 1 bară = 5 minute)
  2. signal_ratio threshold: 1.1 → 0.9 (mai sensibil la mișcări directionale)  
  3. trend_filter bazat pe ATR extern când disponibil
  4. EARLY_TREND trigger: 0.7 → 0.5 din trend_filter (mai devreme)
"""

import time
import numpy as np

# ══════════════════════════════════════════════════════════════════
# FAKE REVERSAL COOLDOWN
# ══════════════════════════════════════════════════════════════════
_fake_rev_last: dict = {}
FAKE_REV_COOLDOWN_SEC = 60

# ══════════════════════════════════════════════════════════════════
# HYSTERESIS N=3
# ══════════════════════════════════════════════════════════════════
HYSTERESIS_N        = 2   # US30: N=3 = 15 min întârziere, N=2 = 10 min
_hyst_bias:  str    = "NEUTRAL"
_hyst_mode:  str    = "NONE"
_hyst_str:   float  = 0.0
_hyst_count: int    = 0


# ══════════════════════════════════════════════════════════════════
# CORE ANALYZE — aceeasi logica, fereastra variabila
# ══════════════════════════════════════════════════════════════════
def _analyze(prices):
    """Analizeaza o fereastra de preturi. Returneaza (bias, mode, strength)."""
    if prices is None or len(prices) < 5:
        return "NEUTRAL", "NONE", 0.0

    recent = prices
    last   = recent[-1]
    prev   = recent[-2]
    prev2  = recent[-3] if len(recent) >= 3 else prev

    move1      = last - prev
    move2      = prev - prev2
    total_move = last - recent[0]
    diffs      = np.diff(recent)

    if len(diffs) == 0:
        return "NEUTRAL", "NONE", 0.0

    trend_strength = float(np.sum(diffs))
    trend_up       = int(np.sum(diffs > 0))
    trend_down     = int(np.sum(diffs < 0))
    volatility     = float(np.std(diffs))
    momentum       = float(np.sum(diffs[-3:])) if len(diffs) >= 3 else float(np.sum(diffs))
    noise          = float(np.mean(np.abs(diffs))) + 1e-6
    signal_ratio   = abs(trend_strength) / noise

    # US30: bare M5 de 5-30pt → volatility minima reala e 0.5pt
    # Pragul 0.2 bloca trend-uri prea curate (toate diff-urile egale)
    if volatility < 0.5:
        # Verificam daca exista totusi miscare directonala reala
        if abs(trend_strength) < 3.0:
            return "NEUTRAL", "LOW_VOL", 0.0
        # Miscare directionala cu volatilitate mica = trend curat
        # Nu blocam — continuam analiza

    trend_filter = max(3.0, volatility * 1.3)

    # US30: prag mai permisiv — mișcările rapide au signal_ratio mai mic
    # Gold folosea 1.1, US30 cu bare mai volatile → 0.9
    if signal_ratio < 0.9:
        return "NEUTRAL", "NOISE", 0.0

    score = 0.0
    if abs(trend_strength) > trend_filter:  score += 3
    if abs(momentum) > volatility:          score += 2
    if trend_up > trend_down or trend_down > trend_up: score += 2
    if volatility > 1:                      score += 1
    if signal_ratio > 2:                    score += 2

    # US30: EARLY_TREND mai devreme — 0.5 în loc de 0.7
    # Pe Gold 0.7 era ok, pe US30 cu mișcări de 30-50$ în 2 bare
    # 0.7 înseamnă că trend_strength trebuie să fie 70% din filter
    # înainte să declare EARLY_TREND → prea târziu
    if trend_strength > trend_filter * 0.5 and move1 > 0:
        return "LONG",  "EARLY_TREND", score
    if trend_strength < -trend_filter * 0.5 and move1 < 0:
        return "SHORT", "EARLY_TREND", score

    if trend_strength > trend_filter and trend_up >= trend_down:
        return ("LONG",  "BREAKOUT" if move1 > 0 else "NORMAL", score)
    if trend_strength < -trend_filter and trend_down >= trend_up:
        return ("SHORT", "BREAKOUT" if move1 < 0 else "NORMAL", score)

    if move2 < 0 and move1 > 0 and abs(momentum) > volatility:
        return "LONG",  "FAKE_REVERSAL", score
    if move2 > 0 and move1 < 0 and abs(momentum) > volatility:
        return "SHORT", "FAKE_REVERSAL", score

    if total_move > volatility * 2:
        return "LONG",  "WEAK_TREND", score
    if total_move < -volatility * 2:
        return "SHORT", "WEAK_TREND", score

    return "NEUTRAL", "NONE", 0.0


def _apply_fake_rev_cooldown(bias, mode):
    global _fake_rev_last
    if mode != "FAKE_REVERSAL":
        return mode
    now     = time.time()
    last_ts = _fake_rev_last.get(bias, 0)
    if now - last_ts < FAKE_REV_COOLDOWN_SEC:
        return "EARLY_TREND"
    _fake_rev_last[bias] = now
    return "FAKE_REVERSAL"


def _apply_hysteresis(raw_bias, raw_mode, raw_str):
    global _hyst_bias, _hyst_mode, _hyst_str, _hyst_count

    if _hyst_bias == "NEUTRAL":
        _hyst_bias  = raw_bias
        _hyst_mode  = raw_mode
        _hyst_str   = raw_str
        _hyst_count = 0
    elif raw_bias == _hyst_bias:
        _hyst_mode  = raw_mode
        _hyst_str   = raw_str
        _hyst_count = 0
    else:
        _hyst_count += 1
        if _hyst_count >= HYSTERESIS_N:
            _hyst_bias  = raw_bias
            _hyst_mode  = raw_mode
            _hyst_str   = raw_str
            _hyst_count = 0

    return _hyst_bias, _hyst_mode, _hyst_str


def reset_hysteresis():
    global _hyst_bias, _hyst_mode, _hyst_str, _hyst_count
    _hyst_bias  = "NEUTRAL"
    _hyst_mode  = "NONE"
    _hyst_str   = 0.0
    _hyst_count = 0


# ══════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════
def get_aurora_signal(df_m5):
    """
    Fast/Slow M5:
      FAST = ultimele 5 bare  → ce face piata ACUM
      SLOW = ultimele 15 bare → contextul trendului
    """
    if df_m5 is None or "close" not in df_m5.columns:
        return None

    prices = df_m5["close"].values

    if len(prices) < 15:
        return None

    # ── FAST + SLOW ───────────────────────────────────────────────
    fast_bias, fast_mode, fast_str = _analyze(prices[-5:])
    slow_bias, slow_mode, slow_str = _analyze(prices[-15:])

    # ── FAKE REVERSAL COOLDOWN ────────────────────────────────────
    fast_mode = _apply_fake_rev_cooldown(fast_bias, fast_mode)

    # ── COMBINE ───────────────────────────────────────────────────
    # STR = convingerea exclusiva a FAST despre ce vede ACUM.
    # SLOW nu modifica STR — apare doar in output ca context.
    # [v54] eliminat: strength += 2 (SLOW bonus) si strength -= 1 (tranzitie)
    strength = fast_str

    # ── NORMALIZARE ───────────────────────────────────────────────
    try:
        daily_vol   = float(np.std(np.diff(prices[-50:])))
        norm_factor = float(np.clip(1.5 / daily_vol, 0.5, 2.0)) if daily_vol > 0.01 else 1.0
        strength    = float(np.clip(strength * norm_factor, 0, 13))
    except Exception:
        norm_factor = 1.0

    # ── HYSTERESIS N=3 ────────────────────────────────────────────
    conf_bias, conf_mode, conf_str = _apply_hysteresis(fast_bias, fast_mode, strength)

    # ── VOLATILITY ────────────────────────────────────────────────
    try:
        volatility = float(np.std(np.diff(prices)))
    except Exception:
        volatility = 0.0

    return {
        "bias":       conf_bias,
        "mode":       conf_mode,
        "strength":   conf_str,
        "confidence": "HIGH" if fast_bias != "NEUTRAL" and slow_bias == fast_bias else "NORMAL" if conf_bias != "NEUTRAL" else "WEAK",
        "volatility": volatility,
        "speed":      "FAST",
        "m15_bias":   "NEUTRAL",   # compatibilitate bot
        "m15_mode":   "NONE",
        # debug
        "fast_bias":  fast_bias,
        "fast_mode":  fast_mode,
        "fast_str":   fast_str,
        "slow_bias":  slow_bias,
        "slow_mode":  slow_mode,
        "slow_str":   slow_str,
        "norm_factor": norm_factor,
    }