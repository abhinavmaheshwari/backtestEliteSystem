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
#   Category weight   — 30 pts   (stock quality tier: Elite/High Growth/Mature)
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
from config import ADX_MIN_THRESHOLD

logger = logging.getLogger(__name__)

# =====================================================================================
# CATEGORY WEIGHTS
# =====================================================================================

SCORE_CATEGORY = {
    # ── Non-financial (PATH A) ────────────────────────────────────────────────────
    "Elite Compounder":     30,
    "Diamond Hold":         22,   # FIX: was missing; 5Y+ compounder quality ≈ High Growth tier
    "High Growth":          22,
    "Steady Compounder":    18,
    "Mature Quality":       14,
    "Turnaround":            8,

    # ── Financial (PATH B) — set equal to non-financial analogues ─────────────────
    "Financial Compounder":     30,
    "Financial High Growth":    22,
    "Financial Mature Quality": 14,
    "Financial Turnaround":      8,
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
    # FIX 1: Threshold raised from 22 → ADX_MIN_THRESHOLD (25, from config.py).
    #
    # ADX 22–24 is a "weak/establishing" trend. These stocks were generating alerts
    # that reversed quickly because there was no committed directional movement
    # behind the breakout. ADX ≥ 25 is the standard minimum for a trend worth trading.
    #
    if "ADX" in ticker.columns:
        adx_val = float(latest.get("ADX", 0) or 0)
        if 0 < adx_val < ADX_MIN_THRESHOLD:
            reason = (
                f"ADX {adx_val:.1f} < {ADX_MIN_THRESHOLD} "
                f"(trend too weak — ranging or establishing)"
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
    # FIX 4: Volume threshold raised from 1.8 → 2.0.
    # Consistent with the raised MIN_VOLUME_RATIO in config.py.
    # A stock breaking above BB upper on only 1.8× volume is not convincing enough.
    #
    if "BB_UPPER" in ticker.columns:
        bb_upper = float(latest.get("BB_UPPER", 0) or 0)
        if bb_upper > 0 and float(latest["Close"]) > bb_upper and volume_ratio < 2.0:
            reason = (
                f"Price above BB upper (₹{float(latest['Close']):.2f} > ₹{bb_upper:.2f}) "
                f"with weak volume ({volume_ratio:.1f}x < 2.0x) — overextension risk"
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
    if len(ticker) >= 4:
        avg_vol_20   = float(ticker["Volume"].iloc[-21:-1].mean())
        recent_above = sum(
            1 for i in range(-3, 0)
            if float(ticker["Volume"].iloc[i]) > avg_vol_20 * 0.80
        )
        if recent_above < 2:
            logger.warning(
                f"  -8 {tag}Unsustained volume "
                f"(only {recent_above}/3 recent bars above 80% of avg)"
            )
            bonus -= 8

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
            if pct_above_sma50 > 5.0:
                logger.warning(
                    f"  -6 {tag}Extended above SMA50 "
                    f"({pct_above_sma50:.1f}% above — chasing extension, "
                    f"SMA50=₹{sma50:.2f}, Close=₹{close:.2f})"
                )
                bonus -= 6

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

    # ── STEP 2: CATEGORY WEIGHT ──────────────────────────────────────────────────────
    category_pts = 0
    for label, pts in SCORE_CATEGORY.items():
        if label in category:
            category_pts = pts
            break

    score += category_pts
    logger.debug(f"  Score after category ({category}): {score} (+{category_pts})")

    # ── STEP 3: BREAKOUT SIGNALS (WEIGHTED STRENGTH) ─────────────────────────────────
    PER_SIGNAL_CAP = 8.0

    if isinstance(breakout_signals, dict) and breakout_signals:
        signal_pts = min(
            sum(min(v, PER_SIGNAL_CAP) for v in breakout_signals.values()),
            24.0
        )
        if any("52W" in s for s in breakout_signals):
            signal_pts = min(signal_pts + 1, 24.0)
            logger.debug(f"  +1 {tag}52W breakout signal bonus")
        logger.debug(
            f"  {tag}Signal breakdown: "
            + ", ".join(f"{k}={min(v, PER_SIGNAL_CAP):.2f}" for k, v in breakout_signals.items())
        )
    else:
        signal_pts = min(breakout_count, 3) * 8
        if breakout_signals and any("52W" in s for s in breakout_signals):
            signal_pts += 1
            logger.debug(f"  +1 {tag}52W breakout signal bonus")

    score += int(signal_pts)
    logger.debug(f"  Score after signals ({breakout_count} signals, pts={signal_pts:.1f}): {score} (+{int(signal_pts)})")

    # ── STEP 4: RSI QUALITY ──────────────────────────────────────────────────────────
    #
    # FIX 2: Sweet spot tightened from 58–72 → 60–70.
    # Tapering bands adjusted accordingly to avoid cliff edges.
    #
    # Old bands:  58–72 (15), 72–75 (10), 55–57 (6), 75–78 (3), 78–82 (1)
    # New bands:  60–70 (15), 70–74 (10), 57–59 (6), 74–78 (3), 78–82 (1)
    #
    if 60 <= rsi <= 70:
        rsi_pts = 15   # sweet spot: healthy momentum, not overbought
    elif 70 < rsi <= 74:
        rsi_pts = 10   # approaching upper edge — still acceptable
    elif 57 <= rsi < 60:
        rsi_pts = 6    # building momentum — partial credit
    elif 74 < rsi <= 78:
        rsi_pts = 3    # getting extended — small partial credit
    elif 78 < rsi <= 82:
        rsi_pts = 1    # near overbought — token credit before -5 penalty fires
    else:
        rsi_pts = 0    # RSI < 57 or > 82 — no points

    score += rsi_pts
    logger.debug(f"  Score after RSI ({rsi:.1f}): {score} (+{rsi_pts})")

    # ── STEP 5: VOLUME QUALITY ───────────────────────────────────────────────────────
    #
    # FIX 3: Low-end volume tiers reduced to remove easy point accumulation.
    # 1.2× entry removed (was 2 pts) — 1.2× daily volume is not real conviction.
    # 1.5× reduced from 5 → 3 pts.
    # Upper tiers (2.5×, 3×) modestly reduced to maintain relative differentiation.
    # 4× and above unchanged — climax volume still earns full points.
    #
    if volume_ratio >= 4.0:
        vol_pts = 20   # unchanged — climax volume is the gold standard
    elif volume_ratio >= 3.0:
        vol_pts = 15   # reduced from 17 — still strong, slight tightening
    elif volume_ratio >= 2.5:
        vol_pts = 12   # reduced from 14
    elif volume_ratio >= 2.0:
        vol_pts = 7    # reduced from 10 — meaningful but not exceptional
    elif volume_ratio >= 1.5:
        vol_pts = 3    # reduced from 5 — entry-level, barely above average
    else:
        vol_pts = 0    # < 1.5× → no volume quality points (removed 1.2× tier)

    score += vol_pts
    logger.debug(f"  Score after volume ({volume_ratio:.2f}x): {score} (+{vol_pts})")

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
            if adx_val >= 30:
                trend_pts += 2
                logger.debug(f"  +2 {tag}ADX {adx_val:.1f} ≥ 30 (strong trend)")
            elif adx_val >= ADX_MIN_THRESHOLD:   # 25
                trend_pts += 1
                logger.debug(f"  +1 {tag}ADX {adx_val:.1f} ≥ {ADX_MIN_THRESHOLD} (established trend)")
            # ADX < ADX_MIN_THRESHOLD is a hard disqualifier — never reaches here

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

    final_score = max(0, min(score, 100))
    logger.info(f"  📊 Final score: {final_score}")
    return final_score
