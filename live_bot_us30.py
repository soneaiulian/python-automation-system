"""
US30 Bot v1.0 — Dual Position Architecture
===========================================
SESIUNI:  S1 11:00-16:00  |  S2 19:00-22:00

ENTRY:    Aurora str >= 4.5 + EARLY_TREND/NORMAL
          POS1 + POS2 simultan  (sau POS1-only la MO/momentum)

POS1 — Cash Collector:
  Progressive lock (points-based, praguri 10-60 pts)
  RF_PROFIT: iese daca runnerul nu mai face maxime noi (2 failed attacks)
  NO_PEAK_ABORT: iese daca peak=0 dupa 100 loops in pierdere

POS2 — Trend Runner:
  Giveback 20 pts de la peak → exit
  SL absolut -25 pts
  RF + Elastic recovery exit

SL:  Trailing MT5 in trepte  |  Airbag software ATR*1.25
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import csv, os, time, json
from datetime import datetime, date, timedelta
from pathlib import Path

AURORA_STATE_FILE = Path(__file__).parent / "aurora_state.json"

from data_collector_m5 import get_data
from features_us30 import add_features
from ai.aurora_signal_bridge import get_aurora_signal
from telegram_notify import tg, tg_open, tg_close, tg_sl, tg_decay, tg_daily_summary
from aurora_v2_engine import MarketStructureEngine as _MSEClass
_mse = _MSEClass()  # Balance Zone Engine — produce cluster.mid

# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════

SYMBOL   = "US30"
SLIPPAGE = 100
MAGIC    = 123456
MIN_LOT  = 1.0
MAX_LOT  = 1.0   # US30 IC Markets — lot minim 1.0, calibram dupa primele teste

# ── SCHEDULE ZILNIC ──────────────────────────────────────────────
# 13:00-14:00  → WARMUP (fara trade)
# 14:00-15:20  → lot 0.01
# 15:20-17:15  → lot 0.02
# 17:15-18:00  → lot 0.01 (continuare)
# 17:15-21:00  → lot 0.01 (continuare)
# dupa 21:00   → stop trades noi

# ── TARGET ZILNIC ─────────────────────────────────────────────────
# Target 30$ → floor 20$ (stop daca scade sub 20$)
# La 60$+    → nu mai deschide, lasa pozitiile sa se inchida
# ── TARGET ZILNIC GLOBAL — fara restrictie de ore, ruleaza 24/7 ──
DAILY_PROFIT_TARGET = 350.0   # profit zilnic global → stop trades noi (250+100 sesiuni vechi)

SL_ATR_MULT = 1.25   # US30: airbag pentru disconnect/spike violent

# MO_RUNAWAY — limita istoric observatie (evita crestere nelimitata + lag progresiv)
MO_HISTORY_MAX_LEN = 50   # toate verificarile folosesc doar ultimele 3-5 elemente

# DIRECT ENTRY — flux secvential: impuls confirmat -> pullback real -> reluare
# Toate pragurile relative la ATR (functioneaza identic pe ATR mic si mare)
DE_IMPULSE_MIN_ATR    = 0.5    # displacement minim ca impulsul sa fie "confirmat"
DE_PULLBACK_MIN_ATR   = 0.15   # pullback minim de la varful impulsului
DE_PULLBACK_MAX_ATR   = 0.25   # pullback maxim — peste asta, nu mai e "sanatos"
DE_RESUME_MIN_ATR     = 0.08   # reluare minima de la minimul pullback-ului
DE_RESUME_MAX_ATR     = 0.15   # reluare maxima — peste asta, am ratat fereastra
DE_MIN_STR            = 5.0    # Aurora strength minim, in toate etapele

ENTRY_MAX_SPREAD = 5.0
ENTRY_MODES      = {"EARLY_TREND", "MOMENTUM", "SLOW_TREND", "NORMAL"}

POS1_BE_TRIGGER      = 5.0
POS2_BE_TRIGGER      = 5.0
POS2_WEAKNESS_STR    = 4.0
POS2_WEAKNESS_LOOPS  = 20

POST_SL_COOLDOWN_SEC = 15
POST_SL_MIN_STR      = 5.0

DEAD_HOURS   = {22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 16, 17, 18}
RISKY_HOURS  = {14, 21}
MAX_DAILY_LOSS = None

# ══════════════════════════════════════════════════════════════════
# POINTS-BASED THRESHOLDS — independente de lot
# La US30 IC Markets: 1 punct miscare = lot * 1$
# Conversia: profit_pts = profit / lot
# ══════════════════════════════════════════════════════════════════

# RF2 — Recovery Failure unificat (peak-based), POS1 + POS2
# Filozofie: peak nou -> deteriorare -> ARMED -> rebound -> rebound esuat -> EXIT
# Activ DOAR dupa ce peak >= RF2_GATE_PTS. Sub acel prag, sistemul actual ramane responsabil.
RF2_ARM_PTS          = 4.0    # deteriorare de la peak care armeaza RF
RF2_REBOUND_PTS      = 2.0    # recovery minim de la local_low care intra in REBOUND
RF2_FAIL_PTS         = 2.0    # cadere de la rebound_peak care confirma exit
RF2_GATE_PTS         = 15.0   # RF2 activ DOAR dupa ce peak >= acest prag

# P1_RF_LOW — Recovery Failure pentru zona de peak mic, POS1
# Inlocuieste complet P1_PROTECT: exit pe rebound esuat real, nu pe floor fix.
RF_LOW_GATE_MIN_PTS  = 2.9    # peak minim pentru activare — cuplat cu RF_PEAKLOW, fara gap
RF_LOW_GATE_MAX_PTS  = 15.0   # peak maxim — peste asta, RF2 deja preia
RF_LOW_ARM_PTS       = 3.0    # deteriorare de la peak care armeaza
RF_LOW_REBOUND_PTS   = 1.5    # recovery minim de la local_low pentru REBOUND
RF_LOW_FAIL_PTS      = 1.5    # cadere de la rebound_peak care confirma exit

# P2_RF_LOW — Recovery Failure pentru zona de peak mic, POS2 (Trend Runner)
# Inlocuieste complet P2_GIVEBACK. Mai relaxat decat POS1: mai multa
# rabdare ca POS1, dar mult mai putina toleranta decat giveback-ul vechi (20pts fix).
RF_LOW_P2_GATE_MIN_PTS  = 2.9    # peak minim pentru activare — cuplat cu RF_PEAKLOW, fara gap
RF_LOW_P2_GATE_MAX_PTS  = 15.0   # peak maxim — peste asta, RF2 deja preia, fara gap
RF_LOW_P2_ARM_PTS       = 5.0    # deteriorare de la peak care armeaza
RF_LOW_P2_REBOUND_PTS   = 2.0    # recovery minim de la local_low pentru REBOUND
RF_LOW_P2_FAIL_PTS      = 2.0    # cadere de la rebound_peak care confirma exit

# P1_RF_PEAKLOW — Recovery Failure pentru peak FOARTE mic (sub 2.9), POS1
# Inlocuieste complet P1_RF vechi (bazat pe worst_profit) cu aceeasi
# filozofie ARMED->REBOUND->FAIL ca RF2/RF_LOW, dar pe gate de peak.
# Activ DOAR cand peak < RF_PEAKLOW_GATE_MAX_PTS — nu are ce cauta peste.
RF_PEAKLOW_GATE_MAX_PTS = 2.9    # peak maxim pentru activare (sub asta)
RF_PEAKLOW_ARM_PTS      = 3.0    # deteriorare de la peak care armeaza
RF_PEAKLOW_REBOUND_PTS  = 3.0    # recovery minim de la local_low pentru REBOUND
RF_PEAKLOW_FAIL_PTS     = 4.0    # cadere de la rebound_peak care confirma exit

# POS1 — SL software
P1_SL_MOMENTUM_PTS   = 25.0   # SL momentum / direct entry
P1_SL_NORMAL_PTS     = 30.0   # SL normal
P1_SL_REENTRY_PTS    = 25.0   # SL reentry

# POS2 — SL
P2_SL_ABS_PTS        = 25.0   # SL absolut

# POS2 — Elastic recovery exit
P2_ELASTIC_ARM_PTS   = 15.0   # worst minim
P2_ELASTIC_REC_PTS   = 10.0   # recovery pentru exit

# POS2 — Exhaustion score
P2_EXHAUSTION_DD_PTS = 8.0    # worst sub care intra in calcul recovery ratio

# NO_PEAK_ABORT
NPA_EXIT_PTS         = 8.0    # profit sub care se iese

# RF_PROFIT — runner si-a pierdut puterea
RF_PROFIT_PEAK_MIN   = 20.0   # peak minim pentru activare
RF_PROFIT_ARM_DD     = 6.0    # drawdown de la peak pentru armare
RF_PROFIT_REBOUND    = 4.0    # rebound minim valid
RF_PROFIT_MARGIN     = 1.0    # marja failed attack
RF_PROFIT_CONF_DD    = 3.0    # retragere care confirma sfarsitul reboundului
RF_PROFIT_MAX_FA     = 2      # failed attacks inainte de exit

LOG_FILE    = "trades_log_v5.csv"
SIGNALS_LOG = "signals_log_v5.csv"
MARKET_LOG  = "market_snapshot_v5.csv"
RUNNER_LOG  = "runner_log_v5.csv"

# ══════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════

if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, "w", newline="") as f:
        csv.writer(f).writerow([
            "time","type","ticket","pos_type","signal",
            "entry_price","exit_price","profit","peak",
            "duration_sec","reason",
            "aurora_str","aurora_mode","aurora_bias","m15_bias",
            "atr","spread_entry","hour",
            "win_streak","loss_streak","daily_pnl_at_entry",
            "pair_ticket","is_reverse",
        ])

if not os.path.exists(SIGNALS_LOG):
    with open(SIGNALS_LOG, "w", newline="") as f:
        csv.writer(f).writerow([
            "time","bias","mode","strength","reason",
            "atr","spread","hour",
        ])

if not os.path.exists(MARKET_LOG):
    with open(MARKET_LOG, "w", newline="") as f:
        csv.writer(f).writerow([
            "time","price","aurora_bias","aurora_mode","aurora_strength",
            "atr","atr_avg20","spread","open_trades","floating_profit",
            "hour","session",
        ])

if not os.path.exists(RUNNER_LOG):
    with open(RUNNER_LOG, "w", newline="") as f:
        csv.writer(f).writerow([
            "time","ticket","pos_type","signal","loop",
            "profit","peak","fast","slow","str","mode",
        ])


def _log_runner(trade, profit, peak, loop):
    """Log per-loop data pentru trade-uri cu peak >= 10 pts (analiza runner)."""
    if _to_pts(peak, trade) < 10.0:
        return
    ss = SS
    try:
        with open(RUNNER_LOG, "a", newline="") as f:
            csv.writer(f).writerow([
                datetime.now(),
                trade["ticket"], trade["pos_type"], trade["signal"],
                loop,
                round(profit, 2), round(peak, 2),
                ss.get("fast_bias", ""),
                ss.get("slow_bias", ""),
                round(ss.get("aurora_strength", 0), 2),
                ss.get("aurora_mode", ""),
            ])
    except Exception:
        pass


def _log_signal(bias, mode, strength, reason):
    ss = SS
    try:
        tick   = mt5.symbol_info_tick(SYMBOL)
        spread = round(abs(tick.ask - tick.bid), 2) if tick else 0
    except Exception:
        spread = 0
    with open(SIGNALS_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(), bias, mode,
            round(float(strength), 2), reason,
            round(ss["recent_atr"], 3), spread, datetime.now().hour,
        ])


def _log_open(trade, daily_pnl, spread, is_reverse=False):
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(), "OPEN",
            trade["ticket"], trade["pos_type"], trade["signal"],
            round(trade["entry_price"], 2),
            "","","","","",
            round(trade["aurora_str"], 2),
            trade["aurora_mode"], trade["aurora_bias"],
            trade.get("m15_bias",""),
            round(trade["atr"], 3), round(spread, 2),
            datetime.now().hour,
            _win_streak, _loss_streak,
            round(daily_pnl, 2),
            trade.get("pair_ticket",""),
            int(is_reverse),
        ])


def _log_close(trade, exit_price, profit, reason):
    dur = round((datetime.now() - trade["entry_time"]).total_seconds(), 0)
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(), "CLOSE",
            trade["ticket"], trade["pos_type"], trade["signal"],
            round(trade["entry_price"], 2), round(exit_price, 2),
            round(profit, 2), round(trade["peak"], 2),
            dur, reason,
            round(trade["aurora_str"], 2),
            trade["aurora_mode"], trade["aurora_bias"],
            trade.get("m15_bias",""),
            round(trade.get("atr",0), 3),
            round(trade.get("spread_entry",0), 2),
            trade.get("hour",0),
            _win_streak, _loss_streak,
            round(trade.get("daily_pnl_at_entry",0), 2),
            trade.get("pair_ticket",""),
            trade.get("is_reverse",0),
        ])


def _market_snapshot(open_trades, atr, spread):
    ss      = SS
    tick    = mt5.symbol_info_tick(SYMBOL)
    price   = round(tick.bid, 2) if tick else 0
    floating= sum(_get_profit(t) for t in open_trades) if open_trades else 0
    hour    = datetime.now().hour
    session = "LONDON" if 7 <= hour < 12 else ("NY" if 12 <= hour < 20 else "ASIA")
    with open(MARKET_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(), price,
            ss["aurora_bias"], ss["aurora_mode"],
            round(ss["aurora_strength"], 2),
            round(atr, 3), round(ss["atr_avg20"], 3), round(spread, 2),
            len(open_trades), round(floating, 2),
            hour, session,
        ])


# ══════════════════════════════════════════════════════════════════
# SYSTEM STATE
# ══════════════════════════════════════════════════════════════════

SS = {
    "aurora_bias":     None,
    "aurora_mode":     "",
    "aurora_strength": 0.0,
    "aurora_prev_str": 0.0,
    "m15_bias":        None,
    "fast_bias":       None,
    "slow_bias":       None,
    "recent_atr":      0.0,   # se calculeaza din prima bara
    "atr_avg20":       0.0,
    "last_close":      0.0,   # close-ul ultimei lumânări M5
    "prev_close":      0.0,   # close-ul lumânarii anterioare (pentru move1)
}


def _update_state(df):
    ss = SS

    # ── ATR CALC ─────────────────────────────────────────────────
    try:
        if "high" in df.columns and "low" in df.columns and len(df) >= 15:
            high  = df["high"].values
            low   = df["low"].values
            close = df["close"].values
            tr = np.maximum(high - low,
                 np.maximum(abs(high - np.roll(close, 1)),
                            abs(low  - np.roll(close, 1))))
            tr[0] = high[0] - low[0]
            atr14 = float(np.mean(tr[-14:]))
            ss["recent_atr"] = round(atr14, 2)
            if ss["atr_avg20"] == 0.0:
                ss["atr_avg20"] = round(atr14, 2)
            else:
                ss["atr_avg20"] = round(ss["atr_avg20"] * 0.95 + atr14 * 0.05, 2)
    except Exception:
        pass

    try:
        get_aurora_signal._atr_current = ss["recent_atr"]
        get_aurora_signal._atr_avg20   = ss["atr_avg20"]
        aurora = get_aurora_signal(df)
        if aurora:
            ss["aurora_prev_str"]  = ss["aurora_strength"]

            # ── POST-PEAK MODE ────────────────────────────────────
            # Dupa peak mare (>=3.5$), Aurora are inertie si reactioneaza lent.
            # Dezactivam Aurora temporar — price action decide directia.
            # Reactivam cand se stabilizeaza un nou flow.
            if _post_peak_until and datetime.now() < _post_peak_until:
                # Aurora inghetata — PB tracker lucreaza pe price action
                ss["aurora_bias"]     = "NEUTRAL"
                ss["aurora_mode"]     = "POST_PEAK"
                ss["aurora_strength"] = 0.0
            else:
                ss["aurora_bias"]      = aurora["bias"]
                ss["aurora_mode"]      = aurora["mode"]
                ss["aurora_strength"]  = max(0.0, round(float(aurora["strength"]), 2))
                ss["m15_bias"]        = "NEUTRAL"
                ss["fast_bias"]       = aurora.get("fast_bias", aurora["bias"])
                ss["slow_bias"]       = aurora.get("slow_bias", aurora["bias"])
                # last_close / prev_close — pentru move1 fallback
                if "close" in df.columns and len(df) >= 2:
                    ss["prev_close"] = float(df["close"].iloc[-2])
                    ss["last_close"] = float(df["close"].iloc[-1])
                if aurora["mode"] == "DEAD_MARKET":
                    ss["dead_market_loops"] = ss.get("dead_market_loops", 0) + 1
                else:
                    ss["dead_market_loops"] = 0
    except Exception:
        pass








def _write_aurora_state(open_trades):
    """
    Scrie aurora_state.json cu pretul REAL din MT5 + starea curenta Aurora.
    Meta AI citeste current_us30_price direct de aici — fara Yahoo, fara delay.
    """
    ss = SS
    try:
        tick = mt5.symbol_info_tick(SYMBOL)
        us30_price = round(float(tick.bid), 2) if tick else None

        hour    = datetime.now().hour
        session = "LONDON" if 7 <= hour < 12 else ("NY" if 12 <= hour < 20 else "ASIA")

        state = {
            # ── US30 real-time din MT5 ─────────────────────────────
            "current_us30_price": us30_price,
            # ── Aurora signal ──────────────────────────────────────
            "aurora_bias":        ss.get("aurora_bias"),
            "aurora_mode":        ss.get("aurora_mode"),
            "aurora_str":         round(float(ss.get("aurora_strength", 0)), 2),
            "m15_bias":           "NEUTRAL",
            # ── Market context ─────────────────────────────────────
            "atr":                round(float(ss.get("recent_atr", 0)), 3),
            "atr_avg20":          round(float(ss.get("atr_avg20", 0)), 3),
            "session":            session,
            # ── Position state ─────────────────────────────────────
            "pos_count":          len(open_trades),
            "pb_state":           _pb.get("state"),
            "daily_pnl":          round(_daily_pnl, 2),
            # ── Timestamp ──────────────────────────────────────────
            "ts":                 datetime.now().isoformat(),
        }

        with open(AURORA_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

    except Exception as e:
        print(f"  [AURORA STATE] Write error: {e}")


# ══════════════════════════════════════════════════════════════════
# RISK ENGINE
# ══════════════════════════════════════════════════════════════════

_daily_pnl      = 0.0
_daily_peak     = 0.0   # peak-ul zilnic — floor = peak - 60$
_daily_date     = date.today()
_target_hit     = False
_target_stopped = False
_total_trades   = 0
_total_wins     = 0
_win_streak     = 0
_loss_streak    = 0
_last_sl_time   = None
_last_sl_signal = None
_last_sl_reason = None    # "SL" | "HARD_SL" | "AIRBAG" — blocheaza pb rearm
_close_fail_count = 0   # reconnect MT5 doar dupa 5 failuri consecutive
_last_exit_price   = None
_last_exit_peak    = None   # peak-ul ultimului trade — pentru revers detection
_post_peak_until   = None   # Aurora dezactivata temporar dupa peak mare
_last_exit_signal  = None   # directia ultimului trade
_last_abort_time   = None   # momentul ultimului EARLY_ABORT — pentru reconfirmare

# ── MARKET CONTEXT BUFFER ─────────────────────────────────────────────
# Stocheaza pretul real la fiecare loop, pe ultimele 30 de minute
# Folosit exclusiv de _compute_market_context() — fara impact pe logica de trading
_price_buffer: list = []   # lista de (timestamp, price)
_MC_WINDOW_MIN = 30        # fereastra de analiza in minute
_MC_PRINT_EVERY = 30       # afiseaza output la fiecare N loop-uri (~7.5s)


def _compute_market_context(price_now: float, atr: float,
                            aurora_bias: str, aurora_str: float) -> None:
    """
    Entry Context Engine — calculeaza din preturile reale ale ultimelor 30 minute
    ce face piata ACUM si ce ar trebui sa faci: MO sau PB si unde.

    Calcul bazat EXCLUSIV pe preturile curente din _price_buffer:
    - impulsurile reale (cat a urcat/coborat intr-o directie)
    - retragerile reale (cat a retras contra directiei)
    - raportul impuls/retragere
    - retragerea curenta de la ultimul extrem

    NU foloseste statistici istorice. NU face predictii.
    Descrie comportamentul ACTUAL al pietei si spune ce ai de facut.
    """
    if len(_price_buffer) < 10:
        return

    prices = [float(p) for _, p in _price_buffer]

    # ── Detectam swing-urile reale din preturile curente ─────────────
    # Un swing = miscare continua intr-o directie, filtram zgomot < 0.5p
    MIN_SWING = 0.5
    ups   = []   # marimile miscarilor in sus
    downs = []   # marimile miscarilor in jos
    direction = None
    peak   = prices[0]
    trough = prices[0]

    for p in prices[1:]:
        if direction is None:
            if p > peak + MIN_SWING:
                direction = 'up'; peak = p
            elif p < trough - MIN_SWING:
                direction = 'down'; trough = p
        elif direction == 'up':
            if p > peak:
                peak = p
            elif peak - p > MIN_SWING:
                ups.append(round(peak - trough, 1))
                trough = p
                direction = 'down'
        elif direction == 'down':
            if p < trough:
                trough = p
            elif p - trough > MIN_SWING:
                downs.append(round(p - trough, 1))
                peak = p
                direction = 'up'

    if not ups and not downs:
        return

    # ── In functie de bias, definim "impuls" si "retragere" ──────────
    if aurora_bias == 'LONG':
        impulse_list   = ups
        retragere_list = downs
    elif aurora_bias == 'SHORT':
        impulse_list   = downs
        retragere_list = ups
    else:
        # Neutral: luam cele mai mari ca impuls, cele mai mici ca retragere
        impulse_list   = ups if np.mean(ups or [0]) >= np.mean(downs or [0]) else downs
        retragere_list = downs if impulse_list is ups else ups

    avg_impulse   = float(np.mean(impulse_list))   if impulse_list   else 0.0
    avg_retragere = float(np.mean(retragere_list)) if retragere_list else 0.0
    max_retragere = float(max(retragere_list))     if retragere_list else 0.0

    ratio = avg_impulse / avg_retragere if avg_retragere > 0 else 999.0

    net_move   = prices[-1] - prices[0]
    total_move = sum(ups) + sum(downs)
    flow_eff   = abs(net_move) / total_move if total_move > 0 else 0.0
    n_swings   = len(ups) + len(downs)

    # ── Retragerea curenta de la ultimul extrem ───────────────────────
    last30 = prices[-30:] if len(prices) >= 30 else prices
    if aurora_bias == 'LONG':
        recent_ext = max(last30)
        current_pb = round(recent_ext - price_now, 1)
    elif aurora_bias == 'SHORT':
        recent_ext = min(last30)
        current_pb = round(price_now - recent_ext, 1)
    else:
        current_pb = 0.0

    # ── CONCLUZIA — din comportamentul actual al luminarilor ──────────
    if ratio >= 2.5 and flow_eff >= 0.20:
        mkt_state  = "RUNAWAY"
        conclusion = "MO_ALLOWED"
        pb_note    = f"retragere normala pana la {round(avg_retragere * 1.2, 0):.0f}p"
        arm_price  = None  # RUNAWAY — nu se armeaza PB

    elif avg_retragere >= avg_impulse * 0.55:
        # Piata oscileaza — retragerile sunt aproape la fel de mari ca impulsurile
        mkt_state  = "OSCILANT"
        conclusion = "ASTEAPTA_PB"
        # PB Geometry v2: arm_price = cluster.mid ± 10p
        _az = _mse._active
        _ld = _mse.last_dead_zone()
        _bz = _az if (_az and _az.touches >= 3) else (_ld if (_ld and _ld.touches >= 3) else None)
        if _bz and aurora_bias == 'LONG':
            arm_price = round(_bz.mid + 10, 1)
        elif _bz and aurora_bias == 'SHORT':
            arm_price = round(_bz.mid - 10, 1)
        else:
            arm_price = round(price_now - avg_retragere * 0.9, 1) if aurora_bias == 'LONG' else round(price_now + avg_retragere * 0.9, 1) if aurora_bias == 'SHORT' else None
        pb_note = f"BZ_MID={round(_bz.mid,1) if _bz else '?'}  arm={arm_price}"

    else:
        # Piata directionala — impulsurile mai mari decat retragerile
        mkt_state  = "DIRECTIONAL"
        conclusion = "PB_PERMIS_STRANS"
        # PB Geometry v2: arm_price = cluster.mid ± 10p
        _az = _mse._active
        _ld = _mse.last_dead_zone()
        _bz = _az if (_az and _az.touches >= 3) else (_ld if (_ld and _ld.touches >= 3) else None)
        if _bz and aurora_bias == 'LONG':
            arm_price = round(_bz.mid + 10, 1)
        elif _bz and aurora_bias == 'SHORT':
            arm_price = round(_bz.mid - 10, 1)
        else:
            arm_price = round(price_now - avg_retragere, 1) if aurora_bias == 'LONG' else round(price_now + avg_retragere, 1) if aurora_bias == 'SHORT' else None
        pb_note = f"BZ_MID={round(_bz.mid,1) if _bz else '?'}  arm={arm_price}"

    sign = '+' if net_move >= 0 else ''
    arm_str = f" → ARM_PB la {arm_price}" if arm_price else ""
    print(
        f"\n  ┌─[ECE] {aurora_bias} STR={round(aurora_str, 1)}\n"
        f"  │  Pret acum: {price_now}"
        f"  |  Net 30min: {sign}{round(net_move, 1)}p"
        f"  |  Flow: {round(flow_eff, 3)}  |  Swings: {n_swings}\n"
        f"  │  Impuls mediu:    {round(avg_impulse, 1)}p\n"
        f"  │  Retragere medie: {round(avg_retragere, 1)}p  (max: {round(max_retragere, 1)}p)\n"
        f"  │  Retragere acum:  {current_pb}p  |  Raport I/R: {round(ratio, 2)}x\n"
        f"  │  MARKET = {mkt_state}  |  {pb_note}\n"
        f"  └─ {conclusion}{arm_str}"
    )




# EXHAUSTION OBSERVATION SYSTEM
# Activ dupa POS2 exit cu peak >= 6$
# Masoara directia pietei dupa exit mare si decide: continuation sau pullback
_exhaustion = {
    "active":        False,
    "direction":     None,
    "exit_price":    None,
    "peak":          None,
    "atr":           None,
    "pullback_seen": False,
    "rev_loops":     0,
    "start_time":    None,
}

# ── FLOW_TRANSFER_MODE ────────────────────────────────────────────
# Activat dupa peak >= 7$ pe POS2.
# Faza 1 (0-60s):  observation only — flow vechi poate continua defensiv
# Faza 2 (60-150s): noua directie poate fi armata pe PB
# Dupa 150s: control revine complet la Aurora
_ftm = {
    "active":          False,
    "start_time":      None,
    "old_direction":   None,   # BUY/SELL — directia flow-ului epuizat
    "exit_price":      None,   # pretul la care a iesit POS2 (proxy pentru peak)
    "peak":            0.0,    # marimea miscarii in $ (pentru retrace calc)
    "atr":             0.0,
    # Phase
    "phase":           0,      # 0=inactiv 1=observation 2=arm_allowed
    # Scoring
    "verdict":         None,   # "REVERSAL" | "CONTINUATION" | None=incert
    "ftm_direction":   None,   # "LONG" | "SHORT" — ce citeste PB la ARM
    "new_direction":   None,   # "BUY" | "SELL" — directia noului flow
    "score":           0,
    "retrace_live":    0.0,
    "contra_weighted": 0.0,
    "contra_buf":      [],
    "micro_bo_count":  0,
    "last_price":      None,
    "last_mode":       "",
    "new_armed":       False,  # True = PB deja armat pe ftm_direction in P2
    "close_pos1_old":  False,  # True = executa close POS1 vechi la REVERSAL P2
    "_logged_dir":     None,   # previne spam log DIRECTION
    "cooldown_until":  None,   # datetime pana cand FTM nu se poate reactiva
}


# ── POS1 RE-ENTRY STATE ───────────────────────────────────────────
# POS2 devine anchor/runner. POS1 devine reusable attacker.
# Dupa ce POS1 iese (cu peak >= 1.3), sistemul poate redeschide
# DOAR POS1 daca POS2 inca ruleaza si conditiile sunt indeplinite.
_reentry = {
    "active":          False,   # True = POS1 a iesit pe pierdere, POS2 runner activ
    "signal":          None,    # directia POS2
    "str_at_exit":     0.0,     # str Aurora la exit POS1
    "exit_time":       None,
    "pos2_min_profit": 0.0,     # cel mai mic profit al POS2 dupa exit POS1 (drawdown)
    "pos2_was_down":   False,   # True daca POS2 a atins -2$ sau mai mult
}
# Activ DOAR pentru EARLY_TREND.
# Aurora detecteaza trendul rapid (neschimbat).
# Acest layer asteapta o retragere mica dupa impuls
# inainte de a executa intrarea — reduce top entries.
#
# Stari:
#   "idle"     — nu exista semnal EARLY_TREND activ
#   "armed"    — semnal detectat, asteptam pullback
#   "ready"    — pullback valid, urmatorul tick intra
#
# Reset automat daca:
#   - Aurora schimba bias/mode
#   - strength cade sub 5
#   - pretul depaseste impulse_ref + ATR*1.5 (trend prea departe)
# ── MOMENTUM OBSERVATION STATE ───────────────────────────────────
# Observer pur — detecteaza runaway trend prin acceleration + recoil.
# NU inlocuieste PB. Daca nu triggereza, PB continua normal.
# Observatia incepe devreme (str>=3), entry DOAR cu confirmare dubla:
#   1. acceleration_ratio > 1.8x (trendul fuge relativ la recent)
#   2. recoil_loss < 35%         (trendul pastreaza displacement)
_mo = {
    "active":       False,   # observatie activa
    "bias":         None,    # LONG/SHORT
    "ref_price":    0.0,     # pret la prima activare
    "move_history": [],      # miscari directionale per loop de la ref
    "peak_move":    0.0,     # cel mai mare move directional vazut
    "min_move":     0.0,     # cel mai mic move (pentru recoil)
    # ── DNA — supravietuieste resetului ────────────────────────────
    "dna_bias":     None,    # directia la care s-a calculat DNA (life-cycle MO)
    "dna_esc_score":  0,     # escalation score acumulat loop cu loop
}
# MO consumat pe trendul curent.
# Reset DOAR cand: bias se schimba SAU astr < 3.0
_mo_consumed = False

_pb = {
    "state":           "idle",    # idle | armed | ready
    "bias":            None,
    "impulse_ref":     None,
    "armed_str":       None,
    "post_exhaustion": False,
    "neutral_loops":   0,         # cate loop-uri consecutive cu NEUTRAL/NOISE
                                  # reset doar dupa >= 3 (nu instant)
    "armed_at":        None,      # timestamp armare — pentru timeout 90s
    "momentum_entry":  False,     # True = intra direct fara pullback
                                  # (EARLY_TREND + str>=10 + 5 ticks sustain)
    "momentum_ticks":  0,         # cate tick-uri consecutive cu strong_move
                                  # momentum_entry = True doar dupa >= 5 tick-uri
    "momentum_window": 0,         # cate loop-uri au trecut de la primul strong_move
                                  # displacement valid doar daca vine in <= 8 loops
    "str_at_arm":      0.0,       # strength la momentul armarii
    "escalation_entry": False,    # True = trend a accelerat continuu fara PB
                                  # (str a crescut cu +3 fata de armare)
    "stale_until":     None,      # cooldown dupa stale reset — nu rearmeaza imediat
    "fast_escape":     False,     # True = runaway trend detectat in primele 28 loops
    "max_favorable":   0.0,       # max move in directia bias de la armare
    "max_adverse":     0.0,       # max move contra bias de la armare
}


def _check_daily_reset():
    global _daily_pnl, _daily_date, _target_hit, _target_stopped

    today = date.today()
    if today != _daily_date:
        if _total_trades > 0:
            wr = round(_total_wins / _total_trades * 100, 1)
            account = mt5.account_info()
            bal = account.balance if account else 0
            tg_daily_summary(bal, _daily_pnl, wr, _total_trades)
        _daily_date     = today
        _daily_pnl      = 0.0
        _daily_peak     = 0.0
        _target_hit     = False
        _target_stopped = False
        run._target_printed = False
        print("🌅 ZI NOUA — reset")
        return


def _update_stats(profit):
    global _total_trades, _total_wins, _win_streak, _loss_streak
    global _daily_pnl
    _total_trades += 1
    _daily_pnl    += profit
    if profit > 0:
        _total_wins  += 1
        _win_streak  += 1
        _loss_streak  = 0
    else:
        _loss_streak += 1
        _win_streak   = 0
    # MAX_DAILY_LOSS dezactivat DEMO — reactivat inainte de LIVE




def _can_trade_now():
    """
    Fara restrictie de ore — ruleaza 24/7.
    Target zilnic global: DAILY_PROFIT_TARGET → stop trades noi, indiferent de ora.
    """
    global _target_hit, _target_stopped, _daily_peak

    # Actualizeaza peak zilnic
    if _daily_pnl > _daily_peak:
        _daily_peak = _daily_pnl

    if _daily_pnl >= DAILY_PROFIT_TARGET:
        if not _target_hit:
            _target_hit = True
            print(f"  [TARGET] {DAILY_PROFIT_TARGET}$ atins → oprire trades noi")
        _target_stopped = True
        return False

    return True


def _calc_lot():
    lot = 1.0
    if lot == 0.0:
        return 0.0
    if datetime.now().hour in RISKY_HOURS:
        lot = max(0.01, lot * 0.9)
    return max(0.01, min(MAX_LOT, round(lot, 2)))


# ══════════════════════════════════════════════════════════════════
# MT5 HELPERS
# ══════════════════════════════════════════════════════════════════

def _get_spread():
    tick = mt5.symbol_info_tick(SYMBOL)
    return round(abs(tick.ask - tick.bid), 2) if tick else 999.0


def _get_profit(trade):
    """Profit real in USD folosind tick value din MT5.
    Evita calcul manual cu puncte — diferit per simbol si broker."""
    positions = mt5.positions_get(ticket=trade["ticket"])
    if positions and len(positions) > 0:
        return float(positions[0].profit)
    # Fallback manual daca pozitia nu e gasita
    tick = mt5.symbol_info_tick(SYMBOL)
    if not tick: return 0.0
    info = mt5.symbol_info(SYMBOL)
    if not info: return 0.0
    price    = tick.bid if trade["signal"] == "BUY" else tick.ask
    mv       = price - trade["entry_price"]
    if trade["signal"] == "SELL": mv = -mv
    # tick_value = valoarea unui punct in valuta contului
    tick_val  = info.trade_tick_value
    tick_size = info.trade_tick_size
    lot       = trade["lot"]
    if tick_size > 0:
        return mv / tick_size * tick_val * lot
    return mv * lot


def _get_exit_price(signal):
    tick = mt5.symbol_info_tick(SYMBOL)
    if not tick: return 0.0
    return tick.bid if signal == "BUY" else tick.ask


def _send_order(signal, lot, sl, tp, comment, price_fixed=None, sl_fixed_dist=20.0):
    _t0 = time.perf_counter()
    tick = mt5.symbol_info_tick(SYMBOL)
    _t_tick = time.perf_counter()
    if not tick: return None
    if price_fixed is not None:
        price = price_fixed
    else:
        price = tick.ask if signal == "BUY" else tick.bid
    otype = mt5.ORDER_TYPE_BUY if signal == "BUY" else mt5.ORDER_TYPE_SELL
    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       float(lot),
        "type":         otype,
        "price":        price,
        "sl":           round(sl, 2),
        "tp":           round(tp, 2),
        "deviation":    SLIPPAGE,
        "magic":        MAGIC,
        "comment":      comment[:31],
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    print(
        f"  [ORDER] {signal} vol={req['volume']} "
        f"price={req['price']} sl={req['sl']} tp={req['tp']} "
        f"slippage={SLIPPAGE}"
    )
    _t_before_send = time.perf_counter()
    result = mt5.order_send(req)
    _t_after_send = time.perf_counter()
    print(
        f"  [ORDER RESULT] retcode={result.retcode if result else 'None'} "
        f"order={result.order if result else '-'} "
        f"comment={result.comment if result else '-'}"
    )
    print(
        f"  [TIMING] tick_fetch={round((_t_tick-_t0)*1000,1)}ms "
        f"order_send={round((_t_after_send-_t_before_send)*1000,1)}ms"
    )
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        real_entry = result.price if result.price else price
        # Recalculeaza SL din pretul real executat
        # price_now poate diferi de result.price cu 5-15$ pe ATR mare
        real_sl = round(real_entry - sl_fixed_dist, 2) if signal == "BUY" else round(real_entry + sl_fixed_dist, 2)
        # Actualizeaza SL in MT5 daca difera semnificativ
        if abs(real_sl - req["sl"]) > 0.5:
            _t_before_sltp = time.perf_counter()
            mt5.order_send({
                "action":   mt5.TRADE_ACTION_SLTP,
                "position": result.order,
                "sl":       real_sl,
                "tp":       round(tp, 2),
            })
            _t_after_sltp = time.perf_counter()
            print(f"  [TIMING] sltp_update={round((_t_after_sltp-_t_before_sltp)*1000,1)}ms")
        else:
            real_sl = req["sl"]
        _t_total = time.perf_counter()
        print(f"  [TIMING] _send_order TOTAL={round((_t_total-_t0)*1000,1)}ms")
        return {"ticket": result.order, "entry_price": real_entry, "real_sl": real_sl}
    print(f"  ORDER FAILED: {result.retcode if result else 'None'}")
    return None


def _close_order(ticket, signal, lot, comment):
    global _close_fail_count

    print(f"  [CLOSE TRY] ticket={ticket} reason={comment}")

    for attempt in range(3):
        tick = mt5.symbol_info_tick(SYMBOL)
        if not tick:
            print(f"  CLOSE FAIL attempt={attempt+1}: no tick")
            time.sleep(0.25)
            continue

        req = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       SYMBOL,
            "volume":       float(lot),
            "type":         mt5.ORDER_TYPE_SELL if signal == "BUY" else mt5.ORDER_TYPE_BUY,
            "position":     ticket,
            "price":        tick.bid if signal == "BUY" else tick.ask,
            "deviation":    SLIPPAGE,
            "magic":        MAGIC,
            "comment":      "close",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(req)

        if result is None:
            err = mt5.last_error()
            print(f"  CLOSE FAILED attempt={attempt+1}: result=None last_error={err}")
            _close_fail_count += 1
            if _close_fail_count >= 5:
                print(f"  [MT5 RECONNECT] {_close_fail_count} failuri consecutive")
                mt5.shutdown()
                time.sleep(0.5)
                mt5.initialize()
                _close_fail_count = 0
            time.sleep(0.5)
            continue

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            _close_fail_count = 0
            return True

        if result.retcode == 10036:
            print(f"  [CLOSE] ticket={ticket} inchis deja de MT5 (10036) — ok")
            _close_fail_count = 0
            return True

        print(f"  CLOSE FAILED attempt={attempt+1}: retcode={result.retcode} comment={result.comment}")
        time.sleep(0.15)

    _close_fail_count += 1
    return False


def _update_sl(ticket, sl, tp):
    """Fix #7 — returneaza status pentru BE logic."""
    result = mt5.order_send({
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl":       round(sl, 2),
        "tp":       round(tp, 2),
    })
    if result is None:
        print(f"  SL UPDATE FAILED: result=None ticket={ticket}")
        return False
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"  SL UPDATE FAILED: retcode={result.retcode} comment={result.comment}")
        return False
    return True


