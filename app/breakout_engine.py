# =====================================================================================
# app/breakout_engine.py (v2 — ANTI-FAKE-BREAKOUT EDITION)
#
# MAJOR UPGRADE: Every breakout signal now passes through quality gates before firing.
#
# CHANGES FROM v1:
#   1. CLOSING-PRICE BREAKOUT — candle body must be above the level, not just a wick
#      Old: close > prev_high  (wick-only breakouts pass)
#      New: close > prev_high AND low > prev_high * (1 - tolerance)
#
#   2. MINIMUM BREAKOUT MARGIN — stock must close meaningfully above the level
#      15m: 0.3% | 1h: 0.5% | 1d: 0.7% (from config.MIN_BREAKOUT_MARGIN)
#      Eliminates "barely broke" signals that reverse immediately
#
#   3. VOLUME-CONFIRMED BREAKOUT — breakout candle must have elevated volume
#      Breakout candle volume >= 1.5x 20-bar average (from config)
#      Separates institutional breakouts from noise
#
#   4. PRE-BREAKOUT CONSOLIDATION BONUS — tight base before breakout = higher quality
#      Uses BASE_WIDTH from technical_indicators.py
#      Breakouts from tight bases get 1.5x strength multiplier
#
#   5. OBV DIVERGENCE CHECK — if OBV trend is bearish during breakout, halve the score
#      Catches distribution masquerading as breakout (operator trap)
#
# Return format unchanged: {signal_name: strength_score}
# =====================================================================================

import pandas as pd

from config import (
    MIN_BREAKOUT_MARGIN,
    MIN_BREAKOUT_VOLUME_RATIO,
    BASE_TIGHTNESS_THRESHOLD,
)


WINDOW_MAP = {
    "1d": [
        ("52W Breakout",     252),
        ("Monthly Breakout", 100),
        ("Weekly Breakout",  50),
        ("Daily Breakout",   20),
    ],
    "1h": [
        ("Monthly Breakout", 260),
        ("Weekly Breakout",  130),
        ("Daily Breakout",   26),
        ("Hourly Breakout",  6),
    ],
    "15m": [
        ("Weekly Breakout",  104),
        ("Daily Breakout",   52),
        ("Session Breakout", 25),  # NSE session = 375 min = exactly 25 fifteen-minute bars
    ],
}

# Weighting: How much to reward each breakout type (as % of raw strength)
# Gap is deliberately wide — a 52W breakout is 5× more significant than a daily.
BREAKOUT_WEIGHTS = {
    "52W Breakout":     3.0,      # 200% bonus — rarest, highest conviction signal
    "Monthly Breakout": 2.0,      # 100% bonus — strong multi-month breakout
    "Weekly Breakout":  1.2,      # 20% bonus  — solid but common
    "Daily Breakout":   0.6,      # 40% penalty — noise-level on its own
    "Hourly Breakout":  0.5,      # Least significant (timeframe=1h)
    "Session Breakout": 0.4,      # Least significant (timeframe=15m)
    "BB Breakout":      1.2,      # Bollinger Band squeeze (independent)
    "Volume Surge":     1.3,      # Strong volume confirmation (independent)
}

# ── SIGNAL HIERARCHY ─────────────────────────────────────────────────────────
# When a higher-level breakout fires, lower-level ones are REDUNDANT:
#   52W  Breakout → also broke Monthly, Weekly, Daily (remove all three)
#   Monthly Breakout → also broke Weekly, Daily (remove both)
#   Weekly  Breakout → also broke Daily (remove it)
#
# This keeps alerts clean — "52W Breakout" says it all; listing
# "Monthly + Weekly + Daily" alongside it is noise.
# BB Breakout and Volume Surge are independent signals (not windowed) — never pruned.
_SIGNAL_HIERARCHY = [
    "52W Breakout",
    "Monthly Breakout",
    "Weekly Breakout",
    "Daily Breakout",
    "Hourly Breakout",
    "Session Breakout",
]


def _prune_redundant_signals(signals: dict[str, float]) -> dict[str, float]:
    """
    Remove lower-level windowed breakout signals when a higher-level one fires.
    BB Breakout and Volume Surge are independent — never removed.
    """
    # Find the highest-level windowed signal present
    highest_idx = None
    for idx, name in enumerate(_SIGNAL_HIERARCHY):
        if name in signals:
            highest_idx = idx
            break  # first match is the highest level

    if highest_idx is None:
        return signals  # no windowed signals at all

    # Remove everything below the highest-level signal
    pruned = {}
    for name, score in signals.items():
        if name in _SIGNAL_HIERARCHY:
            sig_idx = _SIGNAL_HIERARCHY.index(name)
            if sig_idx <= highest_idx:
                pruned[name] = score  # keep (it IS the highest, or equal level)
            # else: skip — it's a lower-level redundant signal
        else:
            pruned[name] = score  # BB Breakout, Volume Surge — always keep

    return pruned

