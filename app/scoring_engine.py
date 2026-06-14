# =====================================================================================
# app/scoring_engine.py
#
# WHAT THIS FILE DOES:
#   Calculates a composite quality score (0–100) for each candidate stock that has
#   passed the scanner's upstream filter stack. The score is used as a final gate —
#   only stocks above the scanner-specific threshold (78/80/82) generate alerts.
#
#   The score has two layers:
#     1. HARD DISQUALIFIERS — if any fire, score returns 0 immediately (no partial credit)
#     2. ADDITIVE SCORING — five independent components, each with a max point budget
#
# SCORING BREAKDOWN (max 100 + bonuses):
#   Category weight   — 30 pts   (Wealth Compounder/Long Term Compounder/Consistent Performer/Blue Chip Stable/Debt-Free Cash Generator/Capital Efficient/Efficient Lender/Dividend Aristocrat/High Momentum/Recovery Play)
#   Breakout signals  — 25 pts   (signal count × 8, capped at 3; +1 for 52W signal)
#   RSI quality       — 15 pts   (sweet-spot band + direction bonus)
#   Volume quality    — 20 pts   (surge intensity)
#   Trend strength    — 10 pts   (MA alignment + ADX + MACD)
#   Bonus modifiers   — up to +21 pts (sustained vol, bull stack, RSI accel, close pos,
#                                      climax, ATR quality, delivery conviction)
#
# HARD DISQUALIFIERS (returns 0 immediately):
#   1. Avg volume < floor   (illiquid — unreliable fills)
#   2. Volume spike on bearish close (distribution — smart money selling)
#   3. Upper wick > 40% of range (rejection candle — buyers lost control)
#   4. ADX < 25 (RAISED from 22 — weak/establishing trends disqualified)
#   5. RSI divergence: price ↑ but RSI ↓ over lookback window (hidden weakness)
#   6. Price above BB upper with volume ratio < 1.8 (overextension without conviction)
#   7. 3 doji/narrow candles in last 4 bars (pre-breakout exhaustion)
#
# ── PERFORMANCE FIX SUMMARY (v5) ──────────────────────────────────────────────────
#
# FIX 1 — ADX hard disqualifier raised from 22 → 25 (from config.ADX_MIN_THRESHOLD)
#
#   Root cause: ADX 22–24 stocks were passing the disqualifier and generating alerts
#   in choppy / trending-weakly markets. These are statistically poor breakout setups.
#   ADX 25 is the widely-accepted minimum for an "established" trend. Stocks below
#   this have no directional commitment and breakouts often reverse immediately.
#
#   Implementation: check_hard_disqualifiers() now reads ADX_MIN_THRESHOLD from
#   config.py (default 25) instead of hardcoding 22. The scoring step 6 that awards
#   +1 for ADX ≥ 22 is also raised to ≥ 25, so the ADX scoring floor is consistent
#   with the disqualifier floor.
#
# FIX 2 — RSI scoring sweet spot tightened
#
#   Old sweet spot: 58–72 (15 pts), 55–57 (6 pts), 72–75 (10 pts)
#   New sweet spot: 60–70 (15 pts), 57–59 (6 pts), 70–74 (10 pts)
#
#   Rationale: The old 58–72 band was too wide and awarded full RSI points to
#   stocks at either edge. RSI 58 is barely above neutral; RSI 72 is approaching
#   overbought. The tighter 60–70 range rewards the genuinely healthy momentum
#   zone. The taper bands on both sides are kept to avoid cliff edges.
#
# FIX 3 — Volume scoring floor raised
#
#   Old: 1.2× → 2 pts    New: 1.2× → 0 pts (removed — 1.2× is not real conviction)
#   Old: 1.5× → 5 pts    New: 1.5× → 3 pts (reduced — entry-level minimum)
#   Old: 2.0× → 10 pts   New: 2.0× → 7 pts
#   Old: 2.5× → 14 pts   New: 2.5× → 12 pts
#   Old: 3.0× → 17 pts   New: 3.0× → 15 pts
#   Old: 4.0× → 20 pts   New: 4.0× → 20 pts (unchanged — climax volume still max)
#
#   Rationale: The old scoring gave 2 pts for 1.2× and 5 pts for 1.5× volume.
#   Combined with the old low score thresholds, a stock with barely-above-average
#   volume could still accumulate enough points to alert. Reducing the low-end
#   volume scores forces stocks to have genuinely elevated volume to score well.
#
# FIX 4 — Overextension disqualifier tightened
#
#   Old: BB_UPPER breach with volume_ratio < 1.8 → disqualify
#   New: BB_UPPER breach with volume_ratio < 2.0 → disqualify
#
#   Rationale: A stock touching its upper Bollinger Band on only 1.8× average
#   volume is approaching overextension without the conviction needed to sustain
#   the move. Raising to 2.0× is consistent with the raised MIN_VOLUME_RATIO
#   across all timeframes in config.py. Keeps the filter coherent end-to-end.
#
# FIX 5 — Gap-from-support penalty added (new)
#
#   Stocks that have already run >5% above SMA50 are chasing. A breakout at
#   SMA50+8% has poor risk/reward — the stock is extended, not setting up fresh.
#   New penalty: -6 pts if Close > SMA50 × 1.05 (more than 5% above 50-day MA).
#   Applied in bonus_modifiers() for ALL timeframes (the extension risk is the same
#   whether the bar is daily, hourly, or 15m).
#
#   Why 5% and not a different threshold?
#   - < 3% above SMA50: still "near support" — no penalty
#   - 3–5% above SMA50: moderately extended — borderline, no penalty to avoid false positives
#   - > 5% above SMA50: clearly extended — penalise, likely chasing
#   - > 8% above SMA50: very extended — the gap-up chase penalty already catches these
#
# =====================================================================================