def _sync_open_trades(open_trades):
    """Elimina din open_trades ticketele inchise de MT5 (SL/TP fizic).
    Recupereaza profitul real si actualizeaza stats + log."""
    positions    = mt5.positions_get(symbol=SYMBOL)
    live_tickets = {p.ticket for p in positions} if positions else set()
    synced = []
    for t in open_trades:
        if t["ticket"] in live_tickets:
            synced.append(t)
        else:
            real_profit = 0.0
            exit_price  = t["entry_price"]

            # Fereastra mica — SL lovit recent, nu 24h
            deals = mt5.history_deals_get(
                datetime.now() - timedelta(minutes=5),
                datetime.now() + timedelta(seconds=10),
            )
            if deals:
                ticket_deals = [d for d in deals
                                if d.position_id == t["ticket"]
                                and d.symbol == SYMBOL]
                if ticket_deals:
                    for d in ticket_deals:
                        real_profit += d.profit + d.swap + d.commission
                        if d.price > 0:
                            exit_price = d.price
                else:
                    real_profit = t.get("last_known_profit", 0.0)
            else:
                real_profit = t.get("last_known_profit", 0.0)

            reason = "MT5_SL_TP"
            print(f"  [SYNC] ticket={t['ticket']} [{t['pos_type']}] "
                  f"profit={round(real_profit,2)}$ exit={round(exit_price,2)}")
            _log_close(t, exit_price, real_profit, reason)
            _update_stats(real_profit)
            global _last_exit_price, _last_sl_time, _last_sl_signal, _last_sl_reason
            _last_exit_price = exit_price
            _last_sl_time   = datetime.now()
            _last_sl_signal = t["signal"]
            _last_sl_reason = "SL"
            if not _exhaustion["active"] and not _ftm["active"]:
                _exhaustion["active"]        = True
                _exhaustion["direction"]     = t["signal"]
                _exhaustion["exit_price"]    = exit_price
                _exhaustion["peak"]          = t.get("peak", 0)
                _exhaustion["atr"]           = t.get("atr", SS["recent_atr"])
                _exhaustion["pullback_seen"] = False
                _exhaustion["start_time"]    = datetime.now()
            t["closing"] = False
            t["exit_reason_pending"] = None
            t["p1_under_floor_count"] = 0
            t["p2_under_floor_count"] = 0
    return synced

