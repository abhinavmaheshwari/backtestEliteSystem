# =====================================================================================
# app/sl_target_helper.py  (v5 — ANTI-TRAP EDITION)
#
# KEY INSIGHT: Different scanners trade completely different setups.
# One SL/Target formula for all is wrong. This module dispatches to
# a mode-specific sub-function for each scanner type.
#
# MODES:
#   "EOD"      → Daily momentum breakout (swing trade, hold days–weeks)
#   "INTRADAY" → 15m early-momentum scalp (hold position until SL or Target is hit)
#   "LIVE_1H"  → Hourly swing continuation (hold 1–5 days)
#   "REVERSAL" → Counter-trend oversold bounce (mean reversion, hold days–weeks)
#
# v5 UPGRADES:
#   1. MULTI-SWING CLUSTERING — scans last 3 swing lows; if 2+ cluster within 1%,
#      uses the cluster zone for SL placement (much stronger support)
#   2. VWAP-ANCHORED SL — for intraday/1H, uses VWAP as SL anchor when between
#      candle_low and entry (VWAP = institutional fair-value support)
#   3. ADX-AWARE BUFFER WIDENING — trending stocks (ADX>35) get 30% wider buffers
#      to survive deeper pullbacks without stopping out
#   4. ATR-SCALED TARGET CAPS — prevents unrealistic targets that stock can't reach
#      15m: 5×ATR | 1H: 8×ATR | EOD: 12×ATR
#   5. MEASURED MOVE TARGETS — when BASE_WIDTH available, T1 = entry + base height
#
# ANTI-OPERATOR-TRAP DESIGN:
#   Operators/algos know retail places SL exactly at swing low.
#   They run stops with a wick, then reverse. Our fix:
#   → SL is placed BELOW the zone, not at it, with a meaningful % buffer
#   → Buffer is max(mode_atr_fraction × ATR, mode_pct × price)
#   → ADX-scaled: trending stocks get wider buffers (deeper pullbacks)
#   → This makes the stop hunt unprofitable for operators (too far to sweep)
#
# SL BUFFER TABLE (per mode):
#   INTRADAY  → max(0.5×ATR, 0.30% price) — tight momentum scalp trade
#   LIVE_1H   → max(0.5×ATR, 0.50% price) — moderate, hourly swing
#   EOD       → max(0.75×ATR, 0.75% price) — meaningful, daily trade
#   REVERSAL  → max(1.0×ATR, 1.00% price) — widest, volatile beaten stocks
#
# MINIMUM R:R TABLE (per mode):
#   INTRADAY  → 1.5:1 (scalp — quicker in/out)
#   LIVE_1H   → 2.0:1 (hourly swing — higher bar)
#   EOD       → 2.0:1 (daily trade — overnight risk demands it)
#   REVERSAL  → 2.0:1 (counter-trend — higher base risk)
#
# TARGET PHILOSOPHY (per mode):
#   EOD       → Nearest swing high / R1 pivot → R2 → 52W high zone
#   INTRADAY  → Session high / BB_UPPER → Day's R1 (no T3 — hold until SL/Target)
#   LIVE_1H   → R1 / BB_UPPER → R2 (no T3 — 1H has limited range)
#   REVERSAL  → EMA20 or BB_MID (mean reversion T1) → SMA50 (T2) → R1 (T3)
# =====================================================================================

from __future__ import annotations
from typing import Optional
import math

from config import MAX_TARGET_ATR


# ── Per-mode configuration ────────────────────────────────────────────────────
_MODE_CONFIG = {
    #           atr_base  sl_atr_buf  sl_pct_buf  min_rr  max_sl_atr
    "EOD":      (2.00,    0.75,       0.0075,     2.0,    3.0),
    "INTRADAY": (1.00,    0.50,       0.0030,     1.5,    2.5),
    "LIVE_1H":  (1.50,    0.50,       0.0050,     2.0,    2.5),
    "REVERSAL": (2.00,    1.00,       0.0100,     2.0,    3.5),
}
_DEFAULT_CONFIG = (1.50, 0.50, 0.0050, 1.5, 3.0)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe(val) -> Optional[float]:
    """Return float if valid, finite, and > 0, else None."""
    try:
        f = float(val)
        return f if math.isfinite(f) and f > 0 else None
    except (TypeError, ValueError):
        return None