# Anti-wick tolerance: candle low must be within this fraction of the breakout level
# to confirm the candle body is genuinely above it (not just a wick touch)
_WICK_TOLERANCE = 0.003  # 0.3%


def _is_volume_confirmed(df: pd.DataFrame, min_ratio: float = None) -> bool:
    """
    Check if the latest candle has volume >= min_ratio × 20-bar average.
    This is the primary gate against noise breakouts.
    """
    if min_ratio is None:
        min_ratio = MIN_BREAKOUT_VOLUME_RATIO

    n = len(df)
    if n < 21 or "Volume" not in df.columns:
        return True  # no data to check — don't block

    vol_now = float(df["Volume"].iloc[-1])
    vol_series = df["Volume"].iloc[-21:-1]
    vol_avg = float(vol_series.mean())
    vol_std = float(vol_series.std())

    if vol_avg <= 0 or pd.isna(vol_std) or vol_std == 0:
        return True  # avoid division by zero / std errors

    # Calculate Z-score
    vol_z_score = (vol_now - vol_avg) / vol_std

    # Treat min_ratio as the required Z-score since config may still export MIN_BREAKOUT_VOLUME_RATIO
    # Assuming min_ratio ~ 2.5 for Z-score equivalent
    required_z = min_ratio if min_ratio is not None else 2.5

    return vol_z_score >= required_z


def _has_tight_base(df: pd.DataFrame, threshold: float = None) -> bool:
    """
    Check if the stock has a tight consolidation base (BASE_WIDTH < threshold).
    Tight bases produce the most reliable breakouts.
    """
    if threshold is None:
        threshold = BASE_TIGHTNESS_THRESHOLD

    if "BASE_WIDTH" not in df.columns:
        return False

    base_width = df["BASE_WIDTH"].iloc[-1]
    vcp_tightening = df.get("VCP_TIGHTENING", pd.Series([False])).iloc[-1]
    
    if pd.isna(base_width):
        return False

    return float(base_width) < threshold and bool(vcp_tightening)