def _reset_mo():
    global _mo, _mo_consumed
    _mo["active"]       = False
    _mo["bias"]         = None
    _mo["ref_price"]    = 0.0
    _mo["move_history"] = []
    _mo["peak_move"]    = 0.0
    _mo["min_move"]     = 0.0


def _update_momentum_obs(price, bias, astr, open_trades):
    """
    Momentum Observation — detecteaza runaway trend devreme.

    Observatia incepe de la str>=3.
    NU inlocuieste PB. Daca nu triggereaza → False → PB continua normal.

    TRIGGER (obligatoriu ambele):
      1. acceleration_ratio >= 1.8x  — trendul accelereaza RELATIV la trecut
      2. recoil_loss <= 35%          — trendul pastreaza displacement

    CONFIDENCE BOOSTERS (contribuie la DNA, NU la trigger):
      3. displacement_growth — miscarile sunt progresiv crescatoare
      4. pressure_persistence — trendul continua sa impinga mai multe loops
      5. speed_build          — build-up progresiv, nu spike singular
    """
    global _mo, _mo_consumed

    # Nu observam cu pozitii deschise
    if len(open_trades) > 0:
        if _mo["active"]:
            _reset_mo()
        return False

    valid_bias = bias in ("LONG", "SHORT")

    # ── RESET _mo_consumed cand trendul moare ────────────────────
    # Conditii reset: bias se schimba SAU astr < 1.0
    if _mo_consumed:
        trend_died = (not valid_bias) or (bias != _mo.get("bias") and _mo.get("bias") is not None) or (astr < 1.0)
        dna_bias = _mo.get("dna_bias")
        if dna_bias and valid_bias and bias != dna_bias:
            trend_died = True
        if astr < 1.0:
            trend_died = True
        if trend_died:
            _mo_consumed = False
            _reset_mo()
        else:
            return False

    # ── ACTIVARE devreme — str >= 1.0 ────────────────────────────
    if not _mo["active"]:
        # Daca str a depasit 4.2 fara ca MO sa fi intrat → fereastra inchisa
        if astr >= 3.0:
            return False
        if valid_bias and astr >= 1.0:
            _mo["active"]       = True
            _mo["bias"]         = bias
            _mo["ref_price"]    = price
            _mo["move_history"] = []
            _mo["peak_move"]    = 0.0
            _mo["min_move"]     = 0.0
        return False

    # ── RESET daca bias s-a schimbat ─────────────────────────────
    if not valid_bias or bias != _mo["bias"]:
        _mo_consumed = False   # trend nou → MO poate rearma
        _reset_mo()
        return False

    # ── MOVE directional de la ref ────────────────────────────────
    if _mo["bias"] == "LONG":
        move = price - _mo["ref_price"]
    else:
        move = _mo["ref_price"] - price

    _mo["move_history"].append(round(move, 4))
    # Limitam istoricul la ultimele MO_HISTORY_MAX_LEN elemente — toate
    # verificarile folosesc doar hist[-5:] sau mai putin; fara limita,
    # lista creste nelimitat cat MO ramane in observatie pe acelasi trend,
    # iar recalcularea ei completa la fiecare loop incetineste bucla progresiv.
    if len(_mo["move_history"]) > MO_HISTORY_MAX_LEN:
        _mo["move_history"] = _mo["move_history"][-MO_HISTORY_MAX_LEN:]
    hist = _mo["move_history"]

    if move > _mo["peak_move"]: _mo["peak_move"] = move
    if move < _mo["min_move"]:  _mo["min_move"]  = move

    # Minim 4 loops inainte de evaluare
    if len(hist) < 4:
        return False

    # ══════════════════════════════════════════════════════════════
    # TRIGGER 1: ACCELERATION RATIO (PRIMARY)
    # Detecteaza daca trendul accelereaza RELATIV la comportamentul recent.
    # NU valori fixe — relativ, deci calibrat automat la orice sesiune.
    # ══════════════════════════════════════════════════════════════
    last_move  = hist[-1]
    prev_moves = [abs(m) for m in hist[:-1] if abs(m) > 1e-6]

    if not prev_moves or last_move <= 0:
        return False

    avg_prev = sum(prev_moves) / len(prev_moves)
    if avg_prev < 1e-6:
        return False

    acceleration_ratio = last_move / avg_prev
    # Exemplu real:  0.71 / 0.24 = 2.95x → trend pleaca
    # Exemplu noise: 0.31 / 0.26 = 1.1x  → nu triggereaza

    # ══════════════════════════════════════════════════════════════
    # TRIGGER 2: RECOIL QUALITY (PRIMARY)
    # Cat % din peak displacement pastreaza trendul.
    # Trend sanatos: >= 65% (recoil_loss <= 35%)
    # Noise/fake:    < 15%  (pierde aproape tot)
    # ══════════════════════════════════════════════════════════════
    peak = _mo["peak_move"]
    if peak <= 0:
        return False

    recoil_loss = (peak - move) / peak

    # ══════════════════════════════════════════════════════════════
    # CONFIDENCE BOOSTERS — contribuie la DNA, NU la trigger
    # ══════════════════════════════════════════════════════════════

    # BOOSTER 1: DISPLACEMENT GROWTH
    # Miscarile sunt progresiv crescatoare (nu +1.5 -1.4 +1.7 -1.6)
    # Masuram: cate din ultimele 4 miscari sunt > precedenta
    disp_growing = 0
    if len(hist) >= 4:
        recent4 = hist[-4:]
        disp_growing = sum(1 for i in range(1, len(recent4))
                           if recent4[i] > recent4[i-1])
    # 3-4 din 4 crescatoare = build progresiv
    displacement_growth_ok = disp_growing >= 3

    # BOOSTER 2: PRESSURE PERSISTENCE
    # Trendul continua sa impinga — miscarile pozitive domina
    # Masuram: din ultimele 5 loops, cate sunt pozitive (in directie)
    if len(hist) >= 5:
        positive_loops = sum(1 for m in hist[-5:] if m > 0)
        pressure_ok = positive_loops >= 4   # 4 din 5 = presiune persistenta
    else:
        pressure_ok = False

    # BOOSTER 3: SPEED BUILD
    # Build-up progresiv — trendul isi construieste viteza
    # NU spike singular: daca ultimul move e mult mai mare decat al 2-lea din urma
    # dar al 2-lea e mic, e spike. Build = crestere graduala.
    if len(hist) >= 3:
        last3 = [abs(m) for m in hist[-3:]]
        is_spike = (last3[-1] > last3[-2] * 3.0 and last3[-2] < avg_prev * 0.5)
        speed_build_ok = not is_spike
    else:
        speed_build_ok = True   # nu avem destule date, nu penalizam

    # ── CALCULEAZA CONFIDENCE SCORE (0-3) ────────────────────────
    confidence = sum([displacement_growth_ok, pressure_ok, speed_build_ok])

    # ── SALVEAZA DNA — la fiecare loop cu semnal decent ──────────
    # Direct Entry va folosi aceste date chiar daca MO nu triggereaza
    if acceleration_ratio >= 1.2 and recoil_loss <= 0.6:
        _mo["dna_bias"]     = _mo["bias"]   # singurul camp DNA inca citit (life-cycle MO)

    # Aurora mode curent pentru escalation check
    _aurora_mode_now = SS.get("aurora_mode", "")

    # ── ESCALATION SCORE — acumulat loop cu loop in PB_WAIT ───────
    # Daca MO e blocat de PB_WAIT (nu poate intra inca),
    # acumuleaza scor din accel + conf + recoil.
    # Scor >= 5 + disp >= 1.5$ → intra fara sa astepte distanta completa.
    #
    # Scoring:
    #   accel > 2x  = +1  |  accel > 4x  = +2
    #   conf  2/3   = +1  |  conf  3/3   = +2
    #   recoil > 70%= +1  |  recoil > 90%= +2
    esc_score = 0
    if acceleration_ratio > 4:   esc_score += 2
    elif acceleration_ratio > 2: esc_score += 1
    if confidence >= 3:          esc_score += 2
    elif confidence >= 2:        esc_score += 1
    recoil_pct = (1.0 - recoil_loss) * 100
    if recoil_pct > 90:          esc_score += 2
    elif recoil_pct > 70:        esc_score += 1

    # Acumuleaza scorul in DNA — cu cap la 20 pentru a evita acumulare infinita
    _mo["dna_esc_score"] = min(_mo.get("dna_esc_score", 0) + esc_score, 20)

    # Displacement de la origine
    disp_from_origin = abs(move)

    # ESCALATION ENTRY — scor >= 5 + disp >= 15$ (US30: miscare minima semnificativa)
    if (_mo["dna_esc_score"] >= 5 and
            disp_from_origin >= 15.0 and
            disp_from_origin <= 150.0 and   # nu intra in finalul miscarii US30
            astr >= 3.0 and
            _aurora_mode_now == "EARLY_TREND"):
        print(f"  [MO] ESCALATION ENTRY "
              f"bias={_mo['bias']} "
              f"esc_score={_mo['dna_esc_score']} "
              f"disp={round(disp_from_origin,2)}$ "
              f"accel={round(acceleration_ratio,2)}x "
              f"conf={confidence}/3 "
              f"recoil={round(recoil_pct,0)}%")
        _mo["dna_bias"]     = _mo["bias"]   # singurul camp DNA inca citit (life-cycle MO)
        _mo["dna_esc_score"]  = 0
        _reset_mo()
        _mo_consumed = True   # MO consumat — nu mai intra pe trendul asta
        return True

    # ══════════════════════════════════════════════════════════════
    # ENTRY — TRIGGER PRINCIPAL: acceleration + recoil (obligatorii)
    # Confidence boosters pot relaxa usor thresholdul de acceleratie
    # ══════════════════════════════════════════════════════════════
    accel_ok  = acceleration_ratio >= 1.8
    recoil_ok = recoil_loss <= 0.35

    # Daca confidence e mare (2-3/3), acceptam acceleratie usor mai mica
    if confidence >= 2 and recoil_ok:
        accel_ok = accel_ok or (acceleration_ratio >= 1.5)

    # EARLY_TREND obligatoriu — WEAK_TREND si NOISE interzise
    # str minim 5.8 la trigger
    mode_ok = (bias in ("LONG", "SHORT"))  # bias deja verificat
    # Verificam mode-ul Aurora curent din SS
    _aurora_mode_now = SS.get("aurora_mode", "")
    early_trend_only = (
        _aurora_mode_now == "EARLY_TREND" or
        (_aurora_mode_now == "NORMAL" and acceleration_ratio >= 2.5)
    )

    if accel_ok and recoil_ok and astr >= 3.0 and early_trend_only:
        # ── DIST FROM ORIGIN — cat a mers trendul de la armare ───
        dist_from_origin = move

        # US30: miscare minima semnificativa = ATR * 0.15 (~3$ cu ATR=20)
        # sub asta e zgomot, nu trend real
        _atr_mo = SS.get("recent_atr", 20.0)
        min_meaningful_move = max(3.0, _atr_mo * 0.15)
        if dist_from_origin < min_meaningful_move:
            return False

        if dist_from_origin > 150.0:
            # Trend consumat pe US30 (>150$ = aproape de final)
            return False
        elif dist_from_origin > 80.0:
            # Trend matur US30 — cere acceleratie mai mare
            if acceleration_ratio < 2.5:
                return False
        elif dist_from_origin > 40.0:
            # Trend mediu US30
            if acceleration_ratio < 2.0:
                return False

        # US30: conf=1/3 cu move mic = false positive
        if confidence < 1 and dist_from_origin < _atr_mo * 0.3:
            return False

        print(f"  [MO] RUNAWAY "
              f"bias={_mo['bias']} "
              f"accel={round(acceleration_ratio,2)}x "
              f"recoil={round((1-recoil_loss)*100,0)}% "
              f"conf={confidence}/3 "
              f"move={round(move,2)}$ str={round(astr,1)}")
        # Salveaza DNA final
        _mo["dna_bias"]     = _mo["bias"]   # singurul camp DNA inca citit (life-cycle MO)
        _reset_mo()
        _mo_consumed = True   # MO consumat — nu mai intra pe trendul asta
        return True

    return False


