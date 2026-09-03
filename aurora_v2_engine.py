"""
Aurora v2 Engine — modul complet izolat
Se adauga in botul live FARA sa atinga logica existenta.
Ruleaza in paralel, doar logeaza. Zero impact pe trading.

Contine:
  - Market Structure Engine (Sprint 1)
  - PB Geometry v2 (MID ± 10p)
  - Decision Engine (log only)
"""

from dataclasses import dataclass, field
from typing import Optional, List
from enum import Enum
import time


# ── ENUMS ─────────────────────────────────────────────────────

class ZoneState(Enum):
    SEARCHING        = "SEARCHING"
    BUILDING         = "BUILDING"
    CONFIRMED        = "CONFIRMED"
    BREAKOUT_PENDING = "BREAKOUT_PENDING"
    BROKEN           = "BROKEN"
    ARCHIVED         = "ARCHIVED"
    TRADED           = "TRADED"   # zona a generat o oportunitate — nu mai produce semnale

class BreakoutSide(Enum):
    UP   = "UP"
    DOWN = "DOWN"
    NONE = "NONE"

class DecisionType(Enum):
    WAIT     = "WAIT"
    CONTRA   = "CONTRA"
    PULLBACK = "PULLBACK"
    RUNAWAY  = "RUNAWAY"
    NO_TRADE = "NO_TRADE"


# ── BALANCE ZONE ──────────────────────────────────────────────

@dataclass
class BalanceZone:
    id:                 int          = 0
    state:              ZoneState    = ZoneState.SEARCHING
    hh:                 float        = 0.0
    ll:                 float        = 0.0
    mid:                float        = 0.0
    rang:               float        = 0.0
    touches:            int          = 0
    breakout_side:      BreakoutSide = BreakoutSide.NONE
    breakout_confirmed: bool         = False
    created_at:         str          = ""
    confirmed_at:       str          = ""
    broken_at:          str          = ""
    age:                int          = 0

    def mark_traded(self):
        """Marcheaza zona ca tranzactionata. Nu mai produce semnale."""
        self.state = ZoneState.TRADED

    def is_traded(self) -> bool:
        return self.state == ZoneState.TRADED

    def pb_arm(self, aurora_bias: str, offset: float = 10.0) -> Optional[float]:
        """
        PB Geometry v2:
          Aurora LONG  → PB_ARM = MID + offset
          Aurora SHORT → PB_ARM = MID - offset
        """
        if self.mid == 0.0:
            return None
        if aurora_bias == "LONG":
            return round(self.mid + offset, 1)
        elif aurora_bias == "SHORT":
            return round(self.mid - offset, 1)
        return None


# ── MARKET STRUCTURE ENGINE ───────────────────────────────────

TIGHT_PTS       = 3.0
MIN_TOUCHES     = 3
CONFIRM_OUTSIDE = 2

_zone_counter = 0

def _next_zone_id():
    global _zone_counter
    _zone_counter += 1
    return _zone_counter