def _pick_support(
    entry: float,
    swing_low: Optional[float],
    s1: Optional[float],
    swing_low_raw: Optional[float],
    s2: Optional[float],
) -> tuple[Optional[float], str]:
    """
    Best structural support level below entry.
    Priority: true pivot swing low > S1 > rolling window low > S2.
    """
    for level, label in [
        (swing_low,     "pivot swing low"),
        (s1,            "pivot S1"),
        (swing_low_raw, "rolling swing low"),
        (s2,            "pivot S2"),
    ]:
        v = _safe(level)
        if v is not None and v < entry:
            return v, label
    return None, "none"


def _pick_resistance(
    entry: float,
    swing_high: Optional[float],
    r1: Optional[float],
    bb_upper: Optional[float],
    swing_high_raw: Optional[float],
    r2: Optional[float],
) -> tuple[Optional[float], str]:
    """
    Nearest structural resistance above entry.
    Priority: true pivot swing high > R1 > BB_UPPER > rolling high > R2.
    """
    for level, label in [
        (swing_high,     "pivot swing high"),
        (r1,             "pivot R1"),
        (bb_upper,       "BB upper band"),
        (swing_high_raw, "rolling swing high"),
        (r2,             "pivot R2"),
    ]:
        v = _safe(level)
        if v is not None and v > entry:
            return v, label
    return None, "none"


def _adx_atr_scale(adx: Optional[float], atr_pct: Optional[float], base: float) -> float:
    """
    Scale ATR multiplier by trend strength (ADX) and volatility regime (ATR%).
    Stronger trend → slightly wider SL to survive pullbacks without stopping out.
    High volatility → wider SL (stock moves more per day).

    v5 UPGRADE: ADX > 35 now gets 30% wider buffer (up from 20% at >40).
    Rationale: trending stocks with ADX 35-40 get the deepest operator stop-hunts
    because the trend draws in the most retail SL clusters.
    """
    m = base
    if adx is not None:
        if adx > 40:   m *= 1.30   # v5: widened from 1.20 → 1.30 (deep pullback protection)
        elif adx > 35: m *= 1.20   # v5: NEW tier — strong trend, frequent stop-hunts
        elif adx > 30: m *= 1.10
        elif adx < 20: m *= 0.85   # choppy — tighter
    if atr_pct is not None:
        if atr_pct > 4.0:   m *= 1.20
        elif atr_pct > 2.5: m *= 1.10
        elif atr_pct < 1.0: m *= 0.90
    return round(m, 3)


def _cluster_support(
    swing_lows: list[Optional[float]],
    entry: float,
    cluster_pct: float = 1.0,
) -> tuple[Optional[float], str]:
    """
    v5 NEW: Multi-swing clustering for SL placement.

    Scans multiple swing lows — if 2+ cluster within cluster_pct% of each other,
    the cluster zone is a much stronger support than any single point.

    Returns the lowest point of the cluster (for buffer placement below it)
    and a label describing the cluster.
    """
    # Filter valid levels below entry
    valid = sorted(
        [float(v) for v in swing_lows if v is not None and _safe(v) is not None and float(v) < entry],
        reverse=True,  # nearest to entry first
    )

    if len(valid) < 2:
        return None, "none"

    # Check if any 2 consecutive levels cluster within cluster_pct%
    for i in range(len(valid) - 1):
        level_a = valid[i]
        level_b = valid[i + 1]
        if level_a > 0:
            gap_pct = abs(level_a - level_b) / level_a * 100
            if gap_pct <= cluster_pct:
                cluster_bottom = min(level_a, level_b)
                return cluster_bottom, f"swing cluster (₹{level_b:.2f}–₹{level_a:.2f}, gap {gap_pct:.1f}%)"

    return None, "none"


def _cap_target(
    target: float,
    entry: float,
    eff_atr: float,
    timeframe: str,
) -> float:
    """
    v5 NEW: Cap target at MAX_TARGET_ATR × ATR from entry.
    Prevents unrealistic targets that the stock has no chance of reaching.
    """
    max_atr_mult = MAX_TARGET_ATR.get(timeframe, 12.0)
    max_target   = entry + max_atr_mult * eff_atr
    return min(target, max_target)


def _sl_from_support(
    entry: float,
    support: float,
    eff_atr: float,
    sl_atr_buf: float,
    sl_pct_buf: float,
    max_sl_atr: float,
    support_label: str,
) -> tuple[float, str]:
    """
    Places SL below a structural support with an anti-trap buffer.
    Buffer = max(sl_atr_buf × ATR, sl_pct_buf × price)
    Caps at max_sl_atr × ATR to avoid absurdly wide stops.
    """
    buf      = max(sl_atr_buf * eff_atr, sl_pct_buf * entry)
    raw_sl   = support - buf
    sl_method = (
        f"Below {support_label} ₹{round(support, 2)} "
        f"— buffer ₹{round(buf, 2)} (anti-trap zone)"
    )
    # Cap if structure is very far from entry
    if (entry - raw_sl) > max_sl_atr * eff_atr:
        raw_sl    = entry - max_sl_atr * eff_atr
        sl_method = (
            f"Capped at {max_sl_atr}×ATR "
            f"({support_label} ₹{round(support, 2)} too far from entry)"
        )
    return raw_sl, sl_method


