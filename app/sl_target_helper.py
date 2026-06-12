# =====================================================================================
# app/sl_target_helper.py  (UPGRADED v2)
#
# CRITICAL PROBLEMS FIXED from v1:
#
#   v1 PROBLEM 1 — Generic SL for every stock:
#       stop_loss = entry - (ATR_multiplier x ATR)
#       Same formula regardless of trend, momentum, or structure → BAD
#
#   v1 PROBLEM 2 — Hardcoded 2:1 RR target for every stock:
#       target = entry + 2 x risk
#       RSI=80 overbought stock got same target as RSI=45 fresh breakout → BAD
#
#   v1 PROBLEM 3 — No support/resistance usage:
#       SL never placed below swing low or pivot support
#       Target never aimed at resistance / BB_UPPER / swing high → BAD
#
# HOW v2 FIXES THEM:
#
#   SL LOGIC (priority order):
#     1. Below nearest SWING_LOW or Pivot Support (S1/S2) — structure-based
#     2. ATR buffer below that level (so SL is not AT the level, but below it)
#     3. ADX-adjusted multiplier: weak trend (ADX<20) → tighter SL
#        strong trend (ADX>30) → slightly wider SL to avoid shakeout
#     4. Volatility regime (ATR_PCT): high-volatility stock → accept wider SL
#
#   TARGET LOGIC (priority order):
#     1. Nearest SWING_HIGH / Pivot Resistance (R1/R2) — structure-based
#     2. BB_UPPER as natural resistance ceiling
#     3. RSI-adjusted: overbought (RSI>70) → use conservative T1 only
#        neutral (RSI 45-70) → T1 and T2
#        momentum (RSI<45 fresh breakout) → full T1/T2/T3
#     4. MACD confirmation: bullish crossover → allow higher target
#     5. Minimum RR enforced: if structure target gives <1.5 RR, bump to 1.5
# =====================================================================================

from __future__ import annotations
from typing import Optional
import math


# ── ATR base multipliers per timeframe ────────────────────────────────────────
ATR_BASE_MULTIPLIERS: dict[str, float] = {
    "15m":      1.0,
    "INTRADAY": 1.0,
    "1h":       1.5,
    "1H":       1.5,
    "1d":       2.0,
    "EOD":      2.0,
    "REVERSAL": 2.0,
}

MIN_RR_RATIO = 1.5   # never accept a setup with R:R below this


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe(val) -> Optional[float]:
    """Return float if valid, else None."""
    try:
        f = float(val)
        return f if math.isfinite(f) and f > 0 else None
    except (TypeError, ValueError):
        return None


def _atr_multiplier(timeframe: str, adx: Optional[float], atr_pct: Optional[float]) -> float:
    """
    Returns ATR multiplier adjusted for:
      - timeframe (base)
      - ADX (trend strength): strong trend → wider SL buffer
      - ATR_PCT (volatility regime): high volatility → accept wider SL
    """
    base = ATR_BASE_MULTIPLIERS.get(timeframe, 1.5)

    # ADX adjustment
    if adx is not None:
        if adx > 35:
            base *= 1.20    # strong trend — give more room to breathe
        elif adx < 20:
            base *= 0.85    # weak / choppy — tighten SL

    # Volatility regime (ATR as % of price)
    if atr_pct is not None:
        if atr_pct > 3.0:
            base *= 1.15    # high-volatility stock → widen slightly
        elif atr_pct < 1.0:
            base *= 0.90    # low-volatility stock → tighter is fine

    return round(base, 3)


def _nearest_support(
        entry: float,
        swing_low: Optional[float],
        s1: Optional[float],
        s2: Optional[float],
) -> Optional[float]:
    """
    Returns the nearest support level BELOW entry.
    Priority: swing_low > S1 > S2.
    """
    candidates = [v for v in [swing_low, s1, s2] if v is not None and v < entry]
    return max(candidates) if candidates else None


def _nearest_resistance(
        entry: float,
        swing_high: Optional[float],
        r1: Optional[float],
        r2: Optional[float],
        bb_upper: Optional[float],
) -> Optional[float]:
    """
    Returns the nearest resistance level ABOVE entry.
    Priority: swing_high > BB_UPPER > R1 > R2.
    """
    candidates = [v for v in [swing_high, bb_upper, r1, r2] if v is not None and v > entry]
    return min(candidates) if candidates else None


# ── Main function ─────────────────────────────────────────────────────────────