class MarketStructureEngine:
    def __init__(self):
        self._active:   Optional[BalanceZone] = None
        self._history:  List[BalanceZone]     = []
        self._outside:  int                   = 0

    def update(self, high: float, low: float, time_str: str) -> Optional[BalanceZone]:
        if self._active is None:
            self._start_zone(high, low, time_str)
        else:
            self._process(high, low, time_str)
        return self._active

    def last_dead_zone(self) -> Optional[BalanceZone]:
        dead = [z for z in self._history
                if z.state in (ZoneState.BROKEN, ZoneState.ARCHIVED)]
        return dead[-1] if dead else None

    def _start_zone(self, h, l, t):
        self._active = BalanceZone(
            id=_next_zone_id(), state=ZoneState.BUILDING,
            hh=h, ll=l, mid=(h+l)/2, rang=h-l,
            touches=1, created_at=t,
        )
        self._outside = 0

    def _process(self, h, l, t):
        z = self._active
        in_zone = (h <= z.hh + TIGHT_PTS and l >= z.ll - TIGHT_PTS) or \
                  abs(h - z.hh) <= TIGHT_PTS or abs(l - z.ll) <= TIGHT_PTS

        if in_zone:
            self._outside = 0
            z.touches += 1
            z.age     += 1
            if h > z.hh: z.hh = h
            if l < z.ll: z.ll = l
            z.mid  = round((z.hh + z.ll) / 2, 1)
            z.rang = round(z.hh - z.ll, 1)
            if z.touches >= MIN_TOUCHES and z.state == ZoneState.BUILDING:
                z.state = ZoneState.CONFIRMED
                z.confirmed_at = t
            if z.state == ZoneState.BREAKOUT_PENDING:
                z.state = ZoneState.CONFIRMED
                z.breakout_side = BreakoutSide.NONE
                self._outside = 0
        else:
            self._outside += 1
            if z.state == ZoneState.CONFIRMED and self._outside == 1:
                z.state = ZoneState.BREAKOUT_PENDING
                if h > z.hh + TIGHT_PTS: z.breakout_side = BreakoutSide.UP
                elif l < z.ll - TIGHT_PTS: z.breakout_side = BreakoutSide.DOWN
            elif self._outside >= CONFIRM_OUTSIDE:
                self._kill_zone(h, l, t)

    def _kill_zone(self, h, l, t):
        z = self._active
        z.state = ZoneState.BROKEN
        z.breakout_confirmed = True
        z.broken_at = t
        if z.breakout_side == BreakoutSide.NONE:
            if h > z.hh + TIGHT_PTS: z.breakout_side = BreakoutSide.UP
            elif l < z.ll - TIGHT_PTS: z.breakout_side = BreakoutSide.DOWN
        z.state = ZoneState.ARCHIVED
        self._history.append(z)
        if len(self._history) > 10:
            self._history.pop(0)
        self._active = None
        self._start_zone(h, l, t)


# ── DECISION ENGINE ───────────────────────────────────────────

PB_OFFSET = 10.0  # MID ± 10p — valoare initiala, de rafinat

def make_decision(
    aurora_bias: str,
    aurora_str:  float,
    price_now:   float,
    active_zone: Optional[BalanceZone],
    last_dead:   Optional[BalanceZone],
) -> dict:
    """
    Arborele de decizie conform spec.
    Returneaza dict cu strategy, reason, pb_arm.
    """
    if aurora_bias not in ("LONG", "SHORT") or aurora_str < 4.0:
        return {"strategy": "WAIT", "reason": "Aurora neclar sau STR<4",
                "pb_arm": None, "zone_id": None}

    z = active_zone

    # Nicio zona activa
    if z is None or z.state == ZoneState.SEARCHING:
        d = last_dead
        if d and d.breakout_confirmed:
            aligned = (aurora_bias == "LONG" and d.breakout_side == BreakoutSide.UP) or \
                      (aurora_bias == "SHORT" and d.breakout_side == BreakoutSide.DOWN)
            if aligned:
                pb = d.pb_arm(aurora_bias, PB_OFFSET)
                return {"strategy": "PULLBACK",
                        "reason": f"BZ#{d.id} moarta, brk={d.breakout_side.value}, aliniat",
                        "pb_arm": pb, "zone_id": d.id}
            else:
                return {"strategy": "NO_TRADE",
                        "reason": f"BZ#{d.id} moarta, brk contra Aurora",
                        "pb_arm": None, "zone_id": d.id}
        return {"strategy": "WAIT", "reason": "Nicio structura", "pb_arm": None, "zone_id": None}

    # Zona in constructie
    if z.state == ZoneState.BUILDING:
        return {"strategy": "WAIT", "reason": f"BZ#{z.id} BUILDING ({z.touches} atingeri)",
                "pb_arm": None, "zone_id": z.id}

    # Zona deja tranzactionata — nu mai produce semnale
    if z.state == ZoneState.TRADED:
        return {"strategy": "WAIT", "reason": f"BZ#{z.id} TRADED — asteapta zona noua",
                "pb_arm": None, "zone_id": z.id}

    # Zona confirmata
    if z.state == ZoneState.CONFIRMED:
        tol = z.rang * 0.15
        at_hh = price_now >= z.hh - tol
        at_ll = price_now <= z.ll + tol
        if at_hh and aurora_bias == "LONG":
            pb = z.pb_arm(aurora_bias, PB_OFFSET)
            return {"strategy": "CONTRA",
                    "reason": f"BZ#{z.id} CONFIRMED, pret@HH, Aurora LONG → CONTRA SHORT",
                    "pb_arm": pb, "zone_id": z.id}
        if at_ll and aurora_bias == "SHORT":
            pb = z.pb_arm(aurora_bias, PB_OFFSET)
            return {"strategy": "CONTRA",
                    "reason": f"BZ#{z.id} CONFIRMED, pret@LL, Aurora SHORT → CONTRA LONG",
                    "pb_arm": pb, "zone_id": z.id}
        return {"strategy": "WAIT",
                "reason": f"BZ#{z.id} CONFIRMED, pret in mijloc",
                "pb_arm": None, "zone_id": z.id}

    # Breakout pending
    if z.state == ZoneState.BREAKOUT_PENDING:
        return {"strategy": "WAIT",
                "reason": f"BZ#{z.id} BREAKOUT_PENDING, astept confirmare",
                "pb_arm": None, "zone_id": z.id}

    # Zona sparta
    if z.state in (ZoneState.BROKEN, ZoneState.ARCHIVED):
        aligned = (aurora_bias == "LONG" and z.breakout_side == BreakoutSide.UP) or \
                  (aurora_bias == "SHORT" and z.breakout_side == BreakoutSide.DOWN)
        if aligned:
            pb = z.pb_arm(aurora_bias, PB_OFFSET)
            return {"strategy": "PULLBACK",
                    "reason": f"BZ#{z.id} BROKEN brk={z.breakout_side.value} aliniat",
                    "pb_arm": pb, "zone_id": z.id}
        return {"strategy": "NO_TRADE",
                "reason": f"BZ#{z.id} BROKEN brk contra Aurora",
                "pb_arm": None, "zone_id": z.id}

    return {"strategy": "WAIT", "reason": "Fallback", "pb_arm": None, "zone_id": None}