def _update_pullback_tracker(has_open_positions=False):
    """
    PB Tracker simplificat:

    ARM → fereastra 12 loops de observatie
      CAZ 1: trend fuge direct (displacement >= 1.2$ + str>=8 + no recoil)
             → direct_entry = True → ENTER fara PB
      CAZ 2: trend nu fuge direct
             → asteapta pullback normal → READY → ENTER

    Fara: stale reset, too_far, rearm, cooldown, exhaustion
    """
    global _pb
    ss  = SS
    atr = ss["recent_atr"]

    bias = ss["aurora_bias"]
    mode = ss["aurora_mode"]
    astr = ss["aurora_strength"]

    tick = mt5.symbol_info_tick(SYMBOL)
    if not tick:
        return
    price = tick.bid

    # move1 = displacement de la armare (IMPORTANT pentru ESCALATION/DIRECT)
    # Daca PB e armat: masuram cat a mers de la ref, nu ultima lumanare
    # La str < 8: memoria move1 se reseteaza (ref_price = current price)
    # Asa move1 mereu masoara de la ultimul punct solid cu str>=8
    # move1 = cat a mers trendul de la armare
    # LONG: price - impulse_ref | SHORT: impulse_ref - price
    if _pb["state"] == "armed" and _pb.get("impulse_ref") is not None:
        if _pb["bias"] == "LONG":
            move1 = price - _pb["impulse_ref"]
        elif _pb["bias"] == "SHORT":
            move1 = _pb["impulse_ref"] - price
        else:
            move1 = ss["last_close"] - ss["prev_close"]
    else:
        move1 = ss["last_close"] - ss["prev_close"]
    move1 = max(min(move1, atr * 3), -(atr * 3))

    # ── HOLD MODE ─────────────────────────────────────────────────
    if has_open_positions and _pb["state"] == "idle":
        return

    # ── CONDITII ARM ──────────────────────────────────────────────
    # Orele 04:00-10:30 (London open + NY open) — piata mai volatila
    # str mai mare necesar pentru a evita fake signals
    _now_h = datetime.now().hour
    _now_m = datetime.now().minute
    _risky_open = (_now_h >= 4 and (_now_h < 10 or (_now_h == 10 and _now_m <= 30)))
    # US30: str max = 5.0 → risky_open nu poate cere mai mult de 5.0
    _early_str  = 5.0  if _risky_open else 4.0
    _weak_str   = 5.0  if _risky_open else 5.0

    is_early_long  = (bias == "LONG"  and mode == "EARLY_TREND" and astr >= _early_str)
    is_early_short = (bias == "SHORT" and mode == "EARLY_TREND" and astr >= _early_str)
    is_normal_long  = (bias == "LONG"  and mode == "NORMAL" and astr >= _early_str)
    is_normal_short = (bias == "SHORT" and mode == "NORMAL" and astr >= _early_str)
    is_weak_long   = (bias == "LONG"  and mode == "WEAK_TREND"  and astr >= _weak_str)
    is_weak_short  = (bias == "SHORT" and mode == "WEAK_TREND"  and astr >= _weak_str)

    can_arm = (is_early_long or is_early_short or
               is_normal_long or is_normal_short or
               is_weak_long  or is_weak_short)

    # ── RESET LA BIAS FLIP REAL ───────────────────────────────────
    if _pb["state"] != "idle":
        real_flip = mode not in ("NONE", "NOISE", "WEAK_TREND")
        opposite = (
            (_pb["bias"] == "LONG"  and bias == "SHORT") or
            (_pb["bias"] == "SHORT" and bias == "LONG")
        )
        if real_flip and opposite:
            # Bias flip trebuie sa persiste >= 3 loops + displacement real
            _pb["flip_loops"] = _pb.get("flip_loops", 0) + 1
            flip_disp = abs(price - _pb["impulse_ref"]) if _pb["impulse_ref"] else 0
            bias_flipped = (
                _pb["flip_loops"] >= 3 and
                flip_disp >= atr * 0.8
            )
        else:
            _pb["flip_loops"] = 0
            bias_flipped = False

        if bias_flipped:
            print(f"  [PB] reset (bias flip) {_pb['bias']}→{bias} loops={_pb['flip_loops']}")
            _reset_pb()
            return

    # ── ARM ───────────────────────────────────────────────────────
    # FTM Phase 2: armeaza independent de Aurora (Aurora poate fi NEUTRAL)
    if (_pb["state"] == "idle" and
            _ftm["active"] and
            _ftm.get("phase", 0) == 2 and
            _ftm.get("ftm_direction") and
            not _ftm.get("new_armed", False)):
        ftm_dir = _ftm["ftm_direction"]
        _pb["state"]          = "armed"
        _pb["bias"]           = ftm_dir
        _pb["impulse_ref"]    = price
        _pb["str_at_arm"]     = astr
        _pb["armed_at"]       = datetime.now()
        _pb["max_favorable"]  = 0.0
        _pb["max_adverse"]    = 0.0
        _pb["momentum_entry"] = False
        _pb["arm_mode"]       = "FTM"
        _pb["de_state"]       = "WAIT_IMPULSE"   # state machine DIRECT ENTRY
        _pb["de_impulse_peak"] = price
        _pb["de_pb_low"]      = None
        _ftm["new_armed"]     = True   # armat o singura data per FTM ciclu
        print(f"  [PB] armed {_pb['bias']} ref={round(price,2)} "
              f"source=FTM verdict={_ftm.get('verdict')}")
        return

    # ARM normal pe Aurora bias
    if _pb["state"] == "idle" and can_arm:
        # NONE/NOISE nu poate arma PB normal
        if mode in ("NONE", "NOISE"):
            return

        # Daca FTM a armat deja PB → Aurora nu poate rearma
        # Previne double entry window dupa expiry FTM
        if _pb.get("arm_mode") == "FTM":
            return

        arm_long  = is_early_long  or is_normal_long  or is_weak_long
        arm_short = is_early_short or is_normal_short or is_weak_short

        _pb["state"]          = "armed"
        _pb["bias"]           = "LONG" if arm_long else "SHORT"
        _pb["impulse_ref"]    = price
        _pb["str_at_arm"]     = astr
        _pb["armed_at"]       = datetime.now()
        _pb["max_favorable"]  = 0.0
        _pb["max_adverse"]    = 0.0
        _pb["momentum_entry"] = False
        _pb["arm_mode"]       = mode
        _pb["de_state"]       = "WAIT_IMPULSE"   # state machine DIRECT ENTRY
        _pb["de_impulse_peak"] = price
        _pb["de_pb_low"]      = None
        print(f"  [PB] armed {_pb['bias']} ref={round(price,2)} "
              f"str={round(astr,1)} move1={round(move1,2)}")
        return

    # ── FEREASTRA DE OBSERVATIE (armed) ───────────────────────────
    if _pb["state"] == "armed":



        # Timeout SCOS — PB ramane armat pana cand:
        # - pullback > max_pb (prea mult)
        # - bias se schimba (trend opus)
        # - structural expire (5 loops deteriorare)

        # Expirare structurala cu histereza — nu la primul flicker
        # Necesita 5 loops sustinute de deteriorare reala
        is_opposite = (
            bias != _pb["bias"] and
            bias != "NEUTRAL" and
            mode not in ("NONE", "NOISE", "WEAK_TREND")
        )
        is_dead = (astr == 0 and mode == "NONE" and
                   abs(price - _pb["impulse_ref"]) < atr * 0.1)

        if is_opposite or is_dead:
            _pb["struct_expire_loops"] = _pb.get("struct_expire_loops", 0) + 1
        else:
            _pb["struct_expire_loops"] = 0

        if _pb.get("struct_expire_loops", 0) >= 20:
            # Daca pretul a mers deja favorabil >= min_pb, nu anulam — facem READY
            if _pb["bias"] == "LONG":
                favorable_now = price - _pb["impulse_ref"]
            else:
                favorable_now = _pb["impulse_ref"] - price

            if favorable_now >= atr * 0.5:
                # Piata a mers in directia noastra chiar daca Aurora s-a oprit
                _pb["state"] = "ready"
                print(f"  [PB] structural expire → READY direct (fav={round(favorable_now,2)}$)")
            else:
                print(f"  [PB] expired (structural x5) bias={bias} mode={mode}")
                _reset_pb()
            return

        # Actualizeaza max_favorable / max_adverse
        if _pb["bias"] == "LONG":
            favorable = price - _pb["impulse_ref"]
            adverse   = _pb["impulse_ref"] - price
        else:
            favorable = _pb["impulse_ref"] - price
            adverse   = price - _pb["impulse_ref"]

        if favorable > _pb["max_favorable"]: _pb["max_favorable"] = favorable
        if adverse   > _pb["max_adverse"]:   _pb["max_adverse"]   = adverse

        # ── ESCALATION — trend a accelerat de la armare ───────────
        # Daca str a crescut cu +3 fata de armare + directie corecta → direct entry
        str_growth = astr - _pb.get("str_at_arm", astr)
        is_early   = is_early_long or is_early_short
        directional_ok = (
            (is_early_long  and move1 > 0) or
            (is_early_short and move1 < 0)
        )
        if is_early and str_growth >= 3.0 and directional_ok:
            _pb["momentum_entry"] = True
            print(f"  [PB] ESCALATION ENTRY str_growth={round(str_growth,1)} "
                  f"str={round(astr,1)} bias={_pb['bias']}")
            return

        # ── DIRECT ENTRY — flux secvential: impuls -> pullback -> reluare ──
        # Nu mai intra instant pe displacement+recoil istoric. Asteapta
        # explicit un impuls confirmat, apoi un pullback REAL (masurat de
        # la varful impulsului, nu de la armare), apoi o confirmare de
        # reluare a directiei initiale. Toate pragurile relative la ATR.
        de_state = _pb.get("de_state", "WAIT_IMPULSE")

        if _pb["bias"] == "LONG":
            if price > _pb.get("de_impulse_peak", _pb["impulse_ref"]):
                _pb["de_impulse_peak"] = price
        else:
            if price < _pb.get("de_impulse_peak", _pb["impulse_ref"]):
                _pb["de_impulse_peak"] = price

        impulse_peak = _pb["de_impulse_peak"]

        # ── STAGE 1: WAIT_IMPULSE — asteapta impuls confirmat (>=0.5xATR) ──
        if de_state == "WAIT_IMPULSE":
            impulse_disp = abs(impulse_peak - _pb["impulse_ref"])
            if impulse_disp >= atr * DE_IMPULSE_MIN_ATR and astr >= DE_MIN_STR:
                _pb["de_state"] = "IMPULSE_CONFIRMED"
                print(f"  [PB] DIRECT ENTRY: impuls confirmat disp={round(impulse_disp,2)}")

        # ── STAGE 2: IMPULSE_CONFIRMED — asteapta pullback real (0.15-0.25xATR) ──
        elif de_state == "IMPULSE_CONFIRMED":
            if _pb["bias"] == "LONG":
                pullback = impulse_peak - price
            else:
                pullback = price - impulse_peak

            pb_min = atr * DE_PULLBACK_MIN_ATR
            pb_max = atr * DE_PULLBACK_MAX_ATR

            if pb_min <= pullback <= pb_max:
                _pb["de_state"]  = "PULLBACK_DETECTED"
                _pb["de_pb_low"] = price
                print(f"  [PB] DIRECT ENTRY: pullback detectat pb={round(pullback,2)} "
                      f"(prag {round(pb_min,1)}-{round(pb_max,1)})")
            elif pullback > pb_max:
                # Pullback prea mare — nu mai e "sanatos", trendul s-a rupt.
                # Resetam asteptarea unui nou impuls de la acest punct.
                _pb["de_state"]        = "WAIT_IMPULSE"
                _pb["de_impulse_peak"] = price
                _pb["impulse_ref"]     = price

        # ── STAGE 3: PULLBACK_DETECTED — track minim, asteapta reluare ──
        elif de_state == "PULLBACK_DETECTED":
            if _pb["bias"] == "LONG":
                if price < _pb["de_pb_low"]:
                    _pb["de_pb_low"] = price
                pullback_now = impulse_peak - _pb["de_pb_low"]
                resumption   = price - _pb["de_pb_low"]
            else:
                if price > _pb["de_pb_low"]:
                    _pb["de_pb_low"] = price
                pullback_now = _pb["de_pb_low"] - impulse_peak
                resumption   = _pb["de_pb_low"] - price

            pb_max = atr * DE_PULLBACK_MAX_ATR
            if pullback_now > pb_max:
                # Pullback-ul a continuat sa creasca peste prag — nu mai e sanatos.
                _pb["de_state"]        = "WAIT_IMPULSE"
                _pb["de_impulse_peak"] = price
                _pb["impulse_ref"]     = price
            else:
                conf_min = atr * DE_RESUME_MIN_ATR
                conf_max = atr * DE_RESUME_MAX_ATR
                resume_in_window = conf_min <= resumption <= conf_max
                if (resume_in_window and
                        astr >= DE_MIN_STR and
                        bias == _pb["bias"]):
                    _pb["momentum_entry"] = True
                    print(f"  [PB] DIRECT ENTRY "
                          f"impuls={round(abs(impulse_peak - _pb['impulse_ref']),2)} "
                          f"pb={round(pullback_now,2)} "
                          f"resume={round(resumption,2)} "
                          f"str={round(astr,1)}")
                    return
                elif resume_in_window:
                    # resumption era in fereastra valida, dar alta conditie a blocat —
                    # afisam exact ce a picat, ca sa nu mai ghicim din loguri.
                    blocked_by = []
                    if astr < DE_MIN_STR:
                        blocked_by.append(f"astr={round(astr,1)}<{DE_MIN_STR}")
                    if bias != _pb["bias"]:
                        blocked_by.append(f"bias={bias}!=pb_bias={_pb['bias']}")
                    print(f"  [PB] DIRECT ENTRY skip (resume in fereastra dar blocat): "
                          f"resume={round(resumption,2)} reasons={','.join(blocked_by)}")



        # ── CAZ 2: PULLBACK MODE — asteptam retragere ─────────────
        if _pb["bias"] == "LONG":
            pullback = _pb["impulse_ref"] - price
        else:
            pullback = price - _pb["impulse_ref"]

        # min_pb de baza: 15$ minim — pe US30 oscilatie normala e 5-15$
        # sub 15$ e zgomot, nu retragere reala
        m15_b = "NEUTRAL"
        if m15_b not in ("LONG", "SHORT"):
            min_pb = max(15.0, atr * 0.30)   # ~10-15$ cu ATR=35-48
        else:
            min_pb = max(10.0, atr * 0.20)

        # ── RUNAWAY SCALE — US30 dollar-based ────────────────────────
        # Cu cat pretul a mers mai mult de la armare, cere pullback mai mare.
        # Peste 80$: reset complet — re-arm la nivel nou (nu late entry).
        # 0-20$:   pullback normal (min_pb din ATR)
        # 20-40$:  min_pb = 20$ (pullback serios)
        # 40-60$:  min_pb = 30$
        # 60-80$:  min_pb = 50$
        # 80$+:    RESET — trendul a mers prea mult, re-armam la nivel nou
        trend_dist = _pb["max_favorable"]

        if trend_dist >= 80.0:
            # Trend a mers 80$+ de la armare → reset, re-arm la nivel nou
            print(f"  [PB] RUNAWAY reset — trend_dist={round(trend_dist,1)}$ >= 80$ → re-arm")
            _reset_pb()
            return
        elif trend_dist >= 60.0:
            min_pb = max(min_pb, 50.0)
        elif trend_dist >= 40.0:
            min_pb = max(min_pb, 30.0)
        elif trend_dist >= 20.0:
            min_pb = max(min_pb, 20.0)
        # sub 20$: min_pb din ATR (normal)

        # max_pb US30 — cat de mult poate retrage inainte sa consideram trend terminat
        # US30 str max = 5.0 (normalizare clipata la 0.5)
        if astr >= 5.0 and _pb["max_favorable"] >= atr * 0.5:
            max_pb = min(atr * 1.5, 80.0)   # ~72$ cu ATR=48
        elif astr >= 4.0:
            max_pb = min(atr * 1.2, 60.0)   # ~58$
        else:
            max_pb = min(atr * 1.0, 50.0)   # ~48$

        if min_pb <= pullback <= max_pb:
            prev_adverse = _pb.get("prev_adverse", _pb["max_adverse"])
            adverse_still_growing = _pb["max_adverse"] > prev_adverse
            _pb["prev_adverse"] = _pb["max_adverse"]
            _pb["adverse_stable_loops"] = 0 if adverse_still_growing else _pb.get("adverse_stable_loops", 0) + 1

            if _pb["adverse_stable_loops"] >= 1:
                _pb["state"]    = "ready"
                _pb["ready_at"] = datetime.now()
                src = "FTM" if _pb.get("arm_mode") == "FTM" else "AURORA"
                print(f"  [PB] READY ENTRY source={src} bias={_pb['bias']} "
                      f"ref={round(_pb['impulse_ref'],2)} price={round(price,2)} "
                      f"pb={round(pullback,2)} (min={round(min_pb,2)} max={round(max_pb,2)})")

    # READY timeout — verificat mereu, nu doar in blocul armed
    if _pb["state"] == "ready":
        ready_at = _pb.get("ready_at")
        if ready_at and (datetime.now() - ready_at).total_seconds() > 20:
            print(f"  [PB] READY timeout 20s → reset")
            _reset_pb()


def _reset_pb():
    """Reset complet al pullback tracker."""
    global _pb
    _pb["state"]          = "idle"
    _pb["bias"]           = None
    _pb["impulse_ref"]    = None
    _pb["armed_str"]      = None
    _pb["post_exhaustion"]= False
    _pb["neutral_loops"]  = 0
    _pb["armed_at"]       = None
    _pb["momentum_entry"] = False
    _pb["momentum_ticks"] = 0
    _pb["momentum_window"]= 0
    _pb["str_at_arm"]     = 0.0
    _pb["max_favorable"]  = 0.0
    _pb["max_adverse"]    = 0.0
    _pb["escalation_entry"] = False
    _pb["stale_until"]    = None
    _pb["fast_escape"]    = False
    _pb["flip_loops"]               = 0
    _pb["arm_mode"]                 = None
    _pb["struct_expire_loops"]      = 0
    _pb["m15_neutral_ready_loops"]  = 0
    _pb["ready_at"]                 = None
    _pb["prev_adverse"]             = 0.0
    _pb["adverse_stable_loops"]     = 0
    _pb["fav_prev_de"]              = 0.0
    _pb["noise_loops"]              = 0


def _can_enter():
    ss = SS


    # PB confirmat (ready sau momentum_entry) — Aurora nu mai blocheaza directia
    # Pretul a confirmat deja prin pullback sau displacement real
    pb_confirmed = (
        _pb["state"] == "ready" or
        _pb.get("momentum_entry", False)
    )

    # ── STALE PB FILTER ───────────────────────────────────────────
    # La momentul exact al entry-ului: daca momentum e complet mort → cancel
    # Pe US30 pullback-ul trece normal prin NOISE/NEUTRAL — nu e stale
    # Stale inseamna: NONE + str=0 (trend complet disparut)
    if pb_confirmed and _pb["state"] == "ready":
        current_mode = ss.get("aurora_mode", "NONE")
        current_str  = ss.get("aurora_strength", 0)
        if current_mode == "NONE" and current_str == 0:
            print(f"  [PB] STALE entry cancelled — momentum mort la trigger "
                  f"(mode={current_mode} str={current_str})")
            _reset_pb()
            return False, f"STALE_PB(mode={current_mode},str={current_str})"

    if not pb_confirmed:
        if not ss["aurora_bias"] or ss["aurora_bias"] == "NEUTRAL": return False, "NEUTRAL"
        if ss["aurora_mode"] not in ENTRY_MODES:              return False, f"MODE_{ss['aurora_mode']}"
        # Praguri noi pentru noua Aurora (max str SHORT ~5.94, LONG ~7.35)
        # EARLY_TREND si NORMAL: prag 4.5
        # Altele (MOMENTUM, SLOW_TREND): prag 7.0
        if ss["aurora_mode"] in ("EARLY_TREND", "NORMAL"):
            _min_str = 4.5
        else:
            _min_str = 7.0
        if ss["aurora_strength"] < _min_str:                  return False, f"STR_LOW({round(ss['aurora_strength'],1)})"

    spread = _get_spread()
    if spread > ENTRY_MAX_SPREAD:                         return False, f"SPREAD({spread})"

    # FIX 4: daca piata tocmai a fost DEAD_MARKET 3+ loops — nu intra pe BREAKOUT
    # Breakout dupa compresie trebuie confirmat cu energie noua, nu instant
    if ss.get("dead_market_loops", 0) >= 3:
        return False, f"DEAD_MARKET_BLOCK({ss['dead_market_loops']}loops)"

    # EXHAUSTION SYSTEM — observation mode dupa POS2 exit cu peak >= 3.5$
    # Aurora in "confirmation-only mode":
    # - CONTINUATION: Aurora confirma aceeasi directie → valid
    # - REVERSAL: Aurora ignorata — price action decide directia
    if _exhaustion["active"]:
        ex_dir   = _exhaustion["direction"]
        ex_price = _exhaustion["exit_price"]
        ex_atr   = _exhaustion["atr"] or ss["recent_atr"]

        # Timeout: daca exhaustion e activ de prea mult timp → dezactiveaza
        ex_start = _exhaustion.get("start_time")
        if ex_start and (datetime.now() - ex_start).total_seconds() > 60:
            print(f"  [EXHAUSTION] timeout 60s → dezactivat")
            _exhaustion["active"] = False
            return False, "EXHAUSTION_TIMEOUT"

        tick = mt5.symbol_info_tick(SYMBOL)
        if not tick:
            return False, "EXHAUSTION_OBS"

        current_price = tick.bid
        move = current_price - ex_price

        # ── CONTINUATION — Aurora confirma aceeasi directie ───────
        # Piata continua in directia trendului anterior + Aurora aliniata
        # Aurora are voie sa decida DOAR here
        if ex_dir == "BUY":
            continuation = move >= ex_atr * 0.5
        else:
            continuation = move <= -(ex_atr * 0.5)

        aurora_same = (
            (ex_dir == "BUY"  and ss["aurora_bias"] == "LONG") or
            (ex_dir == "SELL" and ss["aurora_bias"] == "SHORT")
        )

        if continuation and aurora_same:
            ss["post_cooldown_entry"] = True
            _exhaustion["active"]     = False
            print(f"  [EXHAUSTION→CONTINUATION] move={round(move,2)} aurora={ss['aurora_bias']}")
            _pb["state"]          = "armed"
            _pb["bias"]           = "LONG" if ss["aurora_bias"] == "LONG" else "SHORT"
            _pb["impulse_ref"]    = current_price
            _pb["armed_str"]      = ss["aurora_strength"]
            _pb["post_exhaustion"]= True
            _pb["neutral_loops"]  = 0
            _pb["armed_at"]       = datetime.now()
            _pb["momentum_entry"] = False
            _pb["str_at_arm"]     = ss["aurora_strength"]
            _pb["max_favorable"]  = 0.0
            _pb["max_adverse"]    = 0.0
            print(f"  [PB] armed post-exhaustion ref={round(current_price,2)}")

        else:
            # ── REVERSAL — price action decide, Aurora ignorata ────
            # Daca piata merge invers fata de trendul anterior:
            # Aurora NU mai are voie sa decida
            # Displacement real confirma reversal-ul
            if ex_dir == "BUY":
                reversal_move = -move   # cate $ a cazut fata de exit
            else:
                reversal_move = move    # cate $ a urcat fata de exit

            # Reversal confirmat de price action: >= ATR*1.0 displacement
            # + stabilizare >= 5 loops (nu panic wick)
            if reversal_move >= ex_atr * 1.0:
                _exhaustion["rev_loops"] = _exhaustion.get("rev_loops", 0) + 1
                if _exhaustion["rev_loops"] >= 5:
                    _exhaustion["active"]    = False
                    _exhaustion["rev_loops"] = 0
                    reversal_bias = "SHORT" if ex_dir == "BUY" else "LONG"
                    print(f"  [EXHAUSTION→REVERSAL] move={round(reversal_move,2)} "
                          f"dir={reversal_bias} loops={_exhaustion.get('rev_loops',0)}")
                    _pb["state"]          = "armed"
                    _pb["bias"]           = reversal_bias
                    _pb["impulse_ref"]    = current_price
                    _pb["str_at_arm"]     = ss["aurora_strength"]
                    _pb["armed_at"]       = datetime.now()
                    _pb["momentum_entry"] = False
                    _pb["max_favorable"]  = 0.0
                    _pb["max_adverse"]    = 0.0
                    ss["aurora_bias"] = reversal_bias
                    ss["aurora_mode"] = "EARLY_TREND"
                else:
                    return False, f"EXHAUSTION_REV_WAIT({_exhaustion['rev_loops']}/5)"
            else:
                _exhaustion["rev_loops"] = 0
                return False, f"EXHAUSTION_OBS(mv={round(move,2)} rev_need={round(ex_atr,2)})"

        _post_exhaustion_continuation = ss.get("post_cooldown_entry", False)
        ss["post_cooldown_entry"] = False
    else:
        _post_exhaustion_continuation = False

    # ── PULLBACK GATE ─────────────────────────────────────────────
    needs_pb = (
        ss["aurora_mode"] in ("EARLY_TREND", "NORMAL", "WEAK_TREND") or
        _post_exhaustion_continuation or
        _pb.get("arm_mode") == "FTM"   # FTM armat PB → cere confirmare PB
    )
    if needs_pb:
        if _pb.get("momentum_entry", False):
            src = "FTM" if _pb.get("arm_mode") == "FTM" else "AURORA"
            print(f"  [PB] MOMENTUM ENTRY source={src} "
                  f"bias={_pb['bias']} str={round(ss['aurora_strength'],1)}")
        elif _pb["state"] != "ready":
            pb_val = 0.0
            if _pb["impulse_ref"] is not None:
                tick = mt5.symbol_info_tick(SYMBOL)
                if tick:
                    if _pb["bias"] == "LONG":
                        pb_val = _pb["impulse_ref"] - tick.bid
                    else:
                        pb_val = tick.bid - _pb["impulse_ref"]
            return False, f"PB_WAIT(pb={round(pb_val,2)}/need={round(ss['recent_atr']*0.5,2)})"
        else:
            pass  # PB READY — M15 eliminat, intra direct

    return True, ""