import logging

# FIX 1: Import ADX_MIN_THRESHOLD from config instead of hardcoding
from config import (
    ADX_MIN_THRESHOLD,
    CLIMAX_VOLUME_LOOKBACK,
    LOWER_HIGH_LOOKBACK,
    MIN_CANDLE_RANGE_PCT,
    BASE_TIGHTNESS_THRESHOLD,
    BASE_VOLATILITY_THRESHOLD,
)

logger = logging.getLogger(__name__)

# =====================================================================================
# CATEGORY WEIGHTS
# =====================================================================================

SCORE_CATEGORY = {
    # ── Non-financial (PATH A) ────────────────────────────────────────────────────
    "Debt-Free Cash Generator": 30,   # Zero debt, strong ROE, huge stability
    "Wealth Compounder":     30,   # High ROE, strong margins, low debt — top tier
    "Long Term Compounder":  28,   # 5Y+ proven growth + fair valuation — highest conviction
    "Dividend Aristocrat":   27,   # High yield + quality
    "Capital Efficient":     26,   # Asset light, exceptional ROE
    "Undervalued Growth":    24,   # Fast growth at a deep discount
    "High Momentum":         22,   # Explosive recent growth — strong but needs confirmation
    "Consistent Performer":  18,   # Steady 10%+ growth — reliable but not explosive
    "Blue Chip Stable":      14,   # Large cap stability — safe but limited upside
    "Recovery Play":          8,   # Turnaround — speculative, lowest weight

    # ── Financial (PATH B) — set equal to non-financial analogues ─────────────────
    "Top Bank/NBFC":             30,
    "Efficient Lender":          26,
    "Fast Growing Financial":    22,
    "Blue Chip Financial":       14,
    "Financial Recovery":         8,
}

# =====================================================================================
# RSI DIVERGENCE LOOKBACK — per timeframe
# =====================================================================================

RSI_DIVERGENCE_LOOKBACK = {
    "1d":  5,
    "1h":  6,
    "15m": 6,
}

# =====================================================================================
# DELIVERY CONVICTION THRESHOLDS
# =====================================================================================

DELIVERY_BONUS_TIERS = [
    (60.0, 6),
    (40.0, 4),
    (25.0, 2),
    (0.0,  0),
]

# =====================================================================================
# HARD DISQUALIFIER CHECK
# =====================================================================================