# ── AURORA V2 — INTERFATA PRINCIPALA ─────────────────────────

class AuroraV2:
    """
    Interfata principala pentru botul live.
    Se instantiaza o data la pornire.
    Se apeleaza update() la fiecare loop cu datele disponibile.
    """
    def __init__(self):
        self.mse = MarketStructureEngine()
        self._last_log = ""

    def update(
        self,
        high:        float,
        low:         float,
        price_now:   float,
        aurora_bias: str,
        aurora_str:  float,
        time_str:    str,
    ) -> dict:
        """
        Apelat la fiecare loop cu ultima lumânare M5 inchisa.

        Returneaza dict cu:
          zone_state, zone_id, hh, ll, mid, pb_arm,
          strategy, reason
        """
        # Actualizam MSE
        self.mse.update(high, low, time_str)
        active = self.mse._active
        last_dead = self.mse.last_dead_zone()

        # Decizie
        dec = make_decision(aurora_bias, aurora_str, price_now,
                            active, last_dead)

        # Output complet
        result = {
            "zone_state": active.state.value if active else "NONE",
            "zone_id":    active.id if active else None,
            "zone_hh":    active.hh if active else None,
            "zone_ll":    active.ll if active else None,
            "zone_mid":   active.mid if active else None,
            "zone_rang":  active.rang if active else None,
            "zone_touch": active.touches if active else 0,
            "pb_arm":     dec["pb_arm"],
            "strategy":   dec["strategy"],
            "reason":     dec["reason"],
        }

        # Log — afiseaza doar cand se schimba ceva
        log_line = f"{result['zone_state']}|{dec['strategy']}|{dec['pb_arm']}"
        if log_line != self._last_log:
            self._last_log = log_line
            self._print_log(result, aurora_bias, aurora_str, price_now)

        return result

    def _print_log(self, r, bias, strength, price):
        zone_str = "NONE"
        if r["zone_id"]:
            zone_str = (f"#{r['zone_id']} {r['zone_state']} "
                        f"HH={r['zone_hh']} MID={r['zone_mid']} "
                        f"LL={r['zone_ll']} N={r['zone_touch']}")

        pb_str = f"PB_ARM={r['pb_arm']}" if r['pb_arm'] else "PB_ARM=—"

        print(f"\n  [v2] Aurora={bias} STR={strength:.1f} P={price:.1f}")
        print(f"  [v2] Zone: {zone_str}")
        print(f"  [v2] {r['strategy']:>8} | {pb_str} | {r['reason']}")