def _update_ftm(current_price, atr):
    """
    FLOW_TRANSFER_MODE — scoring bazat pe 3 semnale din price action:
    1. Retrace % din mișcarea originală (semnal HARD)
    2. Contra agresion weighted recent (decay: recent contează 3x mai mult)
    3. MICRO_BREAKOUT mode count
    """
    global _ftm
    if not _ftm["active"]:
        return False

    elapsed = (datetime.now() - _ftm["start_time"]).total_seconds()

    if elapsed > 150:
        print(f"  [FTM] expirat 150s → cooldown 120s → Aurora preia controlul")
        _ftm["active"]         = False
        _ftm["phase"]          = 0
        _ftm["verdict"]        = None
        _ftm["ftm_direction"]  = None
        _ftm["new_direction"]  = None
        _ftm["new_armed"]      = False
        _ftm["close_pos1_old"] = False
        _ftm["score"]          = 0
        _ftm["retrace_live"]   = 0.0
        _ftm["contra_weighted"]= 0.0
        _ftm["contra_buf"]     = []
        _ftm["micro_bo_count"] = 0
        _ftm["last_price"]     = None
        _ftm["last_mode"]      = ""
        _ftm["_logged_dir"]    = None
        _ftm["cooldown_until"] = datetime.now() + timedelta(seconds=120)
        return False

    old_dir    = _ftm["old_direction"]
    peak_price = _ftm["exit_price"]    # pretul la exit = peak
    move_size  = _ftm["peak"]          # marimea miscarii originale

    # ── SEMNAL 1: RETRACE LIVE ────────────────────────────────────
    if old_dir == "SELL":
        retrace = current_price - peak_price   # cat a urcat fata de low
    else:
        retrace = peak_price - current_price   # cat a coborat fata de high

    retrace_live = retrace / max(move_size, 0.1)

    # ── SEMNAL 2: CONTRA AGGRESSION (decay weighted) ──────────────
    # Tine ultimele 10 miscari contra in buffer
    move_delta = current_price - _ftm.get("last_price", current_price)
    _ftm["last_price"] = current_price

    # miscare contra = invers directiei vechi
    if old_dir == "SELL":
        contra_move = max(0, move_delta)   # BUY apare = contra SELL
    else:
        contra_move = max(0, -move_delta)  # SELL apare = contra BUY

    buf = _ftm.get("contra_buf", [])
    buf.append(contra_move)
    if len(buf) > 10:
        buf.pop(0)
    _ftm["contra_buf"] = buf

    # Decay weights: row vechi 0.5x → row recent 1.6x
    weights = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.4, 1.6]
    n = len(buf)
    w_slice = weights[10-n:]   # aliniaza weights la lungimea buffer-ului
    contra_weighted = sum(b * w for b, w in zip(buf, w_slice))

    # ── SEMNAL 3: MICRO_BREAKOUT mode count ──────────────────────
    aurora_mode = _ftm.get("last_mode", "")
    if aurora_mode == "MICRO_BREAKOUT":
        _ftm["micro_bo_count"] = _ftm.get("micro_bo_count", 0) + 1
    else:
        _ftm["micro_bo_count"] = max(0, _ftm.get("micro_bo_count", 0) - 1)

    # ── SCORING ───────────────────────────────────────────────────
    score = 0

    # Retrace — semnal HARD
    if retrace_live > 0.80:    score += 2
    elif retrace_live > 0.50:  score += 1

    # Contra aggression decay weighted
    if contra_weighted > 2.5:  score += 2
    elif contra_weighted > 1.2: score += 1

    # Mode confirmat
    if _ftm.get("micro_bo_count", 0) >= 3: score += 1

    # Phase — setat INAINTE de decizie
    prev_phase = _ftm.get("phase", 0)
    _ftm["phase"] = 1 if elapsed < 60 else 2
    phase = _ftm["phase"]

    # Log 2: phase change
    if phase != prev_phase:
        print(f"  [FTM] PHASE {phase} (elapsed={round(elapsed,0):.0f}s)")

    # Log 3: scoring live (every 4 loops ~1s)
    if int(elapsed) % 4 == 0:
        print(f"  [FTM] score={score} retrace={round(retrace_live,2)} "
              f"contra={round(contra_weighted,2)} "
              f"micro_bo={_ftm.get('micro_bo_count',0)} "
              f"phase={phase}")

    # ── DECIZIE ───────────────────────────────────────────────────
    new_dir_detected = "BUY" if old_dir == "SELL" else "SELL"
    new_pb_bias = "LONG" if new_dir_detected == "BUY" else "SHORT"
    old_pb_bias = "LONG" if old_dir == "BUY"  else "SHORT"

    if score >= 3:
        _ftm["new_direction"] = new_dir_detected
        if _ftm.get("verdict") != "REVERSAL":
            print(f"  [FTM] REVERSAL score={score} "
                  f"retrace={round(retrace_live*100,0):.0f}% "
                  f"contra_w={round(contra_weighted,2)}$")
        _ftm["verdict"] = "REVERSAL"
        if phase == 2:
            _ftm["ftm_direction"]  = new_pb_bias
            _ftm["close_pos1_old"] = True
            # Log 4: ftm_direction
            if _ftm.get("_logged_dir") != new_pb_bias:
                print(f"  [FTM] DIRECTION = {new_pb_bias}")
                _ftm["_logged_dir"] = new_pb_bias

    elif score == 2 and retrace_live > 0.80:
        _ftm["new_direction"] = new_dir_detected
        if _ftm.get("verdict") != "REVERSAL":
            print(f"  [FTM] REVERSAL (retrace hard) score={score} "
                  f"retrace={round(retrace_live*100,0):.0f}%")
        _ftm["verdict"] = "REVERSAL"
        if phase == 2:
            _ftm["ftm_direction"]  = new_pb_bias
            _ftm["close_pos1_old"] = True
            if _ftm.get("_logged_dir") != new_pb_bias:
                print(f"  [FTM] DIRECTION = {new_pb_bias}")
                _ftm["_logged_dir"] = new_pb_bias

    elif score <= 1:
        if _ftm.get("verdict") != "CONTINUATION":
            print(f"  [FTM] CONTINUATION score={score} "
                  f"retrace={round(retrace_live*100,0):.0f}%")
        _ftm["verdict"]            = "CONTINUATION"
        _ftm["new_direction"]      = None
        _ftm["close_pos1_old"]     = False
        if phase == 2:
            _ftm["ftm_direction"]  = old_pb_bias
            if _ftm.get("_logged_dir") != old_pb_bias:
                print(f"  [FTM] DIRECTION = {old_pb_bias} (continuation)")
                _ftm["_logged_dir"] = old_pb_bias

    if elapsed >= 90 and _ftm.get("verdict") not in ("REVERSAL", "CONTINUATION"):
        _ftm["verdict"]            = "REVERSAL"
        _ftm["new_direction"]      = new_dir_detected
        _ftm["ftm_direction"]      = new_pb_bias
        _ftm["close_pos1_old"]     = True
        print(f"  [FTM] 90s timeout → REVERSAL fortat")
        if _ftm.get("_logged_dir") != new_pb_bias:
            print(f"  [FTM] DIRECTION = {new_pb_bias}")
            _ftm["_logged_dir"] = new_pb_bias

    # Salveaza metrici pentru logging
    _ftm["retrace_live"]     = round(retrace_live, 3)
    _ftm["contra_weighted"]  = round(contra_weighted, 3)
    _ftm["score"]            = score

    return True


def _open_dual(is_reverse=False):
    ss     = SS
    # Recalculeaza pb_confirmed local (identic cu _can_enter)
    pb_confirmed = (
        _pb["state"] == "ready" or
        _pb.get("momentum_entry", False)
    )
    # Semnalul vine INTOTDEAUNA din PB bias cand PB e confirmat
    # Aurora la momentul intrarii poate fi NEUTRAL/NOISE/opus — normal dupa pullback
    if pb_confirmed and _pb["bias"]:
        effective_bias = _pb["bias"]
    elif _pb["bias"] and (ss["aurora_bias"] == "NEUTRAL" or not ss["aurora_bias"]):
        effective_bias = _pb["bias"]
    else:
        effective_bias = ss["aurora_bias"]
    signal = "BUY" if effective_bias == "LONG" else "SELL"
    atr    = ss["recent_atr"]
    if atr <= 0:
        atr = 100.0
    spread = _get_spread()

    # ── FTM LOT / POZITII ─────────────────────────────────────────
    # Phase 1: doar POS1, lot normal, SL mai strans (ATR*2.0 in loc de normal)
    # Phase 2 (transfer): POS1+POS2, lot 0.3 (prudent pe flow nou)
    # Normal (fara FTM): POS1+POS2, lot normal
    ftm_active = _ftm["active"]
    ftm_phase  = _ftm.get("phase", 0)

    # Entry type — PB_READY sau PB_MOMENTUM
    entry_type = "PB_MOMENTUM" if _pb.get("momentum_entry") else "PB_READY"
    pb_source  = "FTM" if _pb.get("arm_mode") == "FTM" else "AURORA"

    if ftm_active and ftm_phase == 2:
        pos_types = ["POS1", "POS2"]
        lot = _calc_lot()
        print(f"  [FTM PHASE 2] OPEN POS1+POS2 {signal} "
              f"reason=FTM_{_ftm.get('verdict','?')} "
              f"entry={entry_type} source={pb_source} lot={lot}")
    elif ftm_active and ftm_phase == 1:
        # FTM Phase 1 — nu deschidem nimic, asteptam Phase 2
        print(f"  [FTM PHASE 1] wait — no entry yet")
        return []
    elif _pb.get("momentum_entry"):
        # Momentum, Escalation, Direct Entry → POS1 only
        # PB ready (state=="ready") → POS1 + POS2
        pos_types = ["POS1"]
        lot = _calc_lot()
    else:
        # PB pullback normal → POS1 + POS2
        pos_types = ["POS1", "POS2"]
        lot = _calc_lot()

    label  = "REV" if is_reverse else "DUAL"
    trades = []

    # ── Pret calculat O SINGURA DATA pentru ambele ordine ─────────
    tick_now = mt5.symbol_info_tick(SYMBOL)
    if not tick_now:
        print("  [DUAL] nu pot citi tick pentru ordine")
        return []
    price_now = tick_now.ask if signal == "BUY" else tick_now.bid

    # SL calculat din price_now — consistent cu ordinul trimis
    SL_FIXED = 20.0
    sl = price_now - SL_FIXED if signal == "BUY" else price_now + SL_FIXED
    tp = 0.0

    for pos_type in pos_types:
        res = _send_order(signal, lot, sl, tp,
                          f"v5|{pos_type}|{label}|s{round(ss['aurora_strength'],1)}",
                          price_fixed=price_now,
                          sl_fixed_dist=SL_FIXED)
        if res:
            # Foloseste SL-ul real recalculat din pretul executat
            sl_actual = res.get("real_sl", sl)
            trade = {
                "ticket":             res["ticket"],
                "entry_price":        res["entry_price"],
                "signal":             signal,
                "lot":                lot,
                "sl":                 sl_actual,
                "tp":                 tp,
                "pos_type":           pos_type,
                "peak":               0.0,
                "entry_time":         datetime.now(),
                "aurora_str":         ss["aurora_strength"],
                "aurora_mode":        ss["aurora_mode"],
                "aurora_bias":        ss["aurora_bias"],
                "m15_bias":           ss.get("m15_bias", ""),
                "atr":                atr,
                "spread_entry":       spread,
                "hour":               datetime.now().hour,
                "daily_pnl_at_entry": _daily_pnl,
                "be_done":            False,
                "pair_ticket":        None,
                "is_reverse":         int(is_reverse),
                "post_cooldown":      ss.get("post_cooldown_entry", False),
                "is_momentum":        getattr(_open_dual, "_mo_pure_entry", False),
                "is_direct_entry":    _pb.get("momentum_entry", False) and not getattr(_open_dual, "_mo_pure_entry", False),
                "is_reentry":         False,
                "loop_count":         0,
                # ── DECAY ENGINE fields (POS2 only) ──────────────
                "deep_dd_loops":      0,     # loops cu profit <= -3$
                "negative_loops":     0,     # loops cu profit < 0
                "adverse_lows":       0,     # new lows confirmate (gap >= spread*1.5)
                "worst_profit":       0.0,   # cel mai mic profit atins
                "decay_confirmed":    False, # structural decay detectat
            }
            trades.append(trade)
            print(f"  OPEN {signal} [{pos_type}] "
                  f"price={round(res['entry_price'],2)} "
                  f"sl={round(sl_actual,2)} tp={round(tp,2)} "
                  f"lot={lot} #{res['ticket']}"
                  f"{' [REVERSE]' if is_reverse else ''}")
            tg_open(pos_type, signal, res['entry_price'], sl, lot,
                    ss["aurora_strength"], ss["aurora_mode"])

    if len(trades) == 2:
        trades[0]["pair_ticket"] = trades[1]["ticket"]
        trades[1]["pair_ticket"] = trades[0]["ticket"]

    # Reset pullback tracker dupa intrare reusita
    if trades:
        _reset_pb()

    for t in trades:
        _log_open(t, _daily_pnl, spread, is_reverse)

    return trades

# ══════════════════════════════════════════════════════════════════
# BE
# ══════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════
# TRAILING SL — trepte fixe US30
# ══════════════════════════════════════════════════════════════════
# POS1: SL start=-20$  | la 20$ profit → BE | la 40$ → +20$ | la 60$ → +40$ ...
# POS2: SL start=-20$  | la 25$ profit → BE | la 45$ → +20$ | la 65$ → +40$ ...
# ══════════════════════════════════════════════════════════════════

def _calc_trail_sl(trade, peak):
    """
    Calculeaza SL-ul nou bazat pe peak profit atins.
    Returneaza (new_sl, level_label) sau (None, None) daca nu e cazul.
    """
    entry  = trade["entry_price"]
    signal = trade["signal"]
    ptype  = trade["pos_type"]

    # Trepte POS1: prag_profit → SL offset fata de entry
    # la 20$ profit  → SL = entry (BE, offset=0)
    # la 40$ profit  → SL = entry + 20$
    # la 60$ profit  → SL = entry + 40$ etc.
    if ptype == "POS1":
        steps = []
        for i in range(50):          # pana la 1000$+
            prag = 20 + i * 20       # 20, 40, 60, 80 ...
            offset = i * 20          # 0, 20, 40, 60 ...
            steps.append((prag, offset))
    else:  # POS2
        steps = []
        for i in range(50):
            prag = 25 + i * 20       # 25, 45, 65, 85 ...
            offset = i * 20          # 0, 20, 40, 60 ...
            steps.append((prag, offset))

    # Gaseste cel mai inalt prag atins
    new_offset = None
    new_prag   = None
    for prag, offset in reversed(steps):
        if peak >= prag:
            new_offset = offset
            new_prag   = prag
            break

    if new_offset is None:
        return None, None  # sub primul prag, SL neschimbat

    # Calculeaza SL nou
    if signal == "BUY":
        new_sl = entry + new_offset
    else:
        new_sl = entry - new_offset

    # Nu permite SL sa coboare sub cel curent
    current_sl = trade["sl"]
    if signal == "BUY" and new_sl <= current_sl:
        return None, None
    if signal == "SELL" and new_sl >= current_sl:
        return None, None

    label = f"BE" if new_offset == 0 else f"+{new_offset}$"
    return new_sl, f"prag={new_prag}$ → SL{label}"


def _check_be(trade, peak):
    """Trailing SL in trepte — inlocuieste vechiul BE fix."""
    new_sl, label = _calc_trail_sl(trade, peak)
    if new_sl is None:
        return

    success = _update_sl(trade["ticket"], new_sl, trade["tp"])
    if success:
        old_sl = trade["sl"]
        trade["sl"]     = new_sl
        trade["be_done"] = True   # compatibilitate cu restul codului
        print(f"  [TRAIL {trade['pos_type']}] {label} | SL {round(old_sl,1)} → {round(new_sl,1)}")