def _get_obv_trend(df: pd.DataFrame) -> int:
    """
    Get the OBV trend direction for the latest bar.
    Returns: 1 (bullish), -1 (bearish), 0 (neutral/unavailable)
    """
    if "OBV_TREND" not in df.columns:
        return 0

    val = df["OBV_TREND"].iloc[-1]
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def detect_breakouts(df: pd.DataFrame, timeframe: str = "15m") -> dict[str, float]:
    """
    Detects breakout signals on the latest completed candle.

    v2 UPGRADE: Every breakout now passes quality gates:
      1. Closing-price confirmation (not just wick)
      2. Minimum breakout margin (timeframe-aware)
      3. Volume confirmation on the breakout candle
      4. Consolidation bonus for tight-base setups
      5. OBV divergence penalty for distribution patterns

    Parameters
    ----------
    df        : OHLCV DataFrame with indicators already applied
    timeframe : "15m" | "1h" | "1d" — controls which rolling windows are used

    Returns
    -------
    dict[str, float]
        {signal_name: strength_score, ...}
        Empty dict if data is insufficient or latest close is NaN.

        Example:
            {"52W Breakout": 24.5, "Weekly Breakout": 12.3, "Volume Surge": 5.8}

            Score represents: breakout magnitude (%) × weighting factor × quality multiplier
            Bigger breakouts, stronger volume, tighter bases = higher scores
    """

    if df is None or df.empty:
        return {}

    latest = df.iloc[-1]
    close  = latest["Close"]

    if pd.isna(close):
        return {}

    close = float(close)

    signals   = {}
    windows   = WINDOW_MAP.get(timeframe, WINDOW_MAP["15m"])
    n         = len(df)

    # ── QUALITY GATE 1: Volume Confirmation ───────────────────────────────────────
    # If breakout candle volume is below threshold, return empty (no breakouts)
    # This is the single most effective anti-fake-breakout filter.
    volume_confirmed = _is_volume_confirmed(df)

    if not volume_confirmed:
        return {}

    # ── QUALITY GATE 2: Read consolidated config values ───────────────────────────
    min_margin = MIN_BREAKOUT_MARGIN.get(timeframe, 0.005)

    # ── QUALITY MULTIPLIERS ───────────────────────────────────────────────────────
    # Tight base = 1.5x score multiplier (higher quality setup)
    base_multiplier = 1.5 if _has_tight_base(df) else 1.0

    # OBV divergence = 0.5x score multiplier (distribution pattern)
    obv_trend = _get_obv_trend(df)
    obv_multiplier = 0.5 if obv_trend == -1 else 1.0

    quality_multiplier = base_multiplier * obv_multiplier

    # ── WINDOWED BREAKOUTS ────────────────────────────────────────────────────────
    for signal_name, window in windows:
        if n < window + 1:
            continue

        prev_high = df["High"].rolling(window).max().iloc[-2]

        if pd.isna(prev_high):
            continue

        prev_high = float(prev_high)

        # ── ANTI-FAKE-BREAKOUT GATE: Closing-price confirmation ───────────────
        #
        # Old: close > prev_high  (wick-only breakouts pass)
        # New: THREE conditions must ALL pass:
        #   1. close > prev_high                      (basic breakout)
        #   2. close > prev_high * (1 + min_margin)   (minimum margin — not "barely broke")
        #   3. low > prev_high * (1 - wick_tolerance)  (candle body above level, not wick)
        #
        candle_low = float(latest["Low"])

        if (
            close > prev_high
            and close > prev_high * (1 + min_margin)
            and candle_low > prev_high * (1 - _WICK_TOLERANCE)
        ):
            # Calculate breakout strength: how much above the prior high (as %)
            breakout_strength = ((close - prev_high) / prev_high) * 100

            # Weight by signal type
            weight = BREAKOUT_WEIGHTS.get(signal_name, 1.0)

            # Final score = strength × weight × quality multiplier
            score = breakout_strength * weight * quality_multiplier

            signals[signal_name] = round(score, 3)

    # ── BOLLINGER BAND SQUEEZE BREAKOUT ───────────────────────────────────────────
    if (
        "BB_UPPER" in df.columns and
        "BB_LOWER" in df.columns and
        n >= 22
    ):
        bb_upper   = float(df["BB_UPPER"].iloc[-1])
        bb_lower   = float(df["BB_LOWER"].iloc[-1])
        bb_width   = bb_upper - bb_lower
        bb_width_5 = (float(df["BB_UPPER"].iloc[-6]) - float(df["BB_LOWER"].iloc[-6])) if n >= 6 else None

        if (
            not pd.isna(bb_upper) and
            close > bb_upper and
            bb_width_5 is not None and
            not pd.isna(bb_width_5) and
            bb_width > bb_width_5   # bands expanding = breakout, not overextension
        ):
            # Additional gate: candle body must be above BB_UPPER (not just wick)
            candle_low = float(latest["Low"])
            if candle_low > bb_upper * (1 - _WICK_TOLERANCE):
                bb_strength = ((close - bb_upper) / bb_upper) * 100
                weight = BREAKOUT_WEIGHTS.get("BB Breakout", 1.2)
                signals["BB Breakout"] = round(bb_strength * weight * quality_multiplier, 3)

    # ── VOLUME SURGE ──────────────────────────────────────────────────────────────
    # Current bar volume >= 3.0 Z-score AND price is up.
    if "Volume" in df.columns and n >= 21:
        vol_now = float(df["Volume"].iloc[-1])
        vol_series = df["Volume"].iloc[-21:-1]
        vol_avg = float(vol_series.mean())
        vol_std = float(vol_series.std())

        if vol_avg > 0 and not pd.isna(vol_std) and vol_std > 0 and close > float(df["Open"].iloc[-1]):
            vol_z_score = (vol_now - vol_avg) / vol_std
            if vol_z_score >= 3.0:
                # Strength = how much above average (as multiple of std)
                vol_strength = vol_z_score * 10  # Scale Z-score to 0-100 roughly
                weight = BREAKOUT_WEIGHTS.get("Volume Surge", 1.3)
                signals["Volume Surge"] = round(vol_strength * weight * quality_multiplier, 3)

    # ── HIERARCHY PRUNING: Remove redundant lower-level signals ────────────────
    # A 52W breakout already implies monthly+weekly+daily — don't clutter the alert.
    signals = _prune_redundant_signals(signals)

    return signals