def _rsi_zone(rsi: Optional[float]) -> str:
    v = _safe(rsi)
    if v is None:       return "neutral"
    if v > 72:          return "overbought"
    if v > 55:          return "bullish"
    if v > 40:          return "neutral"
    return "oversold"


# ─────────────────────────────────────────────────────────────────────────────
# EOD — Daily Breakout (swing trade, hold days to weeks)
# ─────────────────────────────────────────────────────────────────────────────

def _compute_eod(
    entry: float, eff_atr: float, adx, rsi, macd_hist, atr_pct,
    swing_low, swing_high, bb_upper, bb_lower,
    s1, s2, r1, r2, swing_low_raw, swing_high_raw,
) -> dict:
    """
    EOD breakout logic:
    • SL   — below swing/pivot support with 0.75×ATR or 0.75% price buffer
    • T1   — nearest swing high / R1 pivot (min 2:1 RR)
    • T2   — R2 / 52W zone (only if not overbought)
    • T3   — 5×RR on strong MACD+ADX confluence (hold for a run)
    • Trailing SL note: raise SL to breakeven after T1 hit
    """
    atr_base, sl_atr_buf, sl_pct_buf, min_rr, max_sl_atr = _MODE_CONFIG["EOD"]
    scaled_mult = _adx_atr_scale(_safe(adx), _safe(atr_pct), atr_base)

    support, sup_label = _pick_support(entry, _safe(swing_low), _safe(s1), _safe(swing_low_raw), _safe(s2))

    if support is not None:
        raw_sl, sl_method = _sl_from_support(entry, support, eff_atr, sl_atr_buf, sl_pct_buf, max_sl_atr, sup_label)
    else:
        raw_sl    = entry - scaled_mult * eff_atr
        sl_method = f"ATR fallback ({scaled_mult}×ATR) — no support structure below entry"

    stop_loss = round(raw_sl, 2)
    risk      = max(entry - stop_loss, entry * 0.005)

    zone = _rsi_zone(rsi)

    resistance, res_label = _pick_resistance(entry, _safe(swing_high), _safe(r1), _safe(bb_upper), _safe(swing_high_raw), _safe(r2))

    if resistance is not None:
        t1_raw   = resistance
        t_method = f"T1: {res_label} ₹{round(t1_raw, 2)}"
    else:
        t1_raw   = entry + min_rr * risk
        t_method = f"T1: no resistance — min {min_rr}×RR"

    # EOD needs minimum 2:1 — justify overnight risk
    if (t1_raw - entry) < min_rr * risk:
        t1_raw   = entry + min_rr * risk
        t_method = f"T1: bumped to {min_rr}×RR ({res_label} too close)"

    # v5: Cap targets at MAX_TARGET_ATR to prevent unrealistic targets
    target_1 = round(_cap_target(t1_raw, entry, eff_atr, "1d"), 2)
    rr_ratio = round((target_1 - entry) / risk, 2) if risk > 0 else 0.0

    # T2 — R2 or 3.5×RR (not for overbought)
    target_2 = None
    if zone != "overbought":
        r2_v = _safe(r2)
        if r2_v and r2_v > target_1:
            target_2  = round(_cap_target(r2_v, entry, eff_atr, "1d"), 2)
            t_method += f" | T2: R2 ₹{target_2}"
        else:
            target_2  = round(_cap_target(entry + 3.5 * risk, entry, eff_atr, "1d"), 2)
            t_method += f" | T2: 3.5×RR ₹{target_2}"

    # T3 — strong confluence only (MACD bullish + ADX > 25)
    target_3 = None
    adx_v    = _safe(adx)
    macd_bull = macd_hist is not None and _safe(abs(float(macd_hist))) is not None and float(macd_hist) > 0
    above_t2  = target_2 if target_2 else target_1
    if macd_bull and zone in ("neutral", "bullish", "oversold") and (adx_v is None or adx_v > 25):
        t3_cand = round(_cap_target(entry + 5.0 * risk, entry, eff_atr, "1d"), 2)
        if t3_cand > above_t2:
            target_3  = t3_cand
            t_method += f" | T3: 5×RR ₹{target_3} (MACD+ADX)"

    return {
        "stop_loss":    stop_loss,
        "target_1":     target_1,
        "target_2":     target_2,
        "target_3":     target_3,
        "rr_ratio":     rr_ratio,
        "risk":         round(risk, 2),
        "sl_method":    sl_method,
        "t_method":     t_method,
        "rsi_zone":     zone,
        "trail_note":   "Raise SL to breakeven after T1 is hit",
    }