# ══════════════════════════════════════════════════════════════════
# EXIT
# ══════════════════════════════════════════════════════════════════

def _update_decay_engine(trade, profit, spread):
    """
    Decay engine pentru POS2.
    Masoara degradarea structurala acumulata in timp.
    Nu exit instant — doar confirma decay si lasa exit logic sa decida.

    Semnale validate pe 2 zile de date:
    - deep_dd_ratio > 0.50 (fallen: ~51-53%, survivors: ~12%)
    - adverse_lows > 15    (fallen: 20-54, survivors: 3-8)
    - total_loops > 80     (fallen stau 107-155 loops)
    """
    if trade["pos_type"] != "POS2":
        return

    total_loops = trade.get("loop_count", 1)
    gap_min     = spread * 1.5   # gap minim anti-micro-bounce

    # 1. Deep DD tracking — US30: deep DD inseamna sub -15$
    if profit <= -15.0:
        trade["deep_dd_loops"] += 1

    # 2. Negative lifetime
    if profit < 0:
        trade["negative_loops"] += 1

    # 3. Adverse lows — cu gap minim
    if profit < trade["worst_profit"] - gap_min:
        trade["worst_profit"] = profit
        trade["adverse_lows"] += 1

    # 4. Decay confirmation
    if total_loops > 50:
        deep_dd_ratio = trade["deep_dd_loops"] / total_loops
        if deep_dd_ratio > 0.50 and trade["adverse_lows"] > 8:
            if not trade["decay_confirmed"]:
                trade["decay_confirmed"] = True
                print(f"  [DECAY] POS2 structural decay confirmat "
                      f"ratio={round(deep_dd_ratio,2)} "
                      f"adverse_lows={trade['adverse_lows']} "
                      f"loops={total_loops}")
                tg_decay(trade["ticket"], f"ratio={round(deep_dd_ratio,2)} lows={trade['adverse_lows']} loops={total_loops}")


def _to_pts(money_value, trade):
    """Converteste profit/loss din $ in puncte de piata.
    La US30 IC Markets: 1 punct miscare = lot * 1$
    Folosit pentru toate comparatiile de decizie — independente de lot.
    """
    lot = trade.get("lot", 1.0)
    return money_value / lot if lot > 0 else money_value


def _rf2_check(trade, profit, peak, gate_pts=None):
    """
    RF2 — Recovery Failure unificat (peak-based), POS1 + POS2.
    Activ DOAR dupa ce peak >= gate_pts (default RF2_GATE_PTS). Sub acel
    prag, sistemul actual ramane responsabil — RF2 nu evalueaza si nu
    initializeaza nimic.

    Filozofie (activa doar peste gate):
      1. Peak nou (real)         -> reset, IDLE
      2. Deteriorare >= RF2_ARM_PTS de la peak -> ARMED
      3. Recovery >= RF2_REBOUND_PTS de la local_low -> REBOUND
      4. Cadere >= RF2_FAIL_PTS de la rebound_peak    -> EXIT (rebound esuat)
      5. In REBOUND, cadere sub local_low-ul care a generat reboundul
         -> EXIT direct (rebound-ul a esuat complet, nu mai asteptam altul)

    State stocat in trade:
      rf2_state        IDLE | ARMED | REBOUND
      rf2_peak_ref      peak-ul de referinta curent (in puncte)
      rf2_local_low     cel mai prost profit de cand e ARMED (in puncte)
      rf2_rebound_peak  cel mai bun profit din rebound-ul curent (in puncte)
    """
    if gate_pts is None:
        gate_pts = RF2_GATE_PTS

    profit_pts = _to_pts(profit, trade)
    peak_pts   = _to_pts(peak, trade)

    # ── GATE — sub acest peak, RF2 nu e activ deloc ────────────────
    if peak_pts < gate_pts:
        return False, None

    state    = trade.get("rf2_state", "IDLE")
    peak_ref = trade.get("rf2_peak_ref", peak_pts)

    # ── Sincronizam peak_ref cu peak-ul absolut (poate creste intre apeluri) ──
    if peak_pts > peak_ref:
        peak_ref = peak_pts

    # ── Profit a egalat/spart peak_ref -> new high real -> reset complet ──
    if profit_pts >= peak_ref:
        trade["rf2_state"]    = "IDLE"
        trade["rf2_peak_ref"] = profit_pts
        return False, None

    trade["rf2_peak_ref"] = peak_ref

    # ── IDLE -> ARMED ──────────────────────────────────────────────
    if state == "IDLE":
        dd = peak_ref - profit_pts
        if dd >= RF2_ARM_PTS:
            trade["rf2_state"]        = "ARMED"
            trade["rf2_local_low"]    = profit_pts
            trade["rf2_rebound_peak"] = profit_pts
        return False, None

    # ── ARMED — track local_low, asteapta rebound valid ───────────
    if state == "ARMED":
        if profit_pts < trade.get("rf2_local_low", profit_pts):
            trade["rf2_local_low"]    = profit_pts
            trade["rf2_rebound_peak"] = profit_pts

        local_low = trade["rf2_local_low"]
        if profit_pts >= local_low + RF2_REBOUND_PTS:
            trade["rf2_state"]        = "REBOUND"
            trade["rf2_rebound_peak"] = profit_pts
        return False, None

    # ── REBOUND — track rebound_peak, detecteaza rebound esuat ────
    if state == "REBOUND":
        local_low = trade.get("rf2_local_low", profit_pts)

        # Cadere sub local_low-ul care a generat reboundul -> EXIT direct
        if profit_pts < local_low:
            return True, (f"RF2_BREAK pk={round(peak_ref,1)} ll={round(local_low,1)} "
                          f"p={round(profit_pts,1)}")

        if profit_pts > trade.get("rf2_rebound_peak", profit_pts):
            trade["rf2_rebound_peak"] = profit_pts

        rebound_peak = trade["rf2_rebound_peak"]
        if profit_pts <= rebound_peak - RF2_FAIL_PTS:
            return True, (f"RF2 pk={round(peak_ref,1)} ll={round(local_low,1)} "
                          f"rb={round(rebound_peak,1)} p={round(profit_pts,1)}")
        return False, None

    return False, None


def _rf_low_check(trade, profit, peak):
    """
    P1_RF_LOW — Recovery Failure pentru zona de peak mic (2.9-15), POS1.
    Aceeasi structura ca RF2 (ARMED -> REBOUND -> FAIL), praguri mai mici,
    si INLOCUIESTE complet P1_PROTECT pentru aceasta zona — exit pe baza
    unui rebound esuat real, nu pe un floor arbitrar fix.

    Gate: activ doar cand RF_LOW_GATE_MIN_PTS <= peak < RF_LOW_GATE_MAX_PTS.
    Peste RF_LOW_GATE_MAX_PTS, RF2 deja preia rolul de protectie/strangere.

    Filozofie (identica RF2, praguri proprii):
      1. Peak nou (real)              -> reset, IDLE
      2. Deteriorare >= RF_LOW_ARM_PTS de la peak      -> ARMED
      3. Recovery >= RF_LOW_REBOUND_PTS de la local_low -> REBOUND
      4. Cadere >= RF_LOW_FAIL_PTS de la rebound_peak   -> EXIT (rebound esuat)
      5. In REBOUND, cadere sub local_low-ul care a generat reboundul
         -> EXIT direct

    State stocat in trade (prefix separat de rf2_* ca sa nu colizioneze):
      rfl_state        IDLE | ARMED | REBOUND
      rfl_peak_ref      peak-ul de referinta curent (in puncte)
      rfl_local_low     cel mai prost profit de cand e ARMED (in puncte)
      rfl_rebound_peak  cel mai bun profit din rebound-ul curent (in puncte)
    """
    profit_pts = _to_pts(profit, trade)
    peak_pts   = _to_pts(peak, trade)

    # ── GATE dublu — activ doar in fereastra [MIN, MAX) ────────────
    if peak_pts < RF_LOW_GATE_MIN_PTS or peak_pts >= RF_LOW_GATE_MAX_PTS:
        return False, None

    state    = trade.get("rfl_state", "IDLE")
    peak_ref = trade.get("rfl_peak_ref", peak_pts)

    if peak_pts > peak_ref:
        peak_ref = peak_pts

    # ── Profit a egalat/spart peak_ref -> new high real -> reset complet ──
    if profit_pts >= peak_ref:
        trade["rfl_state"]    = "IDLE"
        trade["rfl_peak_ref"] = profit_pts
        return False, None

    trade["rfl_peak_ref"] = peak_ref

    # ── IDLE -> ARMED ──────────────────────────────────────────────
    if state == "IDLE":
        dd = peak_ref - profit_pts
        if dd >= RF_LOW_ARM_PTS:
            trade["rfl_state"]        = "ARMED"
            trade["rfl_local_low"]    = profit_pts
            trade["rfl_rebound_peak"] = profit_pts
        return False, None

    # ── ARMED — track local_low, asteapta rebound valid ───────────
    if state == "ARMED":
        if profit_pts < trade.get("rfl_local_low", profit_pts):
            trade["rfl_local_low"]    = profit_pts
            trade["rfl_rebound_peak"] = profit_pts

        local_low = trade["rfl_local_low"]
        if profit_pts >= local_low + RF_LOW_REBOUND_PTS:
            trade["rfl_state"]        = "REBOUND"
            trade["rfl_rebound_peak"] = profit_pts
        return False, None

    # ── REBOUND — track rebound_peak, detecteaza rebound esuat ────
    if state == "REBOUND":
        local_low = trade.get("rfl_local_low", profit_pts)

        if profit_pts < local_low:
            return True, (f"RF_LOW_BREAK pk={round(peak_ref,1)} ll={round(local_low,1)} "
                          f"p={round(profit_pts,1)}")

        if profit_pts > trade.get("rfl_rebound_peak", profit_pts):
            trade["rfl_rebound_peak"] = profit_pts

        rebound_peak = trade["rfl_rebound_peak"]
        if profit_pts <= rebound_peak - RF_LOW_FAIL_PTS:
            return True, (f"RF_LOW pk={round(peak_ref,1)} ll={round(local_low,1)} "
                          f"rb={round(rebound_peak,1)} p={round(profit_pts,1)}")
        return False, None

    return False, None


def _rf_low_check_p2(trade, profit, peak):
    """
    P2_RF_LOW — Recovery Failure pentru zona de peak mic (2.9-15), POS2.
    Inlocuieste complet P2_GIVEBACK. Aceeasi structura ca P1_RF_LOW, dar
    mai relaxat: mai multa rabdare decat POS1 (ARM=5 vs 3), insa mult mai
    putina toleranta decat vechiul giveback fix (20pts indiferent de peak).

    Gate: activ doar cand RF_LOW_P2_GATE_MIN_PTS <= peak <= RF_LOW_P2_GATE_MAX_PTS.
    Peste acest prag, RF2 (15+) deja preia rolul de protectie/strangere.

    Filozofie (identica RF2/P1_RF_LOW, praguri proprii, mai relaxate):
      1. Peak nou (real)                     -> reset, IDLE
      2. Deteriorare >= RF_LOW_P2_ARM_PTS de la peak       -> ARMED
      3. Recovery >= RF_LOW_P2_REBOUND_PTS de la local_low -> REBOUND
      4. Cadere >= RF_LOW_P2_FAIL_PTS de la rebound_peak   -> EXIT (rebound esuat)
      5. In REBOUND, cadere sub local_low-ul care a generat reboundul
         -> EXIT direct

    State stocat in trade (prefix separat ca sa nu colizioneze cu rfl_*/rf2_*):
      rfl2_state        IDLE | ARMED | REBOUND
      rfl2_peak_ref      peak-ul de referinta curent (in puncte)
      rfl2_local_low     cel mai prost profit de cand e ARMED (in puncte)
      rfl2_rebound_peak  cel mai bun profit din rebound-ul curent (in puncte)
    """
    profit_pts = _to_pts(profit, trade)
    peak_pts   = _to_pts(peak, trade)

    # ── GATE dublu — activ doar in fereastra [MIN, MAX) ────────────
    if peak_pts < RF_LOW_P2_GATE_MIN_PTS or peak_pts >= RF_LOW_P2_GATE_MAX_PTS:
        return False, None

    state    = trade.get("rfl2_state", "IDLE")
    peak_ref = trade.get("rfl2_peak_ref", peak_pts)

    if peak_pts > peak_ref:
        peak_ref = peak_pts

    # ── Profit a egalat/spart peak_ref -> new high real -> reset complet ──
    if profit_pts >= peak_ref:
        trade["rfl2_state"]    = "IDLE"
        trade["rfl2_peak_ref"] = profit_pts
        return False, None

    trade["rfl2_peak_ref"] = peak_ref

    # ── IDLE -> ARMED ──────────────────────────────────────────────
    if state == "IDLE":
        dd = peak_ref - profit_pts
        if dd >= RF_LOW_P2_ARM_PTS:
            trade["rfl2_state"]        = "ARMED"
            trade["rfl2_local_low"]    = profit_pts
            trade["rfl2_rebound_peak"] = profit_pts
        return False, None

    # ── ARMED — track local_low, asteapta rebound valid ───────────
    if state == "ARMED":
        if profit_pts < trade.get("rfl2_local_low", profit_pts):
            trade["rfl2_local_low"]    = profit_pts
            trade["rfl2_rebound_peak"] = profit_pts

        local_low = trade["rfl2_local_low"]
        if profit_pts >= local_low + RF_LOW_P2_REBOUND_PTS:
            trade["rfl2_state"]        = "REBOUND"
            trade["rfl2_rebound_peak"] = profit_pts
        return False, None

    # ── REBOUND — track rebound_peak, detecteaza rebound esuat ────
    if state == "REBOUND":
        local_low = trade.get("rfl2_local_low", profit_pts)

        if profit_pts < local_low:
            return True, (f"RF_LOW_BREAK pk={round(peak_ref,1)} ll={round(local_low,1)} "
                          f"p={round(profit_pts,1)}")

        if profit_pts > trade.get("rfl2_rebound_peak", profit_pts):
            trade["rfl2_rebound_peak"] = profit_pts

        rebound_peak = trade["rfl2_rebound_peak"]
        if profit_pts <= rebound_peak - RF_LOW_P2_FAIL_PTS:
            return True, (f"RF_LOW pk={round(peak_ref,1)} ll={round(local_low,1)} "
                          f"rb={round(rebound_peak,1)} p={round(profit_pts,1)}")
        return False, None

    return False, None


def _rf_peaklow_check(trade, profit, peak):
    """
    P1_RF_PEAKLOW — Recovery Failure pentru peak FOARTE mic (sub 2.9), POS1.
    Inlocuieste complet P1_RF vechi (bazat pe worst_profit) cu aceeasi
    filozofie ARMED -> REBOUND -> FAIL ca RF2/RF_LOW, dar pe gate de peak.

    Gate: activ DOAR cand peak < RF_PEAKLOW_GATE_MAX_PTS. Peste acest prag,
    RF_LOW (3-15) si RF2 (15+) preiau — acest mecanism nu are ce cauta acolo.

    Filozofie (identica RF2/RF_LOW, praguri proprii):
      1. Peak nou (real)                  -> reset, IDLE
      2. Deteriorare >= RF_PEAKLOW_ARM_PTS de la peak       -> ARMED
      3. Recovery >= RF_PEAKLOW_REBOUND_PTS de la local_low -> REBOUND
      4. Cadere >= RF_PEAKLOW_FAIL_PTS de la rebound_peak   -> EXIT (rebound esuat)
      5. In REBOUND, cadere sub local_low-ul care a generat reboundul
         -> EXIT direct

    State stocat in trade (prefix separat ca sa nu colizioneze cu rf2_*/rfl_*):
      rfpl_state        IDLE | ARMED | REBOUND
      rfpl_peak_ref      peak-ul de referinta curent (in puncte)
      rfpl_local_low     cel mai prost profit de cand e ARMED (in puncte)
      rfpl_rebound_peak  cel mai bun profit din rebound-ul curent (in puncte)
    """
    profit_pts = _to_pts(profit, trade)
    peak_pts   = _to_pts(peak, trade)

    # ── GATE — activ doar sub sau la acest peak (2.9 inclus) ───────
    if peak_pts > RF_PEAKLOW_GATE_MAX_PTS:
        return False, None

    state    = trade.get("rfpl_state", "IDLE")
    peak_ref = trade.get("rfpl_peak_ref", peak_pts)

    if peak_pts > peak_ref:
        peak_ref = peak_pts

    # ── Profit a egalat/spart peak_ref -> new high real -> reset complet ──
    if profit_pts >= peak_ref:
        trade["rfpl_state"]    = "IDLE"
        trade["rfpl_peak_ref"] = profit_pts
        return False, None

    trade["rfpl_peak_ref"] = peak_ref

    # ── IDLE -> ARMED ──────────────────────────────────────────────
    if state == "IDLE":
        dd = peak_ref - profit_pts
        if dd >= RF_PEAKLOW_ARM_PTS:
            trade["rfpl_state"]        = "ARMED"
            trade["rfpl_local_low"]    = profit_pts
            trade["rfpl_rebound_peak"] = profit_pts
        return False, None

    # ── ARMED — track local_low, asteapta rebound valid ───────────
    if state == "ARMED":
        if profit_pts < trade.get("rfpl_local_low", profit_pts):
            trade["rfpl_local_low"]    = profit_pts
            trade["rfpl_rebound_peak"] = profit_pts

        local_low = trade["rfpl_local_low"]
        if profit_pts >= local_low + RF_PEAKLOW_REBOUND_PTS:
            trade["rfpl_state"]        = "REBOUND"
            trade["rfpl_rebound_peak"] = profit_pts
        return False, None

    # ── REBOUND — track rebound_peak, detecteaza rebound esuat ────
    if state == "REBOUND":
        local_low = trade.get("rfpl_local_low", profit_pts)

        if profit_pts < local_low:
            return True, (f"RF_PEAKLOW_BREAK pk={round(peak_ref,1)} ll={round(local_low,1)} "
                          f"p={round(profit_pts,1)}")

        if profit_pts > trade.get("rfpl_rebound_peak", profit_pts):
            trade["rfpl_rebound_peak"] = profit_pts

        rebound_peak = trade["rfpl_rebound_peak"]
        if profit_pts <= rebound_peak - RF_PEAKLOW_FAIL_PTS:
            return True, (f"RF_PEAKLOW pk={round(peak_ref,1)} ll={round(local_low,1)} "
                          f"rb={round(rebound_peak,1)} p={round(profit_pts,1)}")
        return False, None

    return False, None


def _no_peak_abort(trade, profit, peak):
    """
    NO_PEAK_ABORT — iese din trade-uri moarte care nu au generat niciun peak.
    Conditii (toate simultan):
      - peak == 0 (niciun profit atins vreodata)
      - loops >= 100 (am lasat suficient timp)
      - profit <= -8.0 (suntem deja in pierdere semnificativa)
      - Aurora s-a intors impotriva pozitiei (fast + slow opuse)
    """
    if peak > 0:
        return False, None

    loops = trade.get("loop_count", 0)
    if loops < 100:
        return False, None

    profit_pts = _to_pts(profit, trade)
    if profit_pts > -NPA_EXIT_PTS:
        return False, None

    signal   = trade["signal"]
    fast     = SS.get("fast_bias", "NEUTRAL")
    slow     = SS.get("slow_bias", "NEUTRAL")

    if signal == "BUY":
        if fast in ("SHORT", "NEUTRAL") and slow == "SHORT":
            return True, f"NO_PEAK_ABORT pk=0 p={round(profit,2)} loops={loops}"

    elif signal == "SELL":
        if fast in ("LONG", "NEUTRAL") and slow == "LONG":
            return True, f"NO_PEAK_ABORT pk=0 p={round(profit,2)} loops={loops}"

    return False, None