def check_hard_disqualifiers(ticker, latest, volume_ratio, symbol=None, timeframe="15m", min_vol=50_000):
    """
    Checks for hard structural flaws that invalidate a breakout signal.

    Parameters
    ----------
    ticker       : pd.DataFrame — full OHLCV + indicator data
    latest       : pd.Series   — ticker.iloc[-1] (the bar being evaluated)
    volume_ratio : float       — current bar volume / 20-bar average
    symbol       : str         — used only for logging (optional)
    timeframe    : str         — "1d", "1h", or "15m" — affects RSI divergence lookback
    min_vol      : int         — minimum 20-bar average volume threshold (timeframe-aware)

    Returns
    -------
    (True, reason_string)  if the stock is disqualified
    (False, None)          if all checks pass
    """

    tag = f"[{symbol}] " if symbol else ""

    # ── DISQUALIFIER 1: ILLIQUID STOCK ──────────────────────────────────────────────
    avg_vol_20 = float(ticker["Volume"].iloc[-21:-1].mean())
    if avg_vol_20 < min_vol:
        reason = f"Avg vol {avg_vol_20:,.0f} < {min_vol:,} (illiquid)"
        logger.warning(f"🚫 {tag}DISQ: {reason}")
        return True, reason

    # ── DISQUALIFIER 2: DISTRIBUTION CANDLE ─────────────────────────────────────────
    candle_mid = (float(latest["High"]) + float(latest["Low"])) / 2
    if float(latest["Close"]) < candle_mid and volume_ratio >= 2.0:
        reason = f"High volume ({volume_ratio:.1f}x) on bearish close (distribution candle)"
        logger.warning(f"🚫 {tag}DISQ: {reason}")
        return True, reason

    # ── DISQUALIFIER 3: REJECTION CANDLE ────────────────────────────────────────────
    candle_range = float(latest["High"]) - float(latest["Low"])
    upper_wick   = float(latest["High"]) - float(latest["Close"])
    if candle_range > 0 and (upper_wick / candle_range) > 0.40:
        wick_pct = upper_wick / candle_range
        reason = f"Upper wick {wick_pct:.0%} of range > 40% (rejection candle)"
        logger.warning(f"🚫 {tag}DISQ: {reason}")
        return True, reason

    # ── DISQUALIFIER 4: NO DIRECTIONAL TREND (ADX) ──────────────────────────────────
    #
    # PRACTICAL FIX: ADX threshold is now timeframe-aware.
    #
    # Why: ADX naturally reads lower on intraday bars than daily bars because
    # short-term bars have more noise. A 15m or 1H ADX of 20 represents a genuinely
    # trending stock — the same "trend strength" that shows as ADX 28 on a daily chart.
    # Applying the strict EOD threshold (25) to intraday was disqualifying valid setups.
    #
    # Thresholds:
    #   1d  → ADX_MIN_THRESHOLD (25 from config) — strict, EOD quality standard
    #   1h  → 20  — intraday bars have lower ADX naturally; 20 is a real trend
    #   15m → 18  — very short bars; 18 confirms directional movement exists
    #
    if "ADX" in ticker.columns:
        adx_val = float(latest.get("ADX", 0) or 0)
        if timeframe == "1d":
            adx_floor = ADX_MIN_THRESHOLD   # 25
        elif timeframe == "1h":
            adx_floor = 20
        else:                               # 15m
            adx_floor = 18
        if 0 < adx_val < adx_floor:
            reason = (
                f"ADX {adx_val:.1f} < {adx_floor} "
                f"(trend too weak for {timeframe} timeframe)"
            )
            logger.warning(f"🚫 {tag}DISQ: {reason}")
            return True, reason

    # ── DISQUALIFIER 5: RSI BEARISH DIVERGENCE ──────────────────────────────────────
    if "RSI" in ticker.columns:
        lookback = RSI_DIVERGENCE_LOOKBACK.get(timeframe, 6)
        if len(ticker) > lookback:
            rsi_now    = float(latest["RSI"])
            rsi_prev   = float(ticker["RSI"].iloc[-1 - lookback])
            close_now  = float(latest["Close"])
            close_prev = float(ticker["Close"].iloc[-1 - lookback])

            if timeframe == "1d":
                if close_now > close_prev and rsi_now < rsi_prev:
                    reason = (
                        f"RSI bearish divergence [EOD]: price "
                        f"+{(close_now/close_prev-1)*100:.1f}% "
                        f"but RSI {rsi_prev:.1f} → {rsi_now:.1f} "
                        f"(↓{rsi_prev-rsi_now:.1f} pts over {lookback} days)"
                    )
                    logger.warning(f"🚫 {tag}DISQ: {reason}")
                    return True, reason
            else:
                if close_now > close_prev * 1.005 and rsi_now < rsi_prev - 3:
                    reason = (
                        f"RSI bearish divergence: price +{(close_now/close_prev-1)*100:.1f}% "
                        f"but RSI {rsi_prev:.1f} → {rsi_now:.1f} "
                        f"(↓{rsi_prev-rsi_now:.1f} pts)"
                    )
                    logger.warning(f"🚫 {tag}DISQ: {reason}")
                    return True, reason

    # ── DISQUALIFIER 6: OVEREXTENSION WITHOUT VOLUME ────────────────────────────────
    #
    # PRACTICAL FIX: Volume conviction threshold is timeframe-aware.
    #
    # EOD: 2.0x required — daily bar needs real conviction to break BB upper
    # 1H:  1.5x required — hourly breakouts are momentum-driven, 1.5x is real
    # 15m: 1.3x required — 15m BB breaks happen frequently; even 1.3x is elevated
    #
    if "BB_UPPER" in ticker.columns:
        bb_upper = float(latest.get("BB_UPPER", 0) or 0)
        if timeframe == "1d":
            bb_vol_floor = 2.0
        elif timeframe == "1h":
            bb_vol_floor = 1.5
        else:   # 15m
            bb_vol_floor = 1.3
        if bb_upper > 0 and float(latest["Close"]) > bb_upper and volume_ratio < bb_vol_floor:
            reason = (
                f"Price above BB upper (₹{float(latest['Close']):.2f} > ₹{bb_upper:.2f}) "
                f"with weak volume ({volume_ratio:.1f}x < {bb_vol_floor}x) — overextension risk"
            )
            logger.warning(f"🚫 {tag}DISQ: {reason}")
            return True, reason

    # ── DISQUALIFIER 7: PRE-BREAKOUT EXHAUSTION (DOJI CLUSTER) ─────────────────────
    if len(ticker) >= 4:
        exhaustion_count = 0
        for i in range(-4, -1):
            c    = float(ticker["Close"].iloc[i])
            o    = float(ticker["Open"].iloc[i])
            h    = float(ticker["High"].iloc[i])
            l    = float(ticker["Low"].iloc[i])
            rng  = h - l
            body = abs(c - o)
            if rng > 0 and (body / rng) < 0.25:
                exhaustion_count += 1
        if exhaustion_count >= 3:
            reason = f"{exhaustion_count} doji/narrow candles in last 4 bars (exhaustion before breakout)"
            logger.warning(f"🚫 {tag}DISQ: {reason}")
            return True, reason

    # ── DISQUALIFIER 8: CLIMAX TOP PATTERN (OPERATOR DISTRIBUTION) ──────────────
    #
    # Classic smart-money exit: operators push price to new highs on massive volume,
    # then sell into retail buying. The candle has:
    #   - Highest volume in CLIMAX_VOLUME_LOOKBACK bars
    #   - Upper wick > 25% of candle range (selling into strength)
    #   - Close in bottom 40% of range (buyers lost control)
    #
    # This is THE signature of institutional distribution — never trade into it.
    #
    lookback = min(CLIMAX_VOLUME_LOOKBACK, len(ticker) - 1)
    if lookback >= 5:
        latest_vol  = float(latest["Volume"])
        max_vol     = float(ticker["Volume"].iloc[-lookback - 1:-1].max())
        candle_high = float(latest["High"])
        candle_low  = float(latest["Low"])
        candle_rng  = candle_high - candle_low

        if candle_rng > 0 and latest_vol > max_vol:
            upper_wick_pct  = (candle_high - float(latest["Close"])) / candle_rng
            close_pos       = (float(latest["Close"]) - candle_low) / candle_rng
            if upper_wick_pct > 0.25 and close_pos < 0.40:
                reason = (
                    f"Climax top: highest vol in {lookback} bars "
                    f"({latest_vol:,.0f} > {max_vol:,.0f}), "
                    f"upper wick {upper_wick_pct:.0%}, "
                    f"close in bottom {close_pos:.0%} (distribution candle)"
                )
                logger.warning(f"🚫 {tag}DISQ: {reason}")
                return True, reason

    # ── DISQUALIFIER 9: LOWER-HIGH PATTERN (FAILED BREAKOUT RETEST) ─────────────
    #
    # If the stock is making lower highs, the trend is reversing:
    #   - Current bar's high < high from 3 bars ago
    #   - AND high from 3 bars ago < high from 6 bars ago
    # This means the breakout has already failed — buying here is chasing a reversal.
    #
    lh_lookback = LOWER_HIGH_LOOKBACK
    if len(ticker) > lh_lookback:
        high_now  = float(latest["High"])
        high_mid  = float(ticker["High"].iloc[-1 - lh_lookback // 2])
        high_far  = float(ticker["High"].iloc[-1 - lh_lookback])
        if high_now < high_mid < high_far:
            reason = (
                f"Lower-high pattern: highs declining "
                f"₹{high_far:.2f} → ₹{high_mid:.2f} → ₹{high_now:.2f} "
                f"over {lh_lookback} bars (breakout failed, trend reversing)"
            )
            logger.warning(f"🚫 {tag}DISQ: {reason}")
            return True, reason

    # ── DISQUALIFIER 10: THIN SPREAD TRAP ───────────────────────────────────────
    #
    # Candle range < 0.3% of price on a breakout candle = no real conviction.
    # Thin-spread breakouts are common in illiquid or manipulated stocks where
    # a small order can push price above a level without genuine demand.
    #
    candle_range_d10 = float(latest["High"]) - float(latest["Low"])
    close_val = float(latest["Close"])
    if close_val > 0 and candle_range_d10 > 0:
        range_pct = candle_range_d10 / close_val
        if range_pct < MIN_CANDLE_RANGE_PCT:
            reason = (
                f"Thin spread: candle range {range_pct:.3%} < {MIN_CANDLE_RANGE_PCT:.1%} "
                f"of price (no conviction — possible manipulation)"
            )
            logger.warning(f"🚫 {tag}DISQ: {reason}")
            return True, reason

    return False, None


# =====================================================================================
# BONUS SCORE MODIFIERS
# =====================================================================================

def bonus_modifiers(
    ticker,
    latest,
    volume_ratio,
    symbol=None,
    timeframe="15m",
    atr_val=None,
    delivery_pct=None,
):
    """
    Returns an integer bonus (can be negative) to add to the base score.

    Bonuses:
      +3  Sustained volume (3-bar avg ≥ 1.5× 20-bar baseline)
      +3  Full MA bull stack (EMA20 > SMA50 > SMA200)
      +2  RSI accelerating (RSI now > RSI 3 bars ago + 2)
      +2  Top-of-range close (≥ 80% of bar range)
      +2  Volume climax (≥ 5× average)
      +2  Near 52W high (≥ 97% of 52W high)
      +3  ATR quality move (1.0–2.0× ATR on high volume) — EOD only
      +6  Delivery conviction EOD (same-day bhavcopy, up to +6 pts)
      +3  Delivery conviction intraday/1H (prev-day bhavcopy, up to +3 pts)

    Penalties:
      -8  Unsustained volume (fewer than 2 of last 3 bars above 80% of avg)
      -5  Gap-up chase (>8% single-bar move) — INTRADAY/1H ONLY
      -5  Extreme overbought RSI (> 78)
      -6  Extended above SMA50 (>5% above SMA50) — FIX 5: new penalty
    """

    import pandas as pd

    bonus = 0
    tag   = f"[{symbol}] " if symbol else ""

    # ── BONUS: SUSTAINED VOLUME ───────────────────────────────────────────────────────
    if len(ticker) >= 23:
        avg_20 = float(ticker["Volume"].iloc[-21:-1].mean())
        avg_3  = float(ticker["Volume"].iloc[-4:-1].mean())
        if avg_20 > 0 and (avg_3 / avg_20) >= 1.5:
            logger.debug(f"  +3 {tag}Sustained volume (3-bar avg {avg_3/avg_20:.1f}x 20-bar baseline)")
            bonus += 3

    # ── BONUS: FULL MA BULL STACK ─────────────────────────────────────────────────────
    if all(c in ticker.columns for c in ["EMA20", "SMA50", "SMA200"]):
        e20  = float(latest.get("EMA20", 0) or 0)
        s50  = float(latest.get("SMA50", 0) or 0)
        s200 = float(latest.get("SMA200", 0) or 0)
        if e20 > 0 and s50 > 0 and s200 > 0 and e20 > s50 > s200:
            logger.debug(f"  +3 {tag}Full bull stack (EMA20 > SMA50 > SMA200)")
            bonus += 3

    # ── BONUS: RSI ACCELERATING ───────────────────────────────────────────────────────
    if "RSI" in ticker.columns and len(ticker) >= 4:
        rsi_now  = float(latest["RSI"])
        rsi_3ago = float(ticker["RSI"].iloc[-4])
        if rsi_now > rsi_3ago + 2:
            logger.debug(f"  +2 {tag}RSI accelerating ({rsi_3ago:.1f} → {rsi_now:.1f})")
            bonus += 2

    # ── BONUS: TOP-OF-RANGE CLOSE ─────────────────────────────────────────────────────
    candle_range = float(latest["High"]) - float(latest["Low"])
    if candle_range > 0:
        close_position = (float(latest["Close"]) - float(latest["Low"])) / candle_range
        if close_position >= 0.80:
            logger.debug(f"  +2 {tag}Top-of-range close ({close_position:.0%} of range)")
            bonus += 2

    # ── BONUS: VOLUME CLIMAX ──────────────────────────────────────────────────────────
    if volume_ratio >= 5.0:
        logger.debug(f"  +2 {tag}Volume climax ({volume_ratio:.1f}x avg)")
        bonus += 2

    # ── BONUS: NEAR 52-WEEK HIGH ──────────────────────────────────────────────────────
    if "HIGH_52W" in ticker.columns:
        high52 = float(latest.get("HIGH_52W", 0) or 0)
        if high52 > 0:
            proximity = float(latest["Close"]) / high52
            if proximity >= 0.97:
                logger.debug(f"  +2 {tag}Near 52W high ({proximity:.1%} of high ₹{high52:.2f})")
                bonus += 2

    # ── BONUS: ATR QUALITY MOVE — EOD ONLY ───────────────────────────────────────────
    if timeframe == "1d" and atr_val is not None and atr_val > 0:
        if len(ticker) >= 2:
            prev_close      = float(ticker["Close"].iloc[-2])
            candle_close    = float(latest["Close"])
            single_move_abs = abs(candle_close - prev_close)
            atr_multiple    = single_move_abs / atr_val

            if 1.0 <= atr_multiple < 2.0:
                logger.debug(
                    f"  +3 {tag}ATR quality move ({atr_multiple:.2f}× ATR — "
                    f"sustainable breakout signature)"
                )
                bonus += 3
            elif atr_multiple < 1.0:
                logger.debug(
                    f"  ○ {tag}Move below 1× ATR ({atr_multiple:.2f}×) — no ATR bonus"
                )
            else:
                logger.debug(
                    f"  ○ {tag}Move {atr_multiple:.2f}× ATR — above sweet spot, no bonus"
                )

    # ── BONUS: DELIVERY CONVICTION ────────────────────────────────────────────────────
    if delivery_pct is not None:
        if timeframe == "1d":
            delivery_bonus = 0
            for threshold, pts in DELIVERY_BONUS_TIERS:
                if delivery_pct >= threshold:
                    delivery_bonus = pts
                    break
            label = "same-day"
        else:
            delivery_bonus = 0
            for threshold, pts in DELIVERY_BONUS_TIERS:
                if delivery_pct >= threshold:
                    delivery_bonus = pts // 2
                    break
            label = "prev-day"

        if delivery_bonus > 0:
            conviction = "institutional" if delivery_pct >= 60 else "positional" if delivery_pct >= 40 else "moderate"
            logger.info(
                f"  +{delivery_bonus} {tag}Delivery conviction "
                f"({delivery_pct:.1f}% [{label}] — {conviction})"
            )
            bonus += delivery_bonus
        else:
            logger.debug(
                f"  ○ {tag}Low delivery ({delivery_pct:.1f}% < 25% [{label}]) — no bonus"
            )
    elif timeframe == "1d":
        logger.debug(f"  ○ {tag}Delivery data unavailable — bonus skipped (no penalty)")
    else:
        logger.debug(f"  ○ {tag}Prev-day delivery unavailable — bonus skipped (no penalty)")

    # ── PENALTY: UNSUSTAINED VOLUME ───────────────────────────────────────────────────
    #
    # PRACTICAL FIX: Penalty halved for intraday timeframes.
    #
    # Why: On a 15m/1H chart, it is completely normal for only 1 of the last 3 bars
    # to have elevated volume — that IS the signal bar. The 3-bar sustained volume
    # check is an EOD concept (3 consecutive days of elevated volume = conviction).
    # On intraday bars it fires constantly and wipes out otherwise strong setups.
    # Reduced to -4 for intraday so it flags but does not destroy the score.
    #
    if len(ticker) >= 4:
        avg_vol_20   = float(ticker["Volume"].iloc[-21:-1].mean())
        recent_above = sum(
            1 for i in range(-3, 0)
            if float(ticker["Volume"].iloc[i]) > avg_vol_20 * 0.80
        )
        if recent_above < 2:
            penalty = -4 if timeframe != "1d" else -8
            logger.warning(
                f"  {penalty} {tag}Unsustained volume "
                f"(only {recent_above}/3 recent bars above 80% of avg)"
            )
            bonus += penalty

    # ── PENALTY: GAP-UP CHASE — INTRADAY AND 1H ONLY ─────────────────────────────────
    if timeframe != "1d" and len(ticker) >= 2:
        prev_close = float(ticker["Close"].iloc[-2])
        if prev_close > 0:
            single_move = (float(latest["Close"]) - prev_close) / prev_close * 100
            if single_move > 8:
                logger.warning(f"  -5 {tag}Gap-up chase ({single_move:.1f}% single-bar move)")
                bonus -= 5

    # ── PENALTY: EXTREME OVERBOUGHT RSI ──────────────────────────────────────────────
    if "RSI" in ticker.columns:
        rsi_val = float(latest["RSI"])
        if rsi_val > 78:
            logger.warning(f"  -5 {tag}Extreme RSI ({rsi_val:.1f} > 78)")
            bonus -= 5

    # ── PENALTY: EXTENDED ABOVE SMA50 ────────────────────────────────────────────────
    #
    # FIX 5: NEW PENALTY — gap-from-support filter.
    #
    # Root cause of chasing: stocks with 4–6 confluence signals (Monthly + Weekly +
    # 52W + BB + Volume) have often already run 8–15% before alerting. By the time
    # we see the signal, the entry is at extension, not at support.
    #
    # This penalty applies when Close > SMA50 × 1.05 (more than 5% above SMA50).
    # At that point the stock is extended from its trend support; breakout entries
    # here have poor risk/reward — stop needs to be placed well below current price.
    #
    # The -6 pt penalty is calibrated to drop a borderline 82-pt EOD score below
    # threshold (82 - 6 = 76 < 82) while a genuinely strong stock (90+ pts) can
    # absorb the penalty and still alert.
    #
    # IMPORTANT: This is a penalty, not a hard block. A stock 6% above SMA50 that
    # has exceptional delivery conviction (+6), ATR quality (+3), and full bull stack
    # (+3) can still net-positive past the penalty. It just needs to be exceptional.
    #
    if "SMA50" in ticker.columns:
        sma50 = float(latest.get("SMA50", 0) or 0)
        close = float(latest["Close"])
        if sma50 > 0:
            pct_above_sma50 = (close - sma50) / sma50 * 100
            # PRACTICAL FIX: Extension threshold and penalty are timeframe-aware.
            # EOD: >5% above SMA50 = chasing, -6 pts (full penalty, daily SMA50 is key support)
            # 1H:  >8% above SMA50 = chasing, -3 pts (hourly trends run further from SMA50)
            # 15m: >10% above SMA50 = chasing, -3 pts (15m scalps can be far from SMA50)
            if timeframe == "1d":
                ext_threshold, ext_penalty = 5.0, -6
            elif timeframe == "1h":
                ext_threshold, ext_penalty = 8.0, -3
            else:  # 15m
                ext_threshold, ext_penalty = 10.0, -3
            if pct_above_sma50 > ext_threshold:
                logger.warning(
                    f"  {ext_penalty} {tag}Extended above SMA50 "
                    f"({pct_above_sma50:.1f}% above — chasing extension, "
                    f"SMA50=₹{sma50:.2f}, Close=₹{close:.2f})"
                )
                bonus += ext_penalty

    # ── BONUS: TIGHT BASE BEFORE BREAKOUT (+4 pts) ───────────────────────────────
    #
    # Breakouts from tight consolidation bases are 3-5x more reliable than breakouts
    # from volatile/choppy price action. If BASE_WIDTH < threshold, award bonus.
    #
    if "BASE_WIDTH" in ticker.columns:
        base_width = latest.get("BASE_WIDTH")
        if base_width is not None and not pd.isna(base_width):
            bw = float(base_width)
            if bw < BASE_TIGHTNESS_THRESHOLD:
                logger.debug(
                    f"  +4 {tag}Tight base (BASE_WIDTH {bw:.2f} "
                    f"< {BASE_TIGHTNESS_THRESHOLD} — consolidation breakout)"
                )
                bonus += 4

    # ── PENALTY: NO PRE-BREAKOUT BASE / CHOPPY APPROACH (-4 pts) ──────────────────
    #
    # If BASE_WIDTH > volatility threshold, the stock is breaking out of volatile/
    # choppy action, not a clean base. These breakouts have high failure rates.
    #
    if "BASE_WIDTH" in ticker.columns:
        base_width = latest.get("BASE_WIDTH")
        if base_width is not None and not pd.isna(base_width):
            bw = float(base_width)
            if bw > BASE_VOLATILITY_THRESHOLD:
                logger.warning(
                    f"  -4 {tag}No base / choppy (BASE_WIDTH {bw:.2f} "
                    f"> {BASE_VOLATILITY_THRESHOLD} — volatile approach)"
                )
                bonus -= 4

    # ── PENALTY: VOLUME DRY-UP ON APPROACH (-3 pts) ──────────────────────────────
    #
    # If the 3 candles BEFORE the breakout candle have avg volume < 60% of 20-bar avg,
    # nobody was interested in this level — breakout likely fails.
    #
    if len(ticker) >= 24:
        avg_vol_20     = float(ticker["Volume"].iloc[-21:-1].mean())
        approach_vol   = float(ticker["Volume"].iloc[-4:-1].mean())
        if avg_vol_20 > 0 and approach_vol < avg_vol_20 * 0.60:
            logger.warning(
                f"  -3 {tag}Volume dry-up on approach "
                f"(pre-breakout 3-bar avg {approach_vol:,.0f} "
                f"< 60% of 20-bar avg {avg_vol_20:,.0f})"
            )
            bonus -= 3

    # ── BONUS: FII BLOCK DEAL FOOTPRINT (+8 pts) ─────────────────────────────────
    # If a recognized FII bought this stock recently, it shows institutional sponsorship.
    try:
        from block_deal_detector import get_fii_buyers
        buyers = get_fii_buyers(symbol)
        if buyers:
            logger.info(f"  +8 {tag}FII Footprint Detected ({', '.join(buyers)})")
            bonus += 8
    except Exception as e:
        pass

    # ── PENALTY: BASE MATURITY (LATE STAGE BREAKOUT) ─────────────────────────────
    # Proxy to penalize Stage 3/4 breakouts without needing a pattern recognition engine.
    if timeframe == "1d" and len(ticker) >= 252 and "SMA200" in ticker.columns:
        price = float(latest["Close"])
        sma200 = float(latest.get("SMA200", 0) or 0)
        sma200_12m_ago = float(ticker["SMA200"].iloc[-252])
        week52_low = float(ticker["Low"].iloc[-252:].min())
        
        maturity_penalty = 0
        if week52_low > 0:
            pct_above_low = (price - week52_low) / week52_low * 100
            if pct_above_low > 150:
                logger.warning(f"  -10 {tag}Late Stage Base (Price >150% above 52W low)")
                maturity_penalty -= 10
                
        if sma200_12m_ago > 0:
            sma200_slope = (sma200 - sma200_12m_ago) / sma200_12m_ago * 100
            if sma200_slope < 0:
                logger.warning(f"  -15 {tag}Stage 4 Base (Declining 200 SMA YoY)")
                maturity_penalty -= 15
                
        # Proxy 3: Momentum Decay (RS decay proxy)
        # If the stock has been flat or down over the last 3 months despite being up over 12 months
        price_6m_ago = float(ticker["Close"].iloc[-126])
        if price_6m_ago > 0:
            recent_6m_ret = (price - price_6m_ago) / price_6m_ago * 100
            if recent_6m_ret < 5 and pct_above_low > 50:
                logger.warning(f"  -8 {tag}Momentum Decay (Flat/Down over last 6 months)")
                maturity_penalty -= 8
                
        bonus += maturity_penalty

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
    symbol=None,
    timeframe="15m",
    atr_val=None,
    delivery_pct=None,
    min_vol=50_000,
):
    """
    Returns an integer score from 0 to 100 (plus bonuses, capped at 100).
    Returns 0 if any hard disqualifier fires.
    """

    import pandas as pd

    score = 0
    tag   = f"[{symbol}] " if symbol else ""

    # ── STEP 1: HARD DISQUALIFIERS ───────────────────────────────────────────────────
    if ticker is not None and latest is not None:
        disq, reason = check_hard_disqualifiers(
            ticker, latest, volume_ratio, symbol, timeframe=timeframe, min_vol=min_vol
        )
        if disq:
            return 0
            
    # ── STEP 1.5: PIOTROSKI F-SCORE DISQUALIFIER & CAP ──────────────────────────────
    f_score_pts = 0
    max_score_cap = 100
    if timeframe == "1d" and symbol is not None:
        try:
            from fundamentals_cache import get_piotroski_score
            p_score = get_piotroski_score(symbol)
            if p_score >= 0 and p_score <= 3:
                logger.warning(f"🚫 {tag}DISQ: Piotroski F-Score {p_score} <= 3 (Fundamental weakness)")
                return 0
            elif p_score >= 7:
                f_score_pts = 12
                logger.debug(f"  +12 {tag}Piotroski F-Score {p_score} >= 7 bonus")
            elif p_score < 0:
                # Missing fundamental data
                max_score_cap = 75
                logger.debug(f"  ○ {tag}Missing fundamentals — capping max score at 75")
        except:
            pass

    # ── STEP 2: CATEGORY WEIGHT ──────────────────────────────────────────────────────
    # FIX: Use exact category matching with split, not substring `in` operator.
    # Multi-category stocks like "Wealth Compounder + High Momentum" get the best score.
    cats = [c.strip() for c in category.split("+")]
    category_pts = max((SCORE_CATEGORY.get(c, 0) for c in cats), default=0)

    score += category_pts + f_score_pts
    logger.debug(f"  Score after category and Piotroski ({category}): {score} (+{category_pts + f_score_pts})")

    # ── STEP 3: BREAKOUT SIGNALS (WEIGHTED STRENGTH) ─────────────────────────────────
    # v5: PER_SIGNAL_CAP raised to 12 — the new breakout weights (3.0× for 52W) mean
    # strong breakouts easily hit the old 8.0 cap, squashing the quality differentiation.
    PER_SIGNAL_CAP = 12.0

    if isinstance(breakout_signals, dict) and breakout_signals:
        signal_pts = min(
            sum(min(v, PER_SIGNAL_CAP) for v in breakout_signals.values()),
            24.0
        )
        # After hierarchy pruning, 52W is the ONLY windowed signal — reward it heavily
        if any("52W" in s for s in breakout_signals):
            signal_pts = min(signal_pts + 4, 24.0)
            logger.debug(f"  +4 {tag}52W breakout signal bonus")
        elif any("Monthly" in s for s in breakout_signals):
            signal_pts = min(signal_pts + 2, 24.0)
            logger.debug(f"  +2 {tag}Monthly breakout signal bonus")
        logger.debug(
            f"  {tag}Signal breakdown: "
            + ", ".join(f"{k}={min(v, PER_SIGNAL_CAP):.2f}" for k, v in breakout_signals.items())
        )
    else:
        signal_pts = min(breakout_count, 3) * 8
        if breakout_signals and any("52W" in s for s in breakout_signals):
            signal_pts += 4
            logger.debug(f"  +4 {tag}52W breakout signal bonus")
        elif breakout_signals and any("Monthly" in s for s in breakout_signals):
            signal_pts += 2
            logger.debug(f"  +2 {tag}Monthly breakout signal bonus")

    score += int(signal_pts)
    logger.debug(f"  Score after signals ({breakout_count} signals, pts={signal_pts:.1f}): {score} (+{int(signal_pts)})")

    # ── STEP 4: RSI QUALITY ──────────────────────────────────────────────────────────
    #
    # PRACTICAL FIX: RSI sweet spot is now timeframe-aware.
    #
    # Why: Intraday RSI is noisier and momentum stocks legitimately run RSI 65-75
    # during strong trends. The tight 60-70 band was penalising healthy intraday
    # momentum. EOD keeps the tight band because daily RSI is more meaningful.
    #
    # 1d  sweet spot: 60-70 (15pts) — tight, daily RSI is meaningful
    # 1h  sweet spot: 58-74 (15pts) — wider, hourly momentum runs hotter
    # 15m sweet spot: 55-75 (15pts) — widest, 15m RSI spikes are normal
    #
    if timeframe == "1d":
        if 60 <= rsi <= 70:       rsi_pts = 15
        elif 70 < rsi <= 74:      rsi_pts = 10
        elif 57 <= rsi < 60:      rsi_pts = 6
        elif 74 < rsi <= 78:      rsi_pts = 3
        elif 78 < rsi <= 82:      rsi_pts = 1
        else:                     rsi_pts = 0
    elif timeframe == "1h":
        if 58 <= rsi <= 74:       rsi_pts = 15  # wider sweet spot for hourly
        elif 74 < rsi <= 78:      rsi_pts = 10
        elif 55 <= rsi < 58:      rsi_pts = 6
        elif 78 < rsi <= 82:      rsi_pts = 3
        elif 82 < rsi <= 85:      rsi_pts = 1
        else:                     rsi_pts = 0
    else:  # 15m
        if 55 <= rsi <= 75:       rsi_pts = 15  # widest for 15m momentum
        elif 75 < rsi <= 80:      rsi_pts = 10
        elif 52 <= rsi < 55:      rsi_pts = 6
        elif 80 < rsi <= 84:      rsi_pts = 3
        elif 84 < rsi <= 87:      rsi_pts = 1
        else:                     rsi_pts = 0

    score += rsi_pts
    logger.debug(f"  Score after RSI ({rsi:.1f}, tf={timeframe}): {score} (+{rsi_pts})")

    # ── STEP 5: VOLUME QUALITY ───────────────────────────────────────────────────────
    #
    # PRACTICAL FIX: Volume scoring is now timeframe-aware.
    #
    # Why: The reduced intraday volume scores were compounding with the -8 unsustained
    # penalty to make it nearly impossible to score well intraday. On a 15m/1H bar,
    # 1.5x volume IS a meaningful spike — that is 15 minutes of 1.5x normal activity.
    # The EOD bar needs higher conviction because it covers a full day.
    #
    # EOD (1d): strict tiers — full day conviction required
    # 1H/15m:   restored closer to original tiers — single bar surge is the signal
    #
    if timeframe == "1d":
        if volume_ratio >= 4.0:   vol_pts = 20
        elif volume_ratio >= 3.0: vol_pts = 15
        elif volume_ratio >= 2.5: vol_pts = 12
        elif volume_ratio >= 2.0: vol_pts = 7
        elif volume_ratio >= 1.5: vol_pts = 3
        else:                     vol_pts = 0
    else:  # 1h and 15m — single bar volume surge is the signal
        if volume_ratio >= 4.0:   vol_pts = 20
        elif volume_ratio >= 3.0: vol_pts = 17
        elif volume_ratio >= 2.5: vol_pts = 14
        elif volume_ratio >= 2.0: vol_pts = 10
        elif volume_ratio >= 1.5: vol_pts = 6
        elif volume_ratio >= 1.2: vol_pts = 2
        else:                     vol_pts = 0

    score += vol_pts
    logger.debug(f"  Score after volume ({volume_ratio:.2f}x, tf={timeframe}): {score} (+{vol_pts})")

    # ── STEP 6: TREND STRENGTH ───────────────────────────────────────────────────────
    #
    # FIX 1 (partial): ADX scoring floor aligned with disqualifier floor.
    # Old: ADX ≥ 25 → +2, ADX ≥ 22 → +1
    # New: ADX ≥ 30 → +2, ADX ≥ 25 → +1
    # Rationale: if ADX < 25 is now a hard disqualifier, there is no reason to keep
    # a scoring bonus at the same level — it would never fire anyway. Align the bonus
    # floor at the disqualifier ceiling (25) and award the larger +2 bonus only for
    # genuinely strong trends (ADX ≥ 30). This differentiates strong-trend stocks.
    #
    if ticker is not None and latest is not None:
        trend_pts = 0

        e20 = float(latest.get("EMA20", 0) or 0)
        s50 = float(latest.get("SMA50", 0) or 0)
        if e20 > 0 and s50 > 0 and e20 > s50:
            trend_pts += 3
            logger.debug(f"  +3 {tag}EMA20 > SMA50 (bull alignment)")

        s200 = float(latest.get("SMA200", 0) or 0)
        if s50 > 0 and s200 > 0 and s50 > s200:
            trend_pts += 3
            logger.debug(f"  +3 {tag}SMA50 > SMA200 (golden cross)")

        if "ADX" in ticker.columns:
            adx_val = float(latest.get("ADX", 0) or 0)
            # ADX bonus floors aligned with timeframe-aware disqualifier
            # 1d: strong=30, established=25 | 1h: strong=25, est=20 | 15m: strong=22, est=18
            if timeframe == "1d":
                strong_adx, est_adx = 30, ADX_MIN_THRESHOLD
            elif timeframe == "1h":
                strong_adx, est_adx = 25, 20
            else:
                strong_adx, est_adx = 22, 18
            if adx_val >= strong_adx:
                trend_pts += 2
                logger.debug(f"  +2 {tag}ADX {adx_val:.1f} ≥ {strong_adx} (strong trend for {timeframe})")
            elif adx_val >= est_adx:
                trend_pts += 1
                logger.debug(f"  +1 {tag}ADX {adx_val:.1f} ≥ {est_adx} (established trend for {timeframe})")

        if "MACD" in ticker.columns and "MACD_SIGNAL" in ticker.columns:
            macd_val = float(latest.get("MACD", 0) or 0)
            macd_sig = float(latest.get("MACD_SIGNAL", 0) or 0)
            if macd_val > macd_sig:
                trend_pts += 2
                logger.debug(f"  +2 {tag}MACD bullish ({macd_val:.4f} > {macd_sig:.4f})")

        trend_pts = min(trend_pts, 10)
        score += trend_pts
        logger.debug(f"  Score after trend: {score} (+{trend_pts})")

    # ── STEP 7: BONUS MODIFIERS ───────────────────────────────────────────────────────
    if ticker is not None and latest is not None:
        bonuses = bonus_modifiers(
            ticker=ticker,
            latest=latest,
            volume_ratio=volume_ratio,
            symbol=symbol,
            timeframe=timeframe,
            atr_val=atr_val,
            delivery_pct=delivery_pct,
        )
        score += bonuses
        logger.debug(f"  Score after bonuses: {score} ({'+' if bonuses >= 0 else ''}{bonuses})")

    # Cap at configured max or 100
    final_score = int(score)
    if final_score > max_score_cap:
        logger.debug(f"  {tag}Score capped at {max_score_cap} (was {final_score})")
        return max_score_cap
    final_score = min(final_score, 100)
    logger.info(f"  📊 Final score: {final_score}")
    return final_score