def compute_sl_and_target(
        entry_price:  float,
        atr:          Optional[float],
        candle_range: float,
        timeframe:    str,
        # ── Technical context inputs ──
        adx:          Optional[float] = None,
        rsi:          Optional[float] = None,
        macd_hist:    Optional[float] = None,
        atr_pct:      Optional[float] = None,
        swing_low:    Optional[float] = None,
        swing_high:   Optional[float] = None,
        bb_upper:     Optional[float] = None,
        bb_lower:     Optional[float] = None,
        s1:           Optional[float] = None,
        s2:           Optional[float] = None,
        r1:           Optional[float] = None,
        r2:           Optional[float] = None,
) -> dict:
    """
    Returns a dict with:
        stop_loss   — structure-aware, ATR-buffered
        target_1    — nearest resistance / conservative
        target_2    — next resistance / moderate (None if overbought)
        target_3    — extended target (None unless strong momentum)
        rr_ratio    — actual R:R of target_1
        risk        — risk per share in rupees
        sl_method   — explains how SL was set
        t_method    — explains how targets were set
        rsi_zone    — overbought / bullish / neutral / oversold

    Usage:
        from app.technical_indicators import apply_indicators
        from app.sl_target_helper import compute_sl_and_target

        df  = apply_indicators(df, timeframe="1d")
        row = df.iloc[-1]

        result = compute_sl_and_target(
            entry_price  = row["Close"],
            atr          = row["ATR"],
            candle_range = row["High"] - row["Low"],
            timeframe    = "1d",
            adx          = row["ADX"],
            rsi          = row["RSI"],
            macd_hist    = row["MACD_HIST"],
            atr_pct      = row["ATR_PCT"],
            swing_low    = row["SWING_LOW"],
            swing_high   = row["SWING_HIGH"],
            bb_upper     = row["BB_UPPER"],
            bb_lower     = row["BB_LOWER"],
            s1           = row["S1"],
            s2           = row["S2"],
            r1           = row["R1"],
            r2           = row["R2"],
        )
    """

    # ── 1. Resolve effective ATR ───────────────────────────────────────────────
    eff_atr = _safe(atr) or (_safe(candle_range) * 1.5 if _safe(candle_range) else None)
    if eff_atr is None or eff_atr <= 0:
        eff_atr = entry_price * 0.015   # last-resort: 1.5% of price

    multiplier = _atr_multiplier(timeframe, _safe(adx), _safe(atr_pct))
    atr_risk   = multiplier * eff_atr

    # ── 2. Stop-Loss placement ─────────────────────────────────────────────────
    support = _nearest_support(entry_price, _safe(swing_low), _safe(s1), _safe(s2))

    if support is not None:
        # Place SL below the structural support with a small ATR buffer (0.25x)
        raw_sl    = support - (0.25 * eff_atr)
        sl_method = f"Below swing/pivot support {round(support, 2)} with 0.25x ATR buffer"
        # Safety cap: if structure-based SL is too wide (>3x ATR), cap it
        if (entry_price - raw_sl) > 3.0 * eff_atr:
            raw_sl    = entry_price - (2.5 * eff_atr)
            sl_method = "Capped at 2.5x ATR (structure too far from entry)"
    else:
        # No support found — fall back to ATR-only SL
        raw_sl    = entry_price - atr_risk
        sl_method = f"{multiplier}x ATR (no nearby support found)"

    stop_loss = round(raw_sl, 2)
    risk      = entry_price - stop_loss

    # ── 3. Target placement ────────────────────────────────────────────────────
    resistance = _nearest_resistance(
        entry_price, _safe(swing_high), _safe(r1), _safe(r2), _safe(bb_upper)
    )

    # RSI context → how aggressive to be with targets
    rsi_val = _safe(rsi)
    if rsi_val is not None:
        if rsi_val > 72:
            rsi_zone = "overbought"    # near exhaustion → conservative
        elif rsi_val > 55:
            rsi_zone = "bullish"       # healthy uptrend
        elif rsi_val > 40:
            rsi_zone = "neutral"
        else:
            rsi_zone = "oversold"      # fresh breakout potential → aggressive
    else:
        rsi_zone = "neutral"

    # MACD confirmation
    macd_bullish = (
            macd_hist is not None
            and _safe(abs(macd_hist)) is not None
            and float(macd_hist) > 0
    )

    # Target 1: nearest resistance or minimum 1.5:1 RR
    if resistance is not None:
        t1_raw   = resistance
        t_method = f"T1: nearest resistance/swing high {round(t1_raw, 2)}"
    else:
        t1_raw   = entry_price + (MIN_RR_RATIO * risk)
        t_method = f"T1: no resistance found — set at min {MIN_RR_RATIO}:1 RR"

    # Enforce minimum RR on T1
    if (t1_raw - entry_price) < (MIN_RR_RATIO * risk):
        t1_raw   = entry_price + (MIN_RR_RATIO * risk)
        t_method = f"T1: bumped to min {MIN_RR_RATIO}:1 RR (resistance too close to entry)"

    target_1 = round(t1_raw, 2)

    # Target 2: only if not overbought
    target_2 = None
    if rsi_zone != "overbought":
        r2_val = _safe(r2)
        if r2_val and r2_val > target_1:
            target_2  = round(r2_val, 2)
            t_method += f" | T2: R2 pivot {target_2}"
        else:
            target_2  = round(entry_price + 2.5 * risk, 2)
            t_method += " | T2: 2.5x RR"

    # Target 3: only on strong momentum confluence
    target_3 = None
    adx_val  = _safe(adx)
    if (
            macd_bullish
            and rsi_zone in ("neutral", "oversold", "bullish")
            and (adx_val is None or adx_val > 25)
    ):
        target_3  = round(entry_price + 4.0 * risk, 2)
        t_method += " | T3: 4x RR (MACD bullish + ADX strong)"

    # ── 4. Actual R:R of T1 ───────────────────────────────────────────────────
    rr_ratio = round((target_1 - entry_price) / risk, 2) if risk > 0 else 0.0

    return {
        "stop_loss": stop_loss,
        "target_1":  target_1,
        "target_2":  target_2,
        "target_3":  target_3,
        "rr_ratio":  rr_ratio,
        "risk":      round(risk, 2),
        "sl_method": sl_method,
        "t_method":  t_method,
        "rsi_zone":  rsi_zone,
    }