def _rf_profit(trade, profit, peak):
    """
    RF_PROFIT — detecteaza runneri care si-au pierdut puterea dupa peak >= 20$.
    Filozofie: nu iesim pentru ca a scazut, iesim pentru ca nu mai poate face maxime noi.

    State machine per trade (stocat in trade dict):
      rfp_state:          IDLE | ARMED | REBOUND
      rfp_local_low:      minimul atins dupa armare (se updateaza in ambele stari)
      rfp_rebound_peak:   maximul atins in reboundul curent
      rfp_failed_attacks: numar de atacuri esuate consecutive
      rfp_peak_ref:       peak-ul de referinta (se avanseaza doar la success attack)
    """
    if peak < RF_PROFIT_PEAK_MIN:
        return False, None

    # Lucram exclusiv in puncte
    lot      = trade.get("lot", 1.0)
    peak_pts = peak / lot if lot > 0 else peak
    prof_pts = profit / lot if lot > 0 else profit

    state    = trade.get("rfp_state", "IDLE")
    peak_ref = trade.get("rfp_peak_ref", peak_pts)
    fa       = trade.get("rfp_failed_attacks", 0)

    # ── Peak nou real → reset complet ─────────────────────────────
    if state != "IDLE" and prof_pts >= peak_ref + RF_PROFIT_MARGIN:
        trade["rfp_state"]          = "IDLE"
        trade["rfp_failed_attacks"] = 0
        trade["rfp_peak_ref"]       = prof_pts
        return False, None

    # ── IDLE → ARMED ──────────────────────────────────────────────
    if state == "IDLE":
        dd = peak_pts - prof_pts
        if dd >= RF_PROFIT_ARM_DD:
            trade["rfp_state"]          = "ARMED"
            trade["rfp_local_low"]      = prof_pts
            trade["rfp_rebound_peak"]   = prof_pts
            trade["rfp_failed_attacks"] = 0
            trade["rfp_peak_ref"]       = peak_pts
        return False, None

    # ── Track local_low in ORICE stare activa (in puncte) ─────────
    if prof_pts < trade.get("rfp_local_low", prof_pts):
        trade["rfp_local_low"]    = prof_pts
        trade["rfp_rebound_peak"] = prof_pts

    local_low = trade["rfp_local_low"]

    # ── ARMED — asteapta rebound valid ────────────────────────────
    if state == "ARMED":
        if prof_pts >= local_low + RF_PROFIT_REBOUND:
            trade["rfp_state"]        = "REBOUND"
            trade["rfp_rebound_peak"] = prof_pts
            if fa >= RF_PROFIT_MAX_FA:
                return True, (
                    f"RF_PROFIT fa={fa} pk={round(peak_ref,1)} "
                    f"p={round(prof_pts,1)} rb={round(prof_pts,1)}"
                )
        return False, None

    # ── REBOUND — track rebound_peak, detecteaza failed/success ──
    if state == "REBOUND":
        if prof_pts > trade.get("rfp_rebound_peak", prof_pts):
            trade["rfp_rebound_peak"] = prof_pts

        rebound_peak = trade["rfp_rebound_peak"]

        if prof_pts <= rebound_peak - RF_PROFIT_CONF_DD:
            if rebound_peak < peak_ref - RF_PROFIT_MARGIN:
                trade["rfp_failed_attacks"] = fa + 1
                fa = trade["rfp_failed_attacks"]
                trade["rfp_state"]        = "ARMED"
                trade["rfp_local_low"]    = prof_pts
                trade["rfp_rebound_peak"] = prof_pts
            else:
                trade["rfp_state"]          = "IDLE"
                trade["rfp_failed_attacks"] = 0
                trade["rfp_peak_ref"]       = rebound_peak
            return False, None

        if fa >= RF_PROFIT_MAX_FA:
            if prof_pts >= local_low + RF_PROFIT_REBOUND:
                return True, (
                    f"RF_PROFIT fa={fa} pk={round(peak_ref,1)} "
                    f"p={round(prof_pts,1)} rb={round(rebound_peak,1)}"
                )

    return False, None


def _should_close_pos1(trade, profit, peak, atr):
    loops = trade.get("loop_count", 0) + 1
    trade["loop_count"] = loops

    is_reentry      = trade.get("is_reentry", False)
    is_momentum     = trade.get("is_momentum", False)
    is_direct_entry = trade.get("is_direct_entry", False)

    # ── NO_PEAK_ABORT — trade mort fara niciun peak ───────────────
    abort, abort_reason = _no_peak_abort(trade, profit, peak)
    if abort:
        return True, abort_reason

    # ── AIRBAG ATR ────────────────────────────────────────────────
    if profit < -(atr * SL_ATR_MULT):
        return True, "P1_HARD_SL"

    # ── SL SOFTWARE US30 (points-based) ───────────────────────────
    lot = trade.get("lot", 1.0)
    profit_pts = _to_pts(profit, trade)

    if is_momentum or is_direct_entry:
        sl_pts = -P1_SL_MOMENTUM_PTS
    else:
        sl_pts = -P1_SL_REENTRY_PTS if is_reentry else -P1_SL_NORMAL_PTS
    if profit_pts <= sl_pts:
        return True, f"P1_SL({round(profit_pts,1)}pts)"

    # ── RF_PEAKLOW — Recovery Failure pentru peak sub 2.9, inlocuieste P1_RF vechi ──
    rfpl_close, rfpl_reason = _rf_peaklow_check(trade, profit, peak)
    if rfpl_close:
        return True, "P1_" + rfpl_reason

    # ── RF_LOW — Recovery Failure pentru peak mic (2.9-15), inlocuieste P1_PROTECT ──
    rfl_close, rfl_reason = _rf_low_check(trade, profit, peak)
    if rfl_close:
        return True, "P1_" + rfl_reason

    # ── RF2 — Recovery Failure unificat, activ doar peste peak>=15 (strangere profit) ──
    rf2_close, rf2_reason = _rf2_check(trade, profit, peak)
    if rf2_close:
        return True, "P1_" + rf2_reason

    # ── RF_PROFIT — runner si-a pierdut puterea ───────────────────
    rfp_close, rfp_reason = _rf_profit(trade, profit, peak)
    if rfp_close:
        return True, rfp_reason

    return False, ""


def _should_close_pos2(trade, profit, peak, atr):
    global _last_abort_time
    ss    = SS
    astr  = ss["aurora_strength"]
    abias = ss["aurora_bias"]
    sig   = trade["signal"]

    loops = trade.get("loop_count", 0) + 1
    trade["loop_count"] = loops

    # ── LIVE CONFIDENCE SCORE ─────────────────────────────────────
    # Dynamic — recalculat la fiecare loop
    # HIGH confidence = M15 aligned + str>7 → relaxam ușor exits
    # LOW confidence = M15 neutral/contra + str cade → revenim normal

    trade_dir  = "LONG" if sig == "BUY" else "SHORT"

    confidence = 0.0
    # str live — US30 max str = 5.0 (normalizare clipata)
    if astr >= 5.0:                confidence += 2.0
    elif astr >= 4.0:              confidence += 1.0
    elif astr < 2.0:               confidence -= 1.0
    # ATR healthy — US30 ATR normal e 40-60$
    if atr >= 30.0:                confidence += 0.5

    # confidence: -4 → +4.5, normalizat in 3 trepte
    # HIGH:   >= 3.0 → adaptive relaxation
    # NORMAL: 1.0-2.9 → comportament standard
    # LOW:    < 1.0  → mai defensiv
    if confidence >= 3.0:
        conf_level = "HIGH"
    elif confidence >= 1.0:
        conf_level = "NORMAL"
    else:
        conf_level = "LOW"

    # ── EXHAUSTION SCORE — strânge elasticul pe deteriorare progresivă ──
    # Nu reacționează la 1 candle slab — ci la motorul care moare lent
    exhaustion_score = 0.0

    # 1. Strength decay progresiv (str9→8→7→6→5 = semnal clar)
    str_now  = SS.get("aurora_strength", 0)
    str_prev = trade.get("prev_aurora_str", str_now)
    if str_now < str_prev:
        decay_rate = str_prev - str_now
        exhaustion_score += min(2.0, decay_rate * 0.5)
    trade["prev_aurora_str"] = str_now

    # 2. M15 a pierdut trendul
    m15_bias = SS.get("m15_bias", "NEUTRAL")
    trade_dir = "LONG" if trade["signal"] == "BUY" else "SHORT"
    if m15_bias != trade_dir and m15_bias != "NEUTRAL":
        exhaustion_score += 2.0   # M15 contra = semn mare
    elif m15_bias == "NEUTRAL":
        exhaustion_score += 0.5   # M15 neutral = pierde suportul

    # 3. Profitul nu mai recuperează — lower highs
    prev_peak = trade.get("prev_peak_check", peak)
    loops_since_peak = loops - trade.get("peak_loop", loops)
    if loops_since_peak > 20 and peak == prev_peak:
        exhaustion_score += 1.0   # peak stagnant > 20 loops
    trade["prev_peak_check"] = peak

    # 4. Recovery slab dupa drawdown (points-based)
    worst_pts_ex = _to_pts(trade["worst_profit"], trade)
    if worst_pts_ex < -P2_EXHAUSTION_DD_PTS:
        recovery_ratio = (profit - trade["worst_profit"]) / max(abs(trade["worst_profit"]), 0.1)
        if recovery_ratio < 0.3:
            exhaustion_score += 1.0   # a recuperat sub 30% din DD

    # 5. Timp de la peak
    if loops_since_peak > 40:
        exhaustion_score += 0.5

    exhaustion_score = min(exhaustion_score, 6.0)  # cap la 6

    # Aplica exhaustion: reduce elastic giveback
    # fresh (score=0) → elastic_mult=1.0 | obosit (score=6) → elastic_mult=0.52
    elastic_mult = max(0.5, 1.0 - exhaustion_score * 0.08)

    # ── SL absolut (points-based) ──────────────────────────────────
    lot        = trade.get("lot", 1.0)
    profit_pts = _to_pts(profit, trade)
    worst_pts  = _to_pts(trade["worst_profit"], trade)

    if profit_pts <= -P2_SL_ABS_PTS:
        return True, f"P2_SL_ABS p={round(profit_pts,1)}pts"

    # ── RF_PEAKLOW — Recovery Failure pentru peak sub 2.9, inlocuieste P2_RF vechi ──
    # Praguri identice cu POS1 (vezi RF_PEAKLOW_ARM_PTS/REBOUND_PTS/FAIL_PTS)
    rfpl_close, rfpl_reason = _rf_peaklow_check(trade, profit, peak)
    if rfpl_close:
        return True, "P2_" + rfpl_reason

    # ── RF_LOW (POS2) — Recovery Failure pentru peak 2.9-15, inlocuieste P2_GIVEBACK ──
    # Mai relaxat decat POS1: ARM=5, REBOUND=2, FAIL=2
    rfl2_close, rfl2_reason = _rf_low_check_p2(trade, profit, peak)
    if rfl2_close:
        return True, "P2_" + rfl2_reason

    # ── RF2 — Recovery Failure unificat, activ doar peste peak>=15 (strangere profit) ──
    rf2_close, rf2_reason = _rf2_check(trade, profit, peak)
    if rf2_close:
        return True, "P2_" + rf2_reason

    # ── ELASTIC RECOVERY EXIT (points-based) ──────────────────────
    if loops >= 30 and worst_pts <= -P2_ELASTIC_ARM_PTS:
        recovery     = profit - trade["worst_profit"]
        recovery_pts = _to_pts(recovery, trade)
        if recovery_pts >= P2_ELASTIC_REC_PTS:
            momentum_restored = (
                SS.get("aurora_strength", 0) >= 4.5 and   # US30 max str=5.0
                SS.get("aurora_bias") == ("LONG" if trade["signal"] == "BUY" else "SHORT") and
                trade.get("adverse_lows", 0) <= 5
            )
            if not momentum_restored:
                return True, f"P2_DR wp={round(worst_pts,1)} r=+{round(recovery_pts,1)}"

    # ── STRUCTURAL DECAY EXIT US30 ────────────────────────────────
    # decay_confirmed → exit pe rebound inteligent
    # recovery >= 5$ (era 1$) — US30 zgomot normal > 1$
    if trade.get("decay_confirmed"):
        recovery_from_low = profit - trade["worst_profit"]
        if recovery_from_low >= 5.0:
            return True, f"P2_DE al={trade['adverse_lows']} r=+{round(recovery_from_low,1)}"

    # ── NO_PEAK_ABORT — trade mort fara niciun peak ───────────────
    abort, abort_reason = _no_peak_abort(trade, profit, peak)
    if abort:
        return True, abort_reason

    # ── AIRBAG ────────────────────────────────────────────────────
    if profit < -(atr * SL_ATR_MULT):
        return True, "P2_HARD_SL_AIRBAG"

    return False, ""


# ══════════════════════════════════════════════════════════════════
# POST-SL REVERSE
# ══════════════════════════════════════════════════════════════════

def _check_post_sl_reverse():
    global _last_sl_time, _last_sl_signal
    if _last_sl_time is None: return False

    elapsed = (datetime.now() - _last_sl_time).total_seconds()
    if elapsed < POST_SL_COOLDOWN_SEC: return False

    ss = SS

    # Aurora trebuie sa confirme directia OPUSA fata de SL
    if _last_sl_signal == "BUY":
        confirmed = (
            ss["aurora_bias"] == "SHORT"
            and ss["aurora_strength"] >= POST_SL_MIN_STR
        )
    else:
        confirmed = (
            ss["aurora_bias"] == "LONG"
            and ss["aurora_strength"] >= POST_SL_MIN_STR
        )

    if confirmed:
        _last_sl_time   = None
        _last_sl_signal = None
        return True

    # Prea mult timp trecut — abandonam
    if elapsed > 120:
        _last_sl_time   = None
        _last_sl_signal = None

    return False


def _can_reenter_pos1():
    """
    Re-entry POS1 cand:
    - POS1 a iesit pe pierdere
    - POS2 e runner si a atins drawdown >= -2$
    - POS2 si-a revenit (profit > -0.5$)
    - Aurora str >= 9 — trendul se reface
    - directia Aurora == directia POS2
    """
    if not _reentry["active"]: return False, ""

    ss   = SS
    bias = ss["aurora_bias"]
    astr = ss["aurora_strength"]

    runner_dir = _reentry["signal"]
    aurora_dir = "BUY" if bias == "LONG" else "SELL" if bias == "SHORT" else None

    # Daca Aurora e NEUTRAL/NONE dar M15 e aliniat cu runner → permite reentry
    # Aurora poate fi temporar NONE in trend lent dar M15 confirma directia
    m15_dir = "BUY" if "NEUTRAL" == "LONG" else "SELL" if "NEUTRAL" == "SHORT" else None
    effective_dir = aurora_dir or (m15_dir if m15_dir == runner_dir else None)

    if effective_dir != runner_dir:
        return False, f"RE_DIR({bias}!={runner_dir})"

    if _exhaustion["active"]:
        return False, "RE_EXHAUSTION"

    spread = _get_spread()
    if spread > ENTRY_MAX_SPREAD:
        return False, f"RE_SPREAD({spread})"

    # POS2 trebuie sa fi fost in drawdown >= -0.5$
    if not _reentry["pos2_was_down"]:
        return False, f"RE_WAIT_DOWN(min={round(_reentry['pos2_min_profit'],2)})"

    # Daca POS2 a revenit pe profit dupa drawdown → re-entry imediat
    # Nu mai astepta str sau Aurora — price action a confirmat deja
    p2_profit = _reentry.get("pos2_current_profit", 0)
    if p2_profit >= 0.3:
        return True, f"RE_RECOVERY(str={round(astr,1)})"

    # Altfel: Aurora str >= 4.0 sau M15 aliniat
    if astr < 4.0 and m15_dir != runner_dir:
        return False, f"RE_STR_LOW({round(astr,1)})"

    return True, f"RE_RECOVERY(str={round(astr,1)})"


def _open_pos1_reentry(runner_trade):
    """
    Redeschide DOAR POS1 in directia runner-ului POS2.
    Nu atinge POS2. Nu reseteaza runner-ul.
    """
    global _reentry
    ss     = SS
    signal = runner_trade["signal"]
    lot    = _calc_lot()
    atr    = ss["recent_atr"]
    spread = _get_spread()

    tick = mt5.symbol_info_tick(SYMBOL)
    if not tick: return None
    price = tick.ask if signal == "BUY" else tick.bid

    sl = price - 20.0 if signal == "BUY" else price + 20.0   # US30: 20$ fix

    res = _send_order(signal, lot, sl, 0.0,
                      f"v5|POS1|REENTRY|s{round(ss['aurora_strength'],1)}")
    if not res:
        return None

    trade = {
        "ticket":             res["ticket"],
        "entry_price":        res["entry_price"],
        "signal":             signal,
        "lot":                lot,
        "sl":                 sl,
        "tp":                 0.0,
        "pos_type":           "POS1",
        "peak":               0.0,
        "entry_time":         datetime.now(),
        "aurora_str":         ss["aurora_strength"],
        "aurora_mode":        ss["aurora_mode"],
        "aurora_bias":        ss["aurora_bias"],
        "m15_bias":           ss.get("m15_bias", ""),
        "atr":                atr,
        "spread_entry":       spread,
        "hour":               datetime.now().hour,
        "daily_pnl_at_entry": _daily_pnl,
        "be_done":            False,
        "pair_ticket":        runner_trade["ticket"],
        "is_reverse":         0,
        "post_cooldown":      False,
        "is_reentry":         True,    # flag pentru BE mai agresiv
    }

    print(f"  OPEN {signal} [POS1/REENTRY] "
          f"price={round(res['entry_price'],2)} "
          f"sl={round(sl,2)} lot={lot} #{res['ticket']}")
    tg_open("POS1/REENTRY", signal, res['entry_price'], sl, lot,
            SS["aurora_strength"], SS["aurora_mode"])
    _log_open(trade, _daily_pnl, spread, False)

    # Dezactivam re-entry dupa ce am deschis (max 1 activ)
    _reentry["active"] = False

    return trade


# ══════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════

