# =====================================================================================
# app/breakout_engine.py (FIXED)
#
# MAJOR FIX: Return weighted scores instead of plain signal strings
# Before: ["Daily Breakout", "Weekly Breakout"]  (equal weight, no signal strength)
# After:  {"Daily Breakout": 8.0, "Weekly Breakout": 15.2}  (strength-weighted)
#
# This allows scoring_engine to reward bigger breakouts more than small ones.
# =====================================================================================

import pandas as pd


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
BREAKOUT_WEIGHTS = {
    "52W Breakout":     1.8,      # 80% bonus on raw strength (rarest, most significant)
    "Monthly Breakout": 1.4,
    "Weekly Breakout":  1.1,
    "Daily Breakout":   0.9,
    "Hourly Breakout":  0.8,
    "Session Breakout": 0.7,      # Least significant (timeframe=15m)
    "BB Breakout":      1.2,      # Bollinger Band squeeze
    "Volume Surge":     1.3,      # Strong volume confirmation
}


def detect_breakouts(df: pd.DataFrame, timeframe: str = "15m") -> dict[str, float]:
    """
    Detects breakout signals on the latest completed candle.
    
    FIX: Returns {signal_name: strength_score} instead of [signal_name, ...]
    
    Strength score = how much above the prior high (as % of price) × weighting factor
    
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
            
            Score represents: breakout magnitude (%) × weighting factor
            Bigger breakouts and stronger signals = higher scores
    """

    if df is None or df.empty:
        return {}

    latest = df.iloc[-1]
    close  = latest["Close"]

    if pd.isna(close):
        return {}

    signals   = {}
    windows   = WINDOW_MAP.get(timeframe, WINDOW_MAP["15m"])
    n         = len(df)

    # ── WINDOWED BREAKOUTS ──────────────────────────────────────────────────────────
    for signal_name, window in windows:
        if n < window + 1:
            continue

        prev_high = df["High"].rolling(window).max().iloc[-2]

        if pd.isna(prev_high):
            continue

        if close > prev_high:
            # Calculate breakout strength: how much above the prior high (as %)
            breakout_strength = ((close - prev_high) / prev_high) * 100
            
            # Weight by signal type
            weight = BREAKOUT_WEIGHTS.get(signal_name, 1.0)
            
            # Final score = strength × weight (higher = more significant)
            score = breakout_strength * weight
            
            signals[signal_name] = score

    # ── BOLLINGER BAND SQUEEZE BREAKOUT ──────────────────────────────────────────────
    if (
        "BB_UPPER" in df.columns and
        "BB_LOWER" in df.columns and
        n >= 22
    ):
        bb_upper   = df["BB_UPPER"].iloc[-1]
        bb_lower   = df["BB_LOWER"].iloc[-1]
        bb_width   = bb_upper - bb_lower
        bb_width_5 = (df["BB_UPPER"].iloc[-6] - df["BB_LOWER"].iloc[-6]) if n >= 6 else None

        if (
            not pd.isna(bb_upper) and
            close > bb_upper and
            bb_width_5 is not None and
            not pd.isna(bb_width_5) and
            bb_width > bb_width_5   # bands expanding = breakout, not overextension
        ):
            # Strength = how much above upper band
            bb_strength = ((close - bb_upper) / bb_upper) * 100
            weight = BREAKOUT_WEIGHTS.get("BB Breakout", 1.2)
            signals["BB Breakout"] = bb_strength * weight

    # ── VOLUME SURGE ─────────────────────────────────────────────────────────────────
    # Current bar volume >= 3x 20-bar average AND price is up.
    if "Volume" in df.columns and n >= 21:
        vol_now = float(df["Volume"].iloc[-1])
        # GAP 1 FIX: exclude the current bar from the baseline to avoid the breakout
        # candle inflating its own average and understating the surge multiple.
        vol_avg = float(df["Volume"].iloc[-21:-1].mean())

        if vol_avg > 0 and vol_now >= 3.0 * vol_avg and close > float(df["Open"].iloc[-1]):
            # Strength = how much above volume average (as multiple)
            vol_strength = (vol_now / vol_avg - 1.0) * 100  # Convert to %
            weight = BREAKOUT_WEIGHTS.get("Volume Surge", 1.3)
            signals["Volume Surge"] = vol_strength * weight

    return signals


def get_signal_names(signals: dict[str, float]) -> list[str]:
    """Convenience function: extract signal names from weighted scores dict."""
    return list(signals.keys())


def get_signal_strength(signals: dict[str, float], signal_name: str) -> float:
    """Convenience function: get strength score for a specific signal."""
    return signals.get(signal_name, 0.0)


def get_total_signal_strength(signals: dict[str, float]) -> float:
    """Convenience function: sum of all signal strengths."""
    return sum(signals.values()) if signals else 0.0
