# =====================================================================================
# app/scoring_engine.py
# =====================================================================================
#
# SCORING BREAKDOWN (max 100):
#   Category        — 30 pts  (additive for multi-category)
#   Breakouts       — 25 pts  (capped at 3×8, +1 bonus for 52W)
#   RSI Quality     — 15 pts  (sweet-spot band + momentum direction)
#   Volume Quality  — 20 pts  (surge + sustained + no thin-volume fake)
#   Trend Strength  — 10 pts  (MA alignment + slope health)
#
# HARD DISQUALIFIERS (returns 0 immediately + reason string for logging):
#   • Avg volume < 50K (illiquid)
#   • Volume spike on a down candle (distribution)
#   • Upper wick > 40% of range (rejection candle)
#   • ADX < 20 (no directional trend)
#   • RSI divergence: price ↑ but RSI ↓ (hidden weakness)
#   • Price above BB upper with low volume (overextension)
#   • 3 doji candles before breakout (exhaustion)
#   • Volume not sustained over last 3 bars (isolated spike)
#
# =====================================================================================

import logging

logger = logging.getLogger(__name__)

# =====================================================================================
# CATEGORY WEIGHTS
# =====================================================================================

SCORE_CATEGORY = {
    "Elite Compounder": 30,
    "High Growth":      22,
    "Mature Quality":   14,
}

# =====================================================================================
# HARD DISQUALIFIER CHECK
# =====================================================================================

def check_hard_disqualifiers(ticker, latest, volume_ratio):
    """
    Returns (True, reason) if the stock should be REJECTED regardless of score.
    Returns (False, None) if it passes all hard filters.
    """

    # 1. VOLUME AUTHENTICITY
    avg_vol_20 = float(ticker["Volume"].tail(20).mean())
    if avg_vol_20 < 50_000:
        return True, "Avg volume < 50K (illiquid)"

    # 2. FAKE MOMENTUM — volume spike on a bearish close
    candle_mid = (float(latest["High"]) + float(latest["Low"])) / 2
    if float(latest["Close"]) < candle_mid and volume_ratio >= 2.0:
        return True, "High volume on bearish close (distribution)"

    # 3. UPPER WICK DOMINANCE — rejection candle
    candle_range = float(latest["High"]) - float(latest["Low"])
    upper_wick   = float(latest["High"]) - float(latest["Close"])
    if candle_range > 0 and (upper_wick / candle_range) > 0.40:
        return True, "Upper wick dominance > 40% (rejection candle)"

    # 4. ADX FILTER — directional strength required
    if "ADX" in ticker.columns:
        adx_val = float(latest.get("ADX", 0) or 0)
        if adx_val > 0 and adx_val < 20:
            return True, f"ADX {adx_val:.1f} < 20 (no directional trend)"

    # 5. RSI DIVERGENCE — price up but RSI falling
    if "RSI" in ticker.columns and len(ticker) >= 6:
        rsi_now    = float(latest["RSI"])
        rsi_prev   = float(ticker["RSI"].iloc[-6])
        close_now  = float(latest["Close"])
        close_prev = float(ticker["Close"].iloc[-6])
        if close_now > close_prev * 1.005 and rsi_now < rsi_prev - 3:
            return True, f"RSI divergence (price ↑ RSI ↓ {rsi_now:.1f} vs {rsi_prev:.1f})"

    # 6. OVEREXTENSION — above BB upper with weak volume
    if "BB_UPPER" in ticker.columns:
        bb_upper = float(latest.get("BB_UPPER", 0) or 0)
        if bb_upper > 0 and float(latest["Close"]) > bb_upper and volume_ratio < 1.8:
            return True, "Price above BB upper with weak volume (overextension)"

    # 7. EXHAUSTION — 3 consecutive doji/narrow candles
    if len(ticker) >= 4:
        exhaustion_count = 0
        for i in range(-4, -1):
            c   = float(ticker["Close"].iloc[i])
            o   = float(ticker["Open"].iloc[i])
            h   = float(ticker["High"].iloc[i])
            l   = float(ticker["Low"].iloc[i])
            rng  = h - l
            body = abs(c - o)
            if rng > 0 and (body / rng) < 0.25:
                exhaustion_count += 1
        if exhaustion_count >= 3:
            return True, "3 doji/narrow candles before breakout (exhaustion)"

    # 8. VOLUME CONFIRMATION — require at least 2 of last 3 bars above avg
    if len(ticker) >= 4:
        avg_vol = float(ticker["Volume"].tail(20).mean())
        recent_above = sum(
            1 for i in range(-3, 0)
            if float(ticker["Volume"].iloc[i]) > avg_vol * 0.8
        )
        if recent_above < 2:
            return True, "Volume not sustained over last 3 bars (isolated spike)"

    return False, None


# =====================================================================================
# BONUS SCORE MODIFIERS
# =====================================================================================