# ─────────────────────────────────────────────────────────────────────────────
# INTRADAY — 15m Early Momentum Scalp (same-day trade)
# ─────────────────────────────────────────────────────────────────────────────

def _compute_intraday(
    entry: float, eff_atr: float, adx, rsi, macd_hist, atr_pct,
    swing_low, swing_high, bb_upper, bb_lower,
    s1, s2, r1, r2, swing_low_raw, swing_high_raw,
    candle_low: Optional[float] = None,
    vwap: Optional[float] = None,
) -> dict:
    """
    Intraday 15m scalp logic (v5 upgrade):
    • SL   — VWAP-anchored (if VWAP between candle_low and entry)
             OR below triggering candle's OWN LOW + 0.5×ATR buffer
             Fallback to 15m swing low if candle_low is impractical
    • T1   — session high / R1 / BB_UPPER (min 1.5:1 RR)
    • T2   — day's R1 or 2.5×RR (capped at 5×ATR)
    • T3   — NONE (do not hold for extended target)
    • Trail note: "Hold position until SL or Target is hit"
    """
    atr_base, sl_atr_buf, sl_pct_buf, min_rr, max_sl_atr = _MODE_CONFIG["INTRADAY"]

    # v5: ADX-aware buffer widening for trending stocks
    adx_v = _safe(adx)
    if adx_v is not None and adx_v > 35:
        sl_atr_buf *= 1.20   # 20% wider buffer for strong intraday trends

    buf = max(sl_atr_buf * eff_atr, sl_pct_buf * entry)

    # v5 UPGRADE: VWAP-ANCHORED SL
    # VWAP is the institutional fair-value line. During genuine breakouts,
    # price rarely stays below VWAP. If VWAP is between candle_low and entry,
    # it's a better SL anchor than candle_low alone.
    vwap_v = _safe(vwap)
    if vwap_v is not None and candle_low is not None and _safe(candle_low):
        candle_low_f = float(candle_low)
        if candle_low_f < vwap_v < entry:
            # VWAP is between candle low and entry — use VWAP as anchor
            raw_sl    = vwap_v - buf
            sl_method = (
                f"Below VWAP ₹{round(vwap_v, 2)} — buffer ₹{round(buf, 2)} "
                f"(institutional support anchor)"
            )
        elif candle_low_f < entry:
            raw_sl    = candle_low_f - buf
            sl_method = f"Below candle low ₹{round(candle_low_f, 2)} — buffer ₹{round(buf, 2)} (anti-trap)"
        else:
            support, sup_label = _pick_support(entry, _safe(swing_low), _safe(s1), _safe(swing_low_raw), _safe(s2))
            if support is not None:
                raw_sl, sl_method = _sl_from_support(entry, support, eff_atr, sl_atr_buf, sl_pct_buf, max_sl_atr, sup_label)
            else:
                raw_sl    = entry - 1.0 * eff_atr
                sl_method = f"1×ATR fallback — no 15m structure below entry"
    elif candle_low is not None and _safe(candle_low) and candle_low < entry:
        raw_sl    = candle_low - buf
        sl_method = f"Below candle low ₹{round(candle_low, 2)} — buffer ₹{round(buf, 2)} (anti-trap)"
    else:
        # Fallback: 15m swing structure
        support, sup_label = _pick_support(entry, _safe(swing_low), _safe(s1), _safe(swing_low_raw), _safe(s2))
        if support is not None:
            raw_sl, sl_method = _sl_from_support(entry, support, eff_atr, sl_atr_buf, sl_pct_buf, max_sl_atr, sup_label)
        else:
            raw_sl    = entry - 1.0 * eff_atr
            sl_method = f"1×ATR fallback — no 15m structure below entry"

    # Hard cap: intraday SL max 2.5×ATR
    if (entry - raw_sl) > max_sl_atr * eff_atr:
        raw_sl    = entry - max_sl_atr * eff_atr
        sl_method = f"Capped at {max_sl_atr}×ATR (intraday risk limit)"

    stop_loss = round(raw_sl, 2)
    risk      = max(entry - stop_loss, entry * 0.003)

    zone = _rsi_zone(rsi)

    # Target: session-level resistance
    resistance, res_label = _pick_resistance(entry, _safe(swing_high), _safe(r1), _safe(bb_upper), _safe(swing_high_raw), _safe(r2))

    if resistance is not None:
        t1_raw   = resistance
        t_method = f"T1: {res_label} ₹{round(t1_raw, 2)} (intraday)"
    else:
        t1_raw   = entry + min_rr * risk
        t_method = f"T1: min {min_rr}×RR (no intraday resistance found)"

    if (t1_raw - entry) < min_rr * risk:
        t1_raw   = entry + min_rr * risk
        t_method = f"T1: bumped to {min_rr}×RR ({res_label} too close)"

    # v5: Cap targets at MAX_TARGET_ATR
    t1_raw   = _cap_target(t1_raw, entry, eff_atr, "15m")
    target_1 = round(t1_raw, 2)
    rr_ratio  = round((target_1 - entry) / risk, 2) if risk > 0 else 0.0

    # T2 — only if not overbought (2.5×RR or R2)
    target_2 = None
    if zone != "overbought":
        r2_v = _safe(r2)
        if r2_v and r2_v > target_1:
            target_2  = round(_cap_target(r2_v, entry, eff_atr, "15m"), 2)
            t_method += f" | T2: R2 ₹{target_2}"
        else:
            target_2  = round(_cap_target(entry + 2.5 * risk, entry, eff_atr, "15m"), 2)
            t_method += f" | T2: 2.5×RR ₹{target_2}"

    # NO T3 on intraday
    return {
        "stop_loss":    stop_loss,
        "target_1":     target_1,
        "target_2":     target_2,
        "target_3":     None,
        "rr_ratio":     rr_ratio,
        "risk":         round(risk, 2),
        "sl_method":    sl_method,
        "t_method":     t_method,
        "rsi_zone":     zone,
        "trail_note":   "Hold position until SL or Target is hit",
    }