def run():
    global _last_sl_time, _last_sl_signal, _last_exit_price, _last_exit_peak, _last_exit_signal
    global _last_abort_time, _exhaustion, _mo_consumed
    global _target_hit, _target_stopped

    print("\n" + "="*65)
    print("  US30 BOT v1.0 — Dual Position Architecture")
    print("  POS1=Cash Collector | POS2=Trend Runner")
    print("  TRAIL SL in trepte | SL fix 20$ | 15:30-22:00 NYSE")
    print("="*65 + "\n")

    if not mt5.initialize():
        print("MT5 init failed"); return
    acc = mt5.account_info()
    tg(
        f"🤖 <b>US30 BOT PORNIT</b>\n"
        f"Cont: <code>{acc.login}</code> | {acc.server}\n"
        f"Balanta: <code>{round(acc.balance, 2)}$</code>\n"
        f"SL: {SL_ATR_MULT}×ATR | POS1+POS2 dual arch\n"
        f"✅ Telegram activ"
    )
    print(f"  Cont: {acc.login} | Broker: {acc.server}")
    print(f"  Balanta: {acc.balance} | Margin: {acc.margin_free}\n")
    print(f"  ENTRY: str >= 4.5 | EARLY_TREND/NORMAL | 15:30-22:00")
    print(f"  SL: 20$ fix + trailing in trepte | TP: dezactivat")
    print(f"  POS1 exit: progressive lock US30 | POS2: giveback 20$ de la peak\n")

    open_trades = []
    loop        = 0
    _df_cached  = None

    while True:
        loop += 1
        time.sleep(0.25)

        try:
            # get_data + add_features la fiecare 4 loop-uri (~1s)
            # Bara M5 se schimba la fiecare 5 minute — 1s e suficient
            # Elibereaza loop-urile 2/3/4 pentru management pozitii fara blocare
            if loop % 4 == 1 or _df_cached is None:
                df_raw = get_data()
                if df_raw is None or len(df_raw) < 30:
                    if _df_cached is None: continue
                else:
                    _df_cached = add_features(df_raw)
            df = _df_cached

            _check_daily_reset()
            _update_state(df)

            # ── TARGET STOP ────────────────────────────────────────
            _can_trade_now()
            if _target_stopped:
                if not getattr(run, '_target_printed', False):
                    print(f"  [TARGET] STOP — {DAILY_PROFIT_TARGET}$ atins | pnl={round(_daily_pnl,2)}$")
                    run._target_printed = True
                time.sleep(5)
                continue

            ss     = SS
            atr    = ss["recent_atr"]
            spread = _get_spread()

            # ── Scrie aurora_state.json — la fiecare 10 loop-uri ──
            if loop % 10 == 0:
                _write_aurora_state(open_trades)

            # ── FLOW_TRANSFER_MODE ─────────────────────────────────
            if _ftm["active"]:
                _ftm["last_mode"] = ss.get("aurora_mode", "")
                tick_ftm = mt5.symbol_info_tick(SYMBOL)
                if tick_ftm:
                    _update_ftm(tick_ftm.bid, atr)

            if loop % 40 == 0:
                _market_snapshot(open_trades, atr, spread)

            # ── MANAGE OPEN TRADES ────────────────────────────────
            open_trades = _sync_open_trades(open_trades)
            to_close = []

            # Batch positions_get — o singura citire pentru toate profiturile
            _live_positions = mt5.positions_get(symbol=SYMBOL)
            _profit_cache = {}
            if _live_positions:
                for _p in _live_positions:
                    _profit_cache[_p.ticket] = float(_p.profit)

            for trade in open_trades:
                profit = _profit_cache.get(trade["ticket"],
                         trade.get("last_known_profit", 0.0))
                # Salveaza profitul curent ca fallback pentru sync
                trade["last_known_profit"] = profit
                peak   = trade["peak"]

                if profit > peak:
                    trade["peak"] = profit
                    trade["atr_at_peak"] = atr   # ATR memory pentru elastic floor
                    peak = profit

                _log_runner(trade, profit, peak, trade.get("loop_count", 0))
                _check_be(trade, peak)

                if trade["pos_type"] == "POS1":
                    if profit < trade.get("worst_profit", 0):
                        trade["worst_profit"] = profit
                    should, reason = _should_close_pos1(trade, profit, peak, atr)
                else:
                    # Actualizeaza decay engine inainte de exit check
                    _update_decay_engine(trade, profit, trade.get("spread_entry", 0.17))
                    should, reason = _should_close_pos2(trade, profit, peak, atr)

                if should and not trade.get("closing", False):
                    trade["closing"] = True
                    trade["exit_reason_pending"] = reason
                    to_close.append((trade, reason))

            # POS1 iese intotdeauna inaintea POS2
            to_close.sort(key=lambda x: 0 if x[0]["pos_type"] == "POS1" else 1)

            for trade, reason in to_close:
                ep     = _get_exit_price(trade["signal"])
                profit = _profit_cache.get(trade["ticket"],
                         trade.get("last_known_profit", 0.0))

                success = False
                if _close_order(trade["ticket"], trade["signal"],
                                trade["lot"], reason):
                    success = True

                if success:
                    emoji = "+" if profit > 0 else "-"
                    print(f"  {emoji} CLOSE [{trade['pos_type']}] "
                          f"{reason} "
                          f"profit={round(profit,2)} "
                          f"peak={round(trade['peak'],2)}")
                    _log_close(trade, ep, profit, reason)
                    tg_close(trade["pos_type"], trade["signal"],
                             profit, trade["peak"], reason)
                    _update_stats(profit)

                    # Salveaza pretul de exit + peak + signal pentru revers detection
                    _last_exit_price  = ep
                    _last_exit_peak   = trade["peak"]
                    _last_exit_signal = trade["signal"]

                    # POST-PEAK MODE / FLOW_TRANSFER_MODE
                    if trade["pos_type"] == "POS2":
                        if trade["peak"] >= 40.0:   # US30: peak semnificativ = 40$+
                            if _ftm.get("cooldown_until") and datetime.now() < _ftm["cooldown_until"]:
                                print(f"  [FTM] cooldown activ, skip activare")
                            else:
                                # FLOW_TRANSFER_MODE — peak major, bridge intre flow-uri
                                global _post_peak_until
                                _ftm["active"]         = True
                                _ftm["start_time"]     = datetime.now()
                                _ftm["old_direction"]  = trade["signal"]
                                _ftm["exit_price"]     = ep
                                _ftm["peak"]           = trade["peak"]
                                _ftm["atr"]            = atr
                                _ftm["phase"]          = 1
                                _ftm["verdict"]        = None
                                _ftm["ftm_direction"]  = None
                                _ftm["new_direction"]  = None
                                _ftm["new_armed"]      = False
                                _ftm["close_pos1_old"] = False
                                _ftm["score"]          = 0
                                _ftm["retrace_live"]   = 0.0
                                _ftm["contra_weighted"]= 0.0
                                _ftm["contra_buf"]     = []
                                _ftm["micro_bo_count"] = 0
                                _ftm["last_price"]     = ep
                                _ftm["last_mode"]      = ""
                                _ftm["_logged_dir"]    = None
                                _post_peak_until       = None
                                _exhaustion["active"]  = False
                                print(f"\n  ══ [FTM] ACTIVE peak={round(trade['peak'],2)}$ "
                                      f"dir={trade['signal']} ══")
                                print(f"  [FTM] PHASE 1 → observation 0-60s")
                        elif trade["peak"] >= 15.0:
                            # POST_PEAK normal pentru peak medii US30
                            _post_peak_until = datetime.now() + timedelta(seconds=20)
                            print(f"  [POST_PEAK] Aurora dezactivata 20s "
                                  f"peak={round(trade['peak'],2)}$")

                    # EXHAUSTION — dupa POS2 exit mare, intra in observation mode
                    # NU se activeaza daca FTM e activ — FTM preia controlul
                    if trade["pos_type"] == "POS2" and trade["peak"] >= 20.0 and not _ftm["active"]:
                        _exhaustion["active"]        = True
                        _exhaustion["direction"]     = trade["signal"]
                        _exhaustion["exit_price"]    = ep
                        _exhaustion["peak"]          = trade["peak"]
                        _exhaustion["atr"]           = atr
                        _exhaustion["pullback_seen"] = False
                        _exhaustion["start_time"]    = datetime.now()
                        print(f"  [EXHAUSTION] peak={round(trade['peak'],2)}$ "
                              f"dir={trade['signal']} exit={round(ep,2)} → obs fara timeout")

                    # Reset counters dupa close reusit (Fix #1+2)
                    trade["p1_under_floor_count"] = 0
                    trade["p2_under_floor_count"] = 0
                    trade["closing"] = False
                    trade["exit_reason_pending"] = None

                    # SL/airbag lovit → inregistram pentru post-SL reverse + blocare pb
                    if "HARD_SL" in reason or "AIRBAG" in reason:
                        _last_sl_time   = datetime.now()
                        _last_sl_signal = trade["signal"]
                        _last_sl_reason = "HARD_SL" if "HARD_SL" in reason else "AIRBAG"
                        print(f"  SL lovit [{trade['signal']}] -> astept {POST_SL_COOLDOWN_SEC}s + confirmare Aurora")
                        tg_sl(trade["signal"], profit, reason)

                    # Scoatem din lista DOAR daca close a reusit
                    open_trades = [t for t in open_trades
                                   if t["ticket"] != trade["ticket"]]

                    # ── POS1 RE-ENTRY ACTIVATION ──────────────────
                    # Se activeaza DOAR cand POS1 iese pe pierdere
                    # RE-ENTRY POS1 dezactivat momentan
                    # Piata nu ofera conditii potrivite
                    # Reactivat cand stilul de piata se schimba
                    pass

                else:
                    print(
                        f"  CLOSE FAILED -> "
                        f"ticket={trade['ticket']} "
                        f"reason={reason}"
                    )
                    # Fix #3 — reset closing flag la fail
                    trade["closing"] = False


            # ── FTM: inchide POS1 pe directia veche la REVERSAL Phase 2 ──
            # Executat INAINTE de PB tracker — altfel has_open_positions=True
            # blocheaza ARM-ul FTM pe noua directie
            if _ftm.get("close_pos1_old") and _ftm.get("phase") == 2:
                for trade in list(open_trades):
                    if trade["pos_type"] == "POS1":
                        old_sig = _ftm["old_direction"]
                        if trade["signal"] == old_sig:
                            profit = _get_profit(trade)
                            success = _close_order(trade["ticket"], trade["signal"],
                                                   trade["lot"], "FTM_REVERSAL")
                            if success:
                                print(f"  [FTM] POS1 CLOSED reason=FTM_REVERSAL "
                                      f"p={round(profit,2)}$")
                                _ftm["close_pos1_old"] = False
                                open_trades = [t for t in open_trades
                                               if t["ticket"] != trade["ticket"]]
                            else:
                                print(f"  [FTM] POS1 CLOSE FAILED — hedge risc!")
                open_trades = _sync_open_trades(open_trades)

            # ── ENTRY ─────────────────────────────────────────────
            # Actualizeaza pullback tracker mereu (cu sau fara pozitii)
            # has_open_positions=True → blocheaza armarea noua (HOLD MODE)
            # ── MOMENTUM OBSERVATION ──────────────────────────────
            _mo_triggered = _update_momentum_obs(
                price       = ss.get("last_close", 0.0),
                bias        = ss["aurora_bias"],
                astr        = ss["aurora_strength"],
                open_trades = open_trades,
            )
            if _mo_triggered and len(open_trades) == 0 and _can_trade_now():
                sig = "BUY" if ss["aurora_bias"] == "LONG" else "SELL"
                _pb["bias"]           = ss["aurora_bias"]
                _pb["momentum_entry"] = True              # INAINTE de _can_enter()
                ok_mo, reason_mo = _can_enter()
                if ok_mo:
                    print(f"\n  [MO] RUNAWAY ENTRY {ss['aurora_bias']} "
                          f"str={round(ss['aurora_strength'],1)}")
                    _t_od0 = time.perf_counter()
                    _open_dual._mo_pure_entry = True
                    new_trades = _open_dual(is_reverse=False)
                    _open_dual._mo_pure_entry = False
                    _t_od1 = time.perf_counter()
                    print(f"  [TIMING] _open_dual(MO_RUNAWAY) TOTAL={round((_t_od1-_t_od0)*1000,1)}ms")
                    open_trades.extend(new_trades)
                    _pb["momentum_entry"] = False  # reset doar flag-ul, PB memory intacta
                else:
                    _pb["momentum_entry"] = False
                    _pb["bias"]           = None
                    print(f"  [MO] runaway blocked: {reason_mo}")

            _update_pullback_tracker(has_open_positions=len(open_trades) > 0)

            pos1_open = any(t["pos_type"] == "POS1" for t in open_trades)
            pos2_open = any(t["pos_type"] == "POS2" for t in open_trades)

            if len(open_trades) == 0 and _can_trade_now():
                # Fara pozitii deschise — reset reentry daca era activ
                if _reentry["active"]:
                    _reentry["active"] = False

                # Post-SL Reverse
                if _last_sl_time is not None and _check_post_sl_reverse():
                    print(f"\n  REVERSE -> {ss['aurora_bias']} "
                          f"str={round(ss['aurora_strength'],1)}")
                    _t_od0 = time.perf_counter()
                    new_trades = _open_dual(is_reverse=True)
                    _t_od1 = time.perf_counter()
                    print(f"  [TIMING] _open_dual(REVERSE) TOTAL={round((_t_od1-_t_od0)*1000,1)}ms")
                    open_trades.extend(new_trades)

                else:
                    # ── POST-SL REBUILD WAIT ──────────────────────
                    # Daca suntem in fereastra post-SL (< 120s):
                    # NU mai folosim _can_enter() direct — lasa exhaustion
                    # si PB engine sa valideze continuation nou.
                    # Evita re-entry in aceeasi directie din lag Aurora.
                    post_sl_active = (
                        _last_sl_time is not None and
                        (datetime.now() - _last_sl_time).total_seconds() < 120
                    )
                    if post_sl_active:
                        # Blocat — asteptam rebuild structural sau reverse
                        # Exhaustion/PB tracker lucreaza in background
                        _log_signal(ss["aurora_bias"], ss["aurora_mode"],
                                    ss["aurora_strength"], "POST_SL_WAIT")
                    else:
                        # 120s trecute — deblocam, resetam sl state
                        if _last_sl_time is not None:
                            _last_sl_time   = None
                            _last_sl_signal = None

                        ok, reason = _can_enter()
                        if not ok:
                            # Daca PB e direct entry dar alt gate blocheaza — forteaza entry
                            if _pb.get("momentum_entry") and reason not in ("LOSS_COOLDOWN", "SPREAD"):
                                print(f"  [PB] DIRECT ENTRY forced (gate={reason})")
                                ok = True
                        if not ok:
                            _log_signal(ss["aurora_bias"], ss["aurora_mode"],
                                        ss["aurora_strength"], reason)
                        else:
                            # Afiseaza directia reala — din PB daca Aurora e NEUTRAL
                            eff_bias = _pb["bias"] if (ss["aurora_bias"] == "NEUTRAL" and _pb["bias"]) else ss["aurora_bias"]
                            print(f"\n  DUAL {eff_bias} "
                                  f"str={round(ss['aurora_strength'],1)} "
                                  f"mode={ss['aurora_mode']}")
                            _t_od0 = time.perf_counter()
                            new_trades = _open_dual(is_reverse=False)
                            _t_od1 = time.perf_counter()
                            print(f"  [TIMING] _open_dual(DUAL) TOTAL={round((_t_od1-_t_od0)*1000,1)}ms")
                            open_trades.extend(new_trades)

            elif pos2_open and not pos1_open:
                # Doar POS2 deschis — urmarim drawdown + recovery pentru re-entry
                if _reentry["active"]:
                    runner = next((t for t in open_trades if t["pos_type"] == "POS2"), None)
                    if runner:
                        p2_profit = _get_profit(runner)

                        # Actualizam minimul de profit al POS2
                        if p2_profit < _reentry["pos2_min_profit"]:
                            _reentry["pos2_min_profit"] = p2_profit

                        # Salvam profitul curent pentru check re-entry rapid
                        _reentry["pos2_current_profit"] = p2_profit

                        # Marcam daca POS2 a atins drawdown >= -0.5$
                        if _reentry["pos2_min_profit"] <= -0.5:
                            _reentry["pos2_was_down"] = True

                        # Verificam re-entry DOAR daca POS2 si-a revenit
                        # (a fost la -2$+ dar acum e peste -0.5$)
                        pos2_recovered = (
                            _reentry["pos2_was_down"] and
                            p2_profit >= -0.5
                        )
                        if pos2_recovered:
                            ok, re_reason = _can_reenter_pos1()
                            if ok:
                                print(f"\n  [REENTRY] POS1 → {runner['signal']} ({re_reason})"
                                      f" POS2_min={round(_reentry['pos2_min_profit'],2)}"
                                      f" POS2_now={round(p2_profit,2)}")
                                new_pos1 = _open_pos1_reentry(runner)
                                if new_pos1:
                                    open_trades.append(new_pos1)

            if loop % 8 == 0 or open_trades:
                wr  = round(_total_wins / max(_total_trades,1) * 100, 1)
                fl  = sum(_get_profit(t) for t in open_trades)
                acc = mt5.account_info()
                bal = round(acc.balance,2) if acc else 0
                tick= mt5.symbol_info_tick(SYMBOL)
                pr  = round(tick.bid,2) if tick else 0
                pos = ""
                for t in open_trades:
                    p = _get_profit(t)
                    pos += (f" | {t['pos_type']}:{t['signal']}"
                            f" P={round(p,2)} pk={round(t['peak'],2)}"
                            )
                fast_str  = str(ss.get('fast_bias','?') or '?')
                slow_str  = str(ss.get('slow_bias','?') or '?')
                print(f"[{loop}] {pr} "
                      f"FAST:{fast_str} SLOW:{slow_str} "
                      f"M5:{ss['aurora_bias']}/{ss['aurora_mode']}/str{round(ss['aurora_strength'],1)} "
                      f"| ATR:{round(atr,2)} SPR:{spread} "
                      f"| BAL:{bal} FL:{round(fl,2)} DAY:{round(_daily_pnl,2)} "
                      f"| W:{_total_wins}/{_total_trades} WR:{wr}% "
                      f"POS:{len(open_trades)}{pos}")

            # ── MARKET CONTEXT — calcul si print la fiecare 30 loop-uri ──
            # Complet izolat de logica de trading, nu influenteaza nicio decizie
            if loop % _MC_PRINT_EVERY == 0:
                tick_mc = mt5.symbol_info_tick(SYMBOL)
                if tick_mc:
                    # MSE update — Balance Zone Engine
                    try:
                        _lr = df.iloc[-1]
                        _mse.update(
                            float(_lr.get("high", _lr["close"])),
                            float(_lr.get("low",  _lr["close"])),
                            str(_lr.name)[:16]
                        )
                    except Exception:
                        pass
                    _compute_market_context(
                        price_now   = tick_mc.bid,
                        atr         = ss["recent_atr"],
                        aurora_bias = ss["aurora_bias"],
                        aurora_str  = ss["aurora_strength"],
                    )

        except KeyboardInterrupt:
            print("\nOprit manual.")
            break
        except Exception as e:
            print(f"  Loop error: {e}")
            import traceback; traceback.print_exc()
            time.sleep(1)

    print("\nInchid pozitii deschise...")
    for trade in open_trades:
        if _close_order(trade["ticket"], trade["signal"], trade["lot"], "MANUAL_CLOSE"):
            profit = _get_profit(trade)
            _update_stats(profit)
            print(f"  Closed [{trade['pos_type']}] profit={round(profit,2)}")

    mt5.shutdown()
    print(f"\nZi finalizata.")
    print(f"  Trades:{_total_trades} Wins:{_total_wins} "
          f"WR:{round(_total_wins/max(_total_trades,1)*100,1)}%")
    print(f"  PnL:{round(_daily_pnl,2)}$")


if __name__ == "__main__":
    run()