def bonus_modifiers(ticker, latest, volume_ratio):
    bonus = 0

    # Sustained volume: 3-bar avg > 1.5x 20-bar avg
    if len(ticker) >= 23:
        avg_20 = float(ticker["Volume"].tail(20).mean())
        avg_3  = float(ticker["Volume"].tail(3).mean())
        if avg_20 > 0 and (avg_3 / avg_20) >= 1.5:
            bonus += 3

    # Full MA bull stack: EMA20 > SMA50 > SMA200
    if all(c in ticker.columns for c in ["EMA20", "SMA50", "SMA200"]):
        e20  = float(latest.get("EMA20", 0) or 0)
        s50  = float(latest.get("SMA50", 0) or 0)
        s200 = float(latest.get("SMA200", 0) or 0)
        if e20 > 0 and s50 > 0 and s200 > 0 and e20 > s50 > s200:
            bonus += 3

    # RSI accelerating upward (RSI now > RSI 3 bars ago + 2)
    if "RSI" in ticker.columns and len(ticker) >= 4:
        rsi_now  = float(latest["RSI"])
        rsi_3ago = float(ticker["RSI"].iloc[-4])
        if rsi_now > rsi_3ago + 2:
            bonus += 2

    # Closed in top 20% of candle
    candle_range = float(latest["High"]) - float(latest["Low"])
    if candle_range > 0:
        close_position = (float(latest["Close"]) - float(latest["Low"])) / candle_range
        if close_position >= 0.80:
            bonus += 2

    # Volume climax: > 5x avg
    if volume_ratio >= 5.0:
        bonus += 2

    # Near 52-week high (within 3%)
    if "HIGH_52W" in ticker.columns:
        high52 = float(latest.get("HIGH_52W", 0) or 0)
        if high52 > 0:
            proximity = float(latest["Close"]) / high52
            if proximity >= 0.97:
                bonus += 2

    # Penalty: single candle move > 8% (gap-up chase risk)
    if len(ticker) >= 2:
        prev_close = float(ticker["Close"].iloc[-2])
        if prev_close > 0:
            single_move = (float(latest["Close"]) - prev_close) / prev_close * 100
            if single_move > 8:
                bonus -= 5

    # Penalty: RSI > 78
    if "RSI" in ticker.columns:
        rsi_val = float(latest["RSI"])
        if rsi_val > 78:
            bonus -= 3

    return bonus


# =====================================================================================
# MAIN SCORING FUNCTION
# =====================================================================================

def calculate_score(
    category,
    breakout_count,
    rsi,
    volume_ratio,
    breakout_signals=None,
    ticker=None,
    latest=None,
    symbol=None,        # optional — used only for logging disqualifier reason
):
    """
    Returns integer score 0–100.
    Returns 0 if any hard disqualifier fires (reason is logged at WARNING level).

    Parameters
    ----------
    category         : str            — stock category string
    breakout_count   : int            — number of breakout signals
    rsi              : float          — current RSI value
    volume_ratio     : float          — current vol / 20-bar avg vol
    breakout_signals : list[str]      — list of signal name strings (optional)
    ticker           : pd.DataFrame   — full OHLCV + indicator DataFrame (required for
                                        hard disqualifiers, trend scoring, bonus modifiers)
    latest           : pd.Series      — ticker.iloc[-1] (required alongside ticker)
    symbol           : str            — ticker symbol, used only for logging (optional)
    """

    score = 0

    # ------------------------------------------------------------------
    # HARD DISQUALIFIERS
    # ------------------------------------------------------------------
    if ticker is not None and latest is not None:
        disq, reason = check_hard_disqualifiers(ticker, latest, volume_ratio)
        if disq:
            if symbol:
                logger.warning(f"🚫 DISQUALIFIED {symbol}: {reason}")
            return 0

    # ------------------------------------------------------------------
    # 1. CATEGORY — additive for multi-category stocks
    # ------------------------------------------------------------------
    for label, pts in SCORE_CATEGORY.items():
        if label in category:
            score += pts

    # ------------------------------------------------------------------
    # 2. BREAKOUTS — 25 pts max (3 signals × 8 pts, +1 for 52W)
    # ------------------------------------------------------------------
    score += min(breakout_count, 3) * 8
    if breakout_signals and any("52W" in s for s in breakout_signals):
        score += 1

    # ------------------------------------------------------------------
    # 3. RSI QUALITY — 15 pts max
    # ------------------------------------------------------------------
    if 62 <= rsi <= 72:
        score += 15
    elif 58 <= rsi < 62:
        score += 10
    elif 72 < rsi <= 78:
        score += 7
    elif 55 <= rsi < 58:
        score += 4
    elif 78 < rsi <= 82:
        score += 2

    # ------------------------------------------------------------------
    # 4. VOLUME QUALITY — 20 pts max
    # ------------------------------------------------------------------
    if volume_ratio >= 4.0:
        score += 20
    elif volume_ratio >= 3.0:
        score += 17
    elif volume_ratio >= 2.5:
        score += 14
    elif volume_ratio >= 2.0:
        score += 10
    elif volume_ratio >= 1.5:
        score += 5
    elif volume_ratio >= 1.2:
        score += 2

    # ------------------------------------------------------------------
    # 5. TREND STRENGTH — 10 pts max
    # ------------------------------------------------------------------
    if ticker is not None and latest is not None:
        trend_pts = 0

        e20 = float(latest.get("EMA20", 0) or 0)
        s50 = float(latest.get("SMA50", 0) or 0)
        if e20 > 0 and s50 > 0 and e20 > s50:
            trend_pts += 3

        s200 = float(latest.get("SMA200", 0) or 0)
        if s50 > 0 and s200 > 0 and s50 > s200:
            trend_pts += 3

        if "ADX" in ticker.columns:
            adx_val = float(latest.get("ADX", 0) or 0)
            if adx_val >= 25:
                trend_pts += 2
            elif adx_val >= 20:
                trend_pts += 1

        if "MACD" in ticker.columns and "MACD_SIGNAL" in ticker.columns:
            macd_val = float(latest.get("MACD", 0) or 0)
            macd_sig = float(latest.get("MACD_SIGNAL", 0) or 0)
            if macd_val > macd_sig:
                trend_pts += 2

        score += min(trend_pts, 10)

    # ------------------------------------------------------------------
    # 6. BONUS MODIFIERS
    # ------------------------------------------------------------------
    if ticker is not None and latest is not None:
        score += bonus_modifiers(ticker, latest, volume_ratio)

    return max(0, min(score, 100))