# ─────────────────────────────────────────────────────────────────────────────
# LIVE_1H — Hourly Swing Continuation (hold 1–5 days)
# ─────────────────────────────────────────────────────────────────────────────

def _compute_live_1h(
    entry: float, eff_atr: float, adx, rsi, macd_hist, atr_pct,
    swing_low, swing_high, bb_upper, bb_lower,
    s1, s2, r1, r2, swing_low_raw, swing_high_raw,
    candle_low: Optional[float] = None,
    vwap: Optional[float] = None,
) -> dict:
    """
    1H swing logic (v5 upgrade):
    • SL   — VWAP-anchored (if VWAP between candle_low and entry)
             OR below last hourly swing low + 0.5×ATR or 0.5% price buffer
    • T1   — R1 / swing high (min 2:1 RR for 1H swing, overnight risk)
    • T2   — R2 / BB_UPPER (not for overbought)
    • T3   — NONE (1H setups don't have multi-week thesis)
    • Trail note: "Trail SL to last hourly swing low after T1"
    """
    atr_base, sl_atr_buf, sl_pct_buf, min_rr, max_sl_atr = _MODE_CONFIG["LIVE_1H"]
    scaled_mult = _adx_atr_scale(_safe(adx), _safe(atr_pct), atr_base)

    # v5: ADX-aware buffer widening for trending stocks
    adx_v = _safe(adx)
    if adx_v is not None and adx_v > 35:
        sl_atr_buf *= 1.20

    buf = max(sl_atr_buf * eff_atr, sl_pct_buf * entry)

    # v5: VWAP-ANCHORED SL (same logic as intraday)
    vwap_v = _safe(vwap)
    if vwap_v is not None and candle_low is not None and _safe(candle_low):
        candle_low_f = float(candle_low)
        if candle_low_f < vwap_v < entry:
            raw_sl    = vwap_v - buf
            sl_method = (
                f"Below VWAP ₹{round(vwap_v, 2)} — buffer ₹{round(buf, 2)} "
                f"(institutional support anchor)"
            )
        elif candle_low_f < entry:
            raw_sl    = candle_low_f - buf
            sl_method = f"Below candle low ₹{round(candle_low_f, 2)} — buffer ₹{round(buf, 2)} (anti-trap)"
        else:
            support, sup_label = _pick_support(entry, _safe(swing_low), _safe(s1), _safe(swing_low_raw), _safe(s2))
            if support is not None:
                raw_sl, sl_method = _sl_from_support(entry, support, eff_atr, sl_atr_buf, sl_pct_buf, max_sl_atr, sup_label)
            else:
                raw_sl    = entry - scaled_mult * eff_atr
                sl_method = f"ATR fallback ({scaled_mult}×ATR) — no 1H structure below entry"
    else:
        support, sup_label = _pick_support(entry, _safe(swing_low), _safe(s1), _safe(swing_low_raw), _safe(s2))
        if support is not None:
            raw_sl, sl_method = _sl_from_support(entry, support, eff_atr, sl_atr_buf, sl_pct_buf, max_sl_atr, sup_label)
        else:
            raw_sl    = entry - scaled_mult * eff_atr
            sl_method = f"ATR fallback ({scaled_mult}×ATR) — no 1H structure below entry"

    stop_loss = round(raw_sl, 2)
    risk      = max(entry - stop_loss, entry * 0.005)

    zone = _rsi_zone(rsi)

    resistance, res_label = _pick_resistance(entry, _safe(swing_high), _safe(r1), _safe(bb_upper), _safe(swing_high_raw), _safe(r2))

    if resistance is not None:
        t1_raw   = resistance
        t_method = f"T1: {res_label} ₹{round(t1_raw, 2)}"
    else:
        t1_raw   = entry + min_rr * risk
        t_method = f"T1: min {min_rr}×RR (no 1H resistance found)"

    # 1H swing: minimum 2:1 RR
    if (t1_raw - entry) < min_rr * risk:
        t1_raw   = entry + min_rr * risk
        t_method = f"T1: bumped to {min_rr}×RR ({res_label} too close)"

    # v5: Cap targets at MAX_TARGET_ATR
    target_1 = round(_cap_target(t1_raw, entry, eff_atr, "1h"), 2)
    rr_ratio  = round((target_1 - entry) / risk, 2) if risk > 0 else 0.0

    # T2 — R2 or 3×RR (not for overbought)
    target_2 = None
    if zone != "overbought":
        r2_v = _safe(r2)
        if r2_v and r2_v > target_1:
            target_2  = round(_cap_target(r2_v, entry, eff_atr, "1h"), 2)
            t_method += f" | T2: R2 ₹{target_2}"
        else:
            target_2  = round(_cap_target(entry + 3.0 * risk, entry, eff_atr, "1h"), 2)
            t_method += f" | T2: 3×RR ₹{target_2}"

    # No T3 for 1H swings
    return {
        "stop_loss":    stop_loss,
        "target_1":     target_1,
        "target_2":     target_2,
        "target_3":     None,
        "rr_ratio":     rr_ratio,
        "risk":         round(risk, 2),
        "sl_method":    sl_method,
        "t_method":     t_method,
        "rsi_zone":     zone,
        "trail_note":   "Trail SL to last hourly swing low after T1 is hit",
    }


# ─────────────────────────────────────────────────────────────────────────────
# REVERSAL — Oversold Bounce / Mean Reversion (counter-trend, long-only)
# ─────────────────────────────────────────────────────────────────────────────

def _compute_reversal(
    entry: float, eff_atr: float, adx, rsi, macd_hist, atr_pct,
    swing_low, swing_high, bb_upper, bb_lower,
    s1, s2, r1, r2, swing_low_raw, swing_high_raw,
    ema20: Optional[float] = None,
    bb_mid: Optional[float] = None,
    sma50: Optional[float] = None,
) -> dict:
    """
    REVERSAL / Mean-Reversion logic (LONG-ONLY oversold bounce):

    These are stocks down 18–60% from highs — volatility is HIGH.
    The trade thesis is: "RSI curled up, MACD turning, price crossed EMA20.
    Mean reversion to EMA20/SMA50 is underway."

    SL:
      → Below the recent oversold swing low with WIDE buffer (1.0×ATR or 1% price)
      → The swing low here IS the entry trigger — if it breaks again, reversal failed
      → Wide buffer critical: beaten-down stocks have huge daily ranges

    Targets (MEAN REVERSION — not resistance-based like breakout scanners):
      → T1: EMA20 or BB_MID (the stock is bouncing back to the mean)
           This is the primary mean reversion target — usually 8–20% above entry
      → T2: SMA50 (further reversion, price normalizing)
      → T3: R1 (only on strong volume + MACD momentum — full recovery)

    Why NOT swing high / R1 as T1?
      These stocks have heavy overhead resistance from the 18–60% drop.
      Going straight for resistance is unrealistic for a REVERSAL setup.
      The mean (EMA20, SMA50) is what price magnetically returns to first.
    """
    atr_base, sl_atr_buf, sl_pct_buf, min_rr, max_sl_atr = _MODE_CONFIG["REVERSAL"]

    # Volatility-scaled buffer (beaten stocks are volatile)
    support, sup_label = _pick_support(entry, _safe(swing_low), _safe(s1), _safe(swing_low_raw), _safe(s2))

    if support is not None:
        raw_sl, sl_method = _sl_from_support(entry, support, eff_atr, sl_atr_buf, sl_pct_buf, max_sl_atr, sup_label)
    else:
        # No pivot swing — use recent candle low (the reversal trigger bar's low)
        raw_sl    = entry - max(sl_atr_buf * eff_atr, sl_pct_buf * entry)
        sl_method = f"Below entry by buffer ₹{round(entry - raw_sl, 2)} (no prior swing low found)"

    stop_loss = round(raw_sl, 2)
    risk      = max(entry - stop_loss, entry * 0.008)  # min 0.8% risk for reversal

    # ── REVERSAL TARGETS: mean reversion levels, NOT overhead resistance ──────
    #
    # CRITICAL FIX: The reversal scanner entry condition requires close > EMA20.
    # Therefore EMA20 is ALWAYS below entry — it can never be T1.
    #
    # Correct cascade for reversal targets:
    #   T1: BB_MID (if above entry — Bollinger mean reversion)
    #       → SMA50 (next mean-reversion level above, if available)
    #       → R1 (nearest resistance if all means are below entry)
    #       → min 2:1 RR fallback
    #   T2: SMA50 (if not used as T1) or R1 or 3.5×RR
    #   T3: R2 on strong MACD + ADX momentum
    #
    t_method = ""

    bbmid_v = _safe(bb_mid)
    sma50_v = _safe(sma50)
    r1_v    = _safe(r1)
    r2_v    = _safe(r2)

    t1_raw = None

    # Priority 1: BB_MID above entry (Bollinger mean reversion)
    if bbmid_v and bbmid_v > entry:
        t1_raw   = bbmid_v
        t_method = f"T1: BB Mid ₹{round(t1_raw, 2)} (Bollinger mean reversion)"
    # Priority 2: SMA50 above entry (longer-term mean reversion)
    elif sma50_v and sma50_v > entry:
        t1_raw   = sma50_v
        t_method = f"T1: SMA50 ₹{round(t1_raw, 2)} (50-day mean reversion)"
    # Priority 3: R1 above entry (nearest resistance)
    elif r1_v and r1_v > entry:
        t1_raw   = r1_v
        t_method = f"T1: R1 ₹{round(t1_raw, 2)} (above all means — use resistance)"
    # Fallback: minimum 2:1 RR
    else:
        t1_raw   = entry + min_rr * risk
        t_method = f"T1: min {min_rr}×RR (all mean/resistance levels below entry)"

    # Enforce minimum 2:1 RR
    if (t1_raw - entry) < min_rr * risk:
        t1_raw   = entry + min_rr * risk
        t_method = f"T1: bumped to {min_rr}×RR (target too close to entry)"

    target_1 = round(_cap_target(t1_raw, entry, eff_atr, "1d"), 2)
    rr_ratio  = round((target_1 - entry) / risk, 2) if risk > 0 else 0.0

    # T2: next level above T1 (SMA50 if not already T1, else R1, else 3.5×RR)
    target_2  = None
    if sma50_v and sma50_v > target_1:
        target_2  = round(_cap_target(sma50_v, entry, eff_atr, "1d"), 2)
        t_method += f" | T2: SMA50 ₹{target_2} (further recovery)"
    elif r1_v and r1_v > target_1:
        target_2  = round(_cap_target(r1_v, entry, eff_atr, "1d"), 2)
        t_method += f" | T2: R1 ₹{target_2} (resistance)"
    else:
        t2_cand   = round(entry + 3.5 * risk, 2)
        if t2_cand > target_1:
            target_2  = t2_cand
            t_method += f" | T2: 3.5×RR ₹{target_2}"

    # T3: R2 only on strong momentum (MACD bull + ADX > 20)
    target_3  = None
    above_t2  = target_2 if target_2 else target_1
    macd_bull = macd_hist is not None and _safe(abs(float(macd_hist))) is not None and float(macd_hist) > 0
    adx_v     = _safe(adx)
    if macd_bull and r2_v and r2_v > above_t2 and (adx_v is None or adx_v > 20):
        target_3  = round(_cap_target(r2_v, entry, eff_atr, "1d"), 2)
        t_method += f" | T3: R2 ₹{target_3} (full recovery — MACD momentum)"
    elif macd_bull and (adx_v is None or adx_v > 20):
        t3_cand = round(_cap_target(entry + 5.0 * risk, entry, eff_atr, "1d"), 2)
        if t3_cand > above_t2:
            target_3  = t3_cand
            t_method += f" | T3: 5×RR ₹{target_3} (MACD momentum)"

    zone = _rsi_zone(rsi)

    return {
        "stop_loss":    stop_loss,
        "target_1":     target_1,
        "target_2":     target_2,
        "target_3":     target_3,
        "rr_ratio":     rr_ratio,
        "risk":         round(risk, 2),
        "sl_method":    sl_method,
        "t_method":     t_method,
        "rsi_zone":     zone,
        "trail_note":   "Book 50% at T1 (EMA20). Trail remainder to breakeven.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public API — single entry point
# ─────────────────────────────────────────────────────────────────────────────

def compute_sl_and_target(
    entry_price:    float,
    atr:            Optional[float],
    candle_range:   float,
    mode:           Optional[str]   = None,     # "EOD" | "INTRADAY" | "LIVE_1H" | "REVERSAL"
    # ── Technical context ──────────────────────────────────────────
    adx:            Optional[float] = None,
    rsi:            Optional[float] = None,
    macd_hist:      Optional[float] = None,
    atr_pct:        Optional[float] = None,
    swing_low:      Optional[float] = None,   # true pivot swing low
    swing_high:     Optional[float] = None,   # true pivot swing high
    bb_upper:       Optional[float] = None,
    bb_lower:       Optional[float] = None,
    bb_mid:         Optional[float] = None,   # used by REVERSAL (mean reversion T1)
    s1:             Optional[float] = None,
    s2:             Optional[float] = None,
    r1:             Optional[float] = None,
    r2:             Optional[float] = None,
    swing_low_raw:  Optional[float] = None,   # rolling window fallback
    swing_high_raw: Optional[float] = None,   # rolling window fallback
    candle_low:     Optional[float] = None,   # used by INTRADAY (bar's own low)
    ema20:          Optional[float] = None,   # used by REVERSAL (mean reversion T1)
    sma50:          Optional[float] = None,   # used by REVERSAL (mean reversion T2)
    vwap:           Optional[float] = None,   # v5: used by INTRADAY (VWAP-anchored SL)
    # Backward-compat alias (old callers used timeframe=)
    timeframe:      Optional[str]   = None,
) -> dict:
    """
    Mode-dispatching SL/Target engine.

    Returns dict with:
        stop_loss   — placement respects structure + anti-trap buffer
        target_1    — primary target (mean reversion for REVERSAL, resistance for others)
        target_2    — secondary target (None for REVERSAL if overbought)
        target_3    — extended target (EOD + REVERSAL only, on strong confluence)
        rr_ratio    — R:R of target_1
        risk        — ₹ risk per share
        sl_method   — explanation of how SL was placed
        t_method    — explanation of how targets were set
        rsi_zone    — overbought / bullish / neutral / oversold
        trail_note  — plain-English trailing instruction for the Telegram alert

    Backward compatibility: if `mode` is not recognized, falls back to `timeframe`.
    """
    # Resolve effective mode — support both mode= (new) and timeframe= (old alias)
    # Priority: mode > timeframe > "EOD" default
    _TIMEFRAME_MAP = {
        "EOD": "EOD", "1d": "EOD",
        "INTRADAY": "INTRADAY", "15m": "INTRADAY",
        "1H": "LIVE_1H", "1h": "LIVE_1H", "LIVE_1H": "LIVE_1H",
        "REVERSAL": "REVERSAL",
    }
    effective_mode = (
        _TIMEFRAME_MAP.get(mode or "", "")
        or _TIMEFRAME_MAP.get(timeframe or "", "")
        or "EOD"
    )

    # Resolve effective ATR
    eff_atr = _safe(atr) or (_safe(candle_range) * 1.5 if _safe(candle_range) else None)
    if eff_atr is None or eff_atr <= 0:
        eff_atr = entry_price * 0.015   # last resort: 1.5% of price

    kwargs = dict(
        entry=entry_price, eff_atr=eff_atr,
        adx=adx, rsi=rsi, macd_hist=macd_hist, atr_pct=atr_pct,
        swing_low=swing_low, swing_high=swing_high,
        bb_upper=bb_upper, bb_lower=bb_lower,
        s1=s1, s2=s2, r1=r1, r2=r2,
        swing_low_raw=swing_low_raw, swing_high_raw=swing_high_raw,
    )

    if effective_mode == "EOD":
        return _compute_eod(**kwargs)
    elif effective_mode == "INTRADAY":
        return _compute_intraday(**kwargs, candle_low=candle_low, vwap=vwap)
    elif effective_mode == "LIVE_1H":
        return _compute_live_1h(**kwargs, candle_low=candle_low, vwap=vwap)
    elif effective_mode == "REVERSAL":
        return _compute_reversal(**kwargs, ema20=ema20, bb_mid=bb_mid, sma50=sma50)
    else:
        return _compute_eod(**kwargs)  # safe default
