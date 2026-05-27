# =====================================================================================
# app/scoring_engine.py
#
# WHAT THIS FILE DOES:
#   Calculates a composite quality score (0–100) for each candidate stock that has
#   passed the scanner's upstream filter stack. The score is used as a final gate —
#   only stocks above the scanner-specific threshold (72/75/78) generate alerts.
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
#   Bonus modifiers   — up to +14 pts (sustained vol, bull stack, RSI accel, close pos, climax)
#
# HARD DISQUALIFIERS (returns 0 immediately):
#   1. Avg volume < 50K (illiquid — unreliable fills)
#   2. Volume spike on bearish close (distribution — smart money selling)
#   3. Upper wick > 40% of range (rejection candle — buyers lost control)
#   4. ADX < 22 (no directional trend — choppy market)
#   5. RSI divergence: price ↑ but RSI ↓ 3+ points over 6 bars (hidden weakness)
#   6. Price above BB upper with volume ratio < 1.8 (overextension without conviction)
#   7. 3 doji/narrow candles in last 4 bars (pre-breakout exhaustion)
#   8. Volume not sustained: penalty of -8 points (was hard block — softened)
#
# CHANGES FROM PREVIOUS VERSION:
#   + RSI sweet spot widened: 58–72 now earns 15 pts (was 62–72 only)
#   + Volume scoring: added ≥2.5× tier for 14 pts (gap between ≥2.0× and ≥3.0×)
#   + ADX disqualifier threshold raised: <22 (was <20 — too permissive)
#   + Disqualifier #8 converted from hard block to -8 penalty
#     Rationale: a single-bar volume spike on a great daily chart shouldn't kill the
#     alert — it should score lower. The MIN_SCORE gate handles the rest.
#   + Penalty added: single-candle move >8% now triggers -5 (was -5, unchanged)
#   + Penalty added: RSI > 78 now triggers -5 (was -3 — tightened)
#   + Bonus: RSI accelerating upward (now > 3 bars ago + 2) earns +2 (unchanged)
#   + All disqualifiers now log the specific value that triggered them
# =====================================================================================

import logging

logger = logging.getLogger(__name__)

# =====================================================================================
# CATEGORY WEIGHTS
#
# These reflect the quality tier of the stock's fundamental profile.
# "Elite Compounder" stocks have the best odds of sustained multi-week momentum
# because they have strong earnings, high ROCE, and institutional ownership.
#
# Do NOT increase these values — the absolute numbers matter for threshold calibration:
# With MIN_SCORE_EOD=78, a "High Growth" stock (22 pts) can qualify if the
# technical setup is excellent. If you raise High Growth to 30, ALL stocks become
# too easy to score and the threshold loses meaning.
# =====================================================================================

SCORE_CATEGORY = {
    "Elite Compounder": 30,   # Best-in-class fundamental quality
    "High Growth":      22,   # Strong growth, some cyclicality or valuation risk
    "Mature Quality":   14,   # Stable but slow-growing — needs exceptional technicals
}

# =====================================================================================
# HARD DISQUALIFIER CHECK
#
# These catch setups that look good on price and RSI but have a hidden structural flaw.
# They fire BEFORE any points are added — a disqualified stock gets 0, not a low score.
# The disqualifier reason is logged at WARNING level for debugging.
# =====================================================================================

def check_hard_disqualifiers(ticker, latest, volume_ratio, symbol=None):
    """
    Checks for hard structural flaws that invalidate a breakout signal.

    Returns:
        (True, reason_string)  if the stock is disqualified
        (False, None)          if all checks pass

    Parameters:
        ticker       : pd.DataFrame — full OHLCV + indicator data
        latest       : pd.Series   — ticker.iloc[-1] (the bar being evaluated)
        volume_ratio : float       — current bar volume / 20-bar average
        symbol       : str         — used only for logging (optional)
    """

    tag = f"[{symbol}] " if symbol else ""

    # ── DISQUALIFIER 1: ILLIQUID STOCK ──────────────────────────────────────────────
    # Even if volume_ratio is high, if the 20-bar average is below 50K the stock
    # is fundamentally illiquid. Wide spreads make entries and exits costly.
    avg_vol_20 = float(ticker["Volume"].tail(20).mean())
    if avg_vol_20 < 50_000:
        reason = f"Avg vol {avg_vol_20:,.0f} < 50K (illiquid)"
        logger.warning(f"🚫 {tag}DISQ: {reason}")
        return True, reason

    # ── DISQUALIFIER 2: DISTRIBUTION CANDLE ─────────────────────────────────────────
    # High volume + close below candle midpoint = institutional selling into strength.
    # This is the opposite of a breakout — it's a distribution event.
    # The volume surge is actually a red flag in this context.
    candle_mid = (float(latest["High"]) + float(latest["Low"])) / 2
    if float(latest["Close"]) < candle_mid and volume_ratio >= 2.0:
        reason = f"High volume ({volume_ratio:.1f}x) on bearish close (distribution candle)"
        logger.warning(f"🚫 {tag}DISQ: {reason}")
        return True, reason

    # ── DISQUALIFIER 3: REJECTION CANDLE ────────────────────────────────────────────
    # Upper wick > 40% of the full range means sellers aggressively pushed price back
    # from the high. The close may be positive but the wick tells the real story.
    # (Note: the scanner also has a ≤25–30% wick pre-filter, so this 40% hard stop
    # is a second safety net for edge cases.)
    candle_range = float(latest["High"]) - float(latest["Low"])
    upper_wick   = float(latest["High"]) - float(latest["Close"])
    if candle_range > 0 and (upper_wick / candle_range) > 0.40:
        wick_pct = upper_wick / candle_range
        reason = f"Upper wick {wick_pct:.0%} of range > 40% (rejection candle)"
        logger.warning(f"🚫 {tag}DISQ: {reason}")
        return True, reason

    # ── DISQUALIFIER 4: NO DIRECTIONAL TREND (ADX) ──────────────────────────────────
    # ADX < 22 = the stock is ranging / chopping, not trending.
    # Breakout signals in ranging markets fail ~70% of the time.
    # Raised from 20 → 22: ADX 20–22 is borderline trending — not worth the risk.
    if "ADX" in ticker.columns:
        adx_val = float(latest.get("ADX", 0) or 0)
        if 0 < adx_val < 22:
            reason = f"ADX {adx_val:.1f} < 22 (no directional trend — ranging market)"
            logger.warning(f"🚫 {tag}DISQ: {reason}")
            return True, reason

    # ── DISQUALIFIER 5: RSI BEARISH DIVERGENCE ──────────────────────────────────────
    # Price is making a higher high but RSI is falling — a classic hidden weakness signal.
    # The divergence threshold is:
    #   - Price up ≥0.5% over 6 bars (not just noise)
    #   - RSI down ≥3 points over the same 6 bars
    # This combination reliably precedes short-term reversals.
    if "RSI" in ticker.columns and len(ticker) >= 6:
        rsi_now    = float(latest["RSI"])
        rsi_prev   = float(ticker["RSI"].iloc[-6])
        close_now  = float(latest["Close"])
        close_prev = float(ticker["Close"].iloc[-6])
        if close_now > close_prev * 1.005 and rsi_now < rsi_prev - 3:
            reason = (
                f"RSI bearish divergence: price +{(close_now/close_prev-1)*100:.1f}% "
                f"but RSI {rsi_prev:.1f} → {rsi_now:.1f} (↓{rsi_prev-rsi_now:.1f} pts)"
            )
            logger.warning(f"🚫 {tag}DISQ: {reason}")
            return True, reason

    # ── DISQUALIFIER 6: OVEREXTENSION WITHOUT VOLUME ────────────────────────────────
    # Price above the upper Bollinger Band = already stretched.
    # If this happened without strong volume confirmation, it's likely to snap back.
    # Combined with volume_ratio < 1.8, the breakout lacks institutional backing.
    if "BB_UPPER" in ticker.columns:
        bb_upper = float(latest.get("BB_UPPER", 0) or 0)
        if bb_upper > 0 and float(latest["Close"]) > bb_upper and volume_ratio < 1.8:
            reason = (
                f"Price above BB upper (₹{float(latest['Close']):.2f} > ₹{bb_upper:.2f}) "
                f"with weak volume ({volume_ratio:.1f}x < 1.8x) — overextension risk"
            )
            logger.warning(f"🚫 {tag}DISQ: {reason}")
            return True, reason

    # ── DISQUALIFIER 7: PRE-BREAKOUT EXHAUSTION (DOJI CLUSTER) ─────────────────────
    # 3 or more doji/narrow candles in the last 4 bars = the stock is running out of
    # buying energy right before the breakout bar. These setups have high failure rates.
    # A doji/narrow bar is defined as: body < 25% of range.
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

    # All hard disqualifiers passed
    return False, None


# =====================================================================================
# BONUS SCORE MODIFIERS
#
# Applied AFTER the base score components.
# Bonuses reward exceptional setups; penalties punish specific risks.
# Net bonus range: roughly -8 to +14 points.
# =====================================================================================

def bonus_modifiers(ticker, latest, volume_ratio, symbol=None):
    """
    Returns an integer bonus (can be negative) to add to the base score.

    Bonuses reward: sustained volume, full MA bull stack, RSI acceleration,
                    top-of-range close, volume climax, 52W high proximity.
    Penalties for: gap-up chases (>8% single-day move), extreme overbought RSI.
    """

    bonus = 0
    tag   = f"[{symbol}] " if symbol else ""

    # ── BONUS: SUSTAINED VOLUME ───────────────────────────────────────────────────────
    # The 3-bar average volume is at least 1.5× the 20-bar baseline.
    # This means volume has been elevated for multiple bars — not just one spike.
    # Sustained accumulation is more bullish than a single-bar surge.
    if len(ticker) >= 23:
        avg_20 = float(ticker["Volume"].tail(20).mean())
        avg_3  = float(ticker["Volume"].tail(3).mean())
        if avg_20 > 0 and (avg_3 / avg_20) >= 1.5:
            logger.info(f"  +3 {tag}Sustained volume (3-bar avg {avg_3/avg_20:.1f}x 20-bar baseline)")
            bonus += 3

    # ── BONUS: FULL MA BULL STACK ─────────────────────────────────────────────────────
    # EMA20 > SMA50 > SMA200: short > medium > long-term MA.
    # This is the ideal trend structure — all three timeframes are aligned bullish.
    # Stocks in full bull stack have the highest probability of continued momentum.
    if all(c in ticker.columns for c in ["EMA20", "SMA50", "SMA200"]):
        e20  = float(latest.get("EMA20", 0) or 0)
        s50  = float(latest.get("SMA50", 0) or 0)
        s200 = float(latest.get("SMA200", 0) or 0)
        if e20 > 0 and s50 > 0 and s200 > 0 and e20 > s50 > s200:
            logger.info(f"  +3 {tag}Full bull stack (EMA20 > SMA50 > SMA200)")
            bonus += 3

    # ── BONUS: RSI ACCELERATING ───────────────────────────────────────────────────────
    # RSI now is more than 2 points higher than RSI 3 bars ago.
    # Acceleration = momentum is building, not just stable at a high level.
    if "RSI" in ticker.columns and len(ticker) >= 4:
        rsi_now  = float(latest["RSI"])
        rsi_3ago = float(ticker["RSI"].iloc[-4])
        if rsi_now > rsi_3ago + 2:
            logger.info(f"  +2 {tag}RSI accelerating ({rsi_3ago:.1f} → {rsi_now:.1f})")
            bonus += 2

    # ── BONUS: TOP-OF-RANGE CLOSE ─────────────────────────────────────────────────────
    # Close in the top 20% of the bar's range = buyers held control into the close.
    # This is a strong sign that demand absorbed all intraday supply.
    candle_range = float(latest["High"]) - float(latest["Low"])
    if candle_range > 0:
        close_position = (float(latest["Close"]) - float(latest["Low"])) / candle_range
        if close_position >= 0.80:
            logger.info(f"  +2 {tag}Top-of-range close ({close_position:.0%} of range)")
            bonus += 2

    # ── BONUS: VOLUME CLIMAX ──────────────────────────────────────────────────────────
    # Volume ≥ 5× average = institutional stampede, not just participation.
    # These moves tend to have follow-through over the next 1–3 sessions.
    if volume_ratio >= 5.0:
        logger.info(f"  +2 {tag}Volume climax ({volume_ratio:.1f}x avg)")
        bonus += 2

    # ── BONUS: NEAR 52-WEEK HIGH ──────────────────────────────────────────────────────
    # Within 3% of the 52-week high = approaching or at all-time breakout territory.
    # No overhead supply, trapped buyers, or resistance to contend with.
    if "HIGH_52W" in ticker.columns:
        high52 = float(latest.get("HIGH_52W", 0) or 0)
        if high52 > 0:
            proximity = float(latest["Close"]) / high52
            if proximity >= 0.97:
                logger.info(f"  +2 {tag}Near 52W high ({proximity:.1%} of high ₹{high52:.2f})")
                bonus += 2

    # ── PENALTY: UNSUSTAINED VOLUME ───────────────────────────────────────────────────
    # Changed from hard disqualifier to -8 penalty.
    # Rationale: a single-bar volume spike on a great daily setup is worth alerting
    # at a lower score — the MIN_SCORE gate will filter truly weak setups.
    # Unsustained = fewer than 2 of the last 3 bars have volume above 80% of avg.
    if len(ticker) >= 4:
        avg_vol_20 = float(ticker["Volume"].tail(20).mean())
        recent_above = sum(
            1 for i in range(-3, 0)
            if float(ticker["Volume"].iloc[i]) > avg_vol_20 * 0.80
        )
        if recent_above < 2:
            logger.info(
                f"  -8 {tag}Unsustained volume "
                f"(only {recent_above}/3 recent bars above 80% of avg)"
            )
            bonus -= 8

    # ── PENALTY: GAP-UP CHASE ─────────────────────────────────────────────────────────
    # Single-candle move > 8% from previous close.
    # The move is already done — entering here is chasing with poor reward:risk.
    if len(ticker) >= 2:
        prev_close = float(ticker["Close"].iloc[-2])
        if prev_close > 0:
            single_move = (float(latest["Close"]) - prev_close) / prev_close * 100
            if single_move > 8:
                logger.info(f"  -5 {tag}Gap-up chase ({single_move:.1f}% single-day move)")
                bonus -= 5

    # ── PENALTY: EXTREME OVERBOUGHT RSI ──────────────────────────────────────────────
    # RSI > 78 on any timeframe = statistically overbought.
    # Tightened from -3 to -5 to more aggressively discourage overbought chasing.
    if "RSI" in ticker.columns:
        rsi_val = float(latest["RSI"])
        if rsi_val > 78:
            logger.info(f"  -5 {tag}Extreme RSI ({rsi_val:.1f} > 78)")
            bonus -= 5

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
):
    """
    Returns an integer score from 0 to 100 (plus bonuses, capped at 100).
    Returns 0 if any hard disqualifier fires.

    Parameters
    ----------
    category         : str           — stock category ("Elite Compounder", etc.)
    breakout_count   : int           — number of breakout signals from detect_breakouts()
    rsi              : float         — current RSI value
    volume_ratio     : float         — current bar volume / 20-bar average volume
    breakout_signals : list[str]     — signal name strings, used to check for 52W signal
    ticker           : pd.DataFrame  — full OHLCV + indicator DataFrame
    latest           : pd.Series     — ticker.iloc[-1]
    symbol           : str           — ticker symbol, used only for log messages
    """

    score = 0
    tag   = f"[{symbol}] " if symbol else ""

    # ── STEP 1: HARD DISQUALIFIERS ───────────────────────────────────────────────────
    # Run before any scoring. A single disqualifier returns 0 immediately.
    if ticker is not None and latest is not None:
        disq, reason = check_hard_disqualifiers(ticker, latest, volume_ratio, symbol)
        if disq:
            return 0   # reason already logged inside check_hard_disqualifiers()

    # ── STEP 2: CATEGORY WEIGHT ──────────────────────────────────────────────────────
    # Additive: a stock in multiple categories earns points for each.
    # (Most stocks are in one category only.)
    category_pts = 0
    for label, pts in SCORE_CATEGORY.items():
        if label in category:
            category_pts += pts

    score += category_pts
    logger.info(f"  Score after category ({category}): {score} (+{category_pts})")

    # ── STEP 3: BREAKOUT SIGNALS ─────────────────────────────────────────────────────
    # 8 points per signal, capped at 3 signals (24 pts max).
    # +1 bonus if any signal is a 52-week high breakout (no-overhead-supply setup).
    signal_pts = min(breakout_count, 3) * 8
    if breakout_signals and any("52W" in s for s in breakout_signals):
        signal_pts += 1
        logger.info(f"  +1 {tag}52W breakout signal bonus")

    score += signal_pts
    logger.info(f"  Score after signals ({breakout_count} signals): {score} (+{signal_pts})")

    # ── STEP 4: RSI QUALITY ──────────────────────────────────────────────────────────
    # Rewards RSI in the momentum sweet spot.
    # CHANGE: widened top band from 62–72 to 58–72 (full 15 pts).
    # RSI 58–62 is early momentum — the highest reward:risk entry zone.
    # It was previously penalized relative to 62–72 for no good reason.
    if 58 <= rsi <= 72:
        rsi_pts = 15   # Sweet spot: early-to-mid momentum
    elif 72 < rsi <= 75:
        rsi_pts = 10   # Upper momentum: valid but chase risk increasing
    elif 55 <= rsi < 58:
        rsi_pts = 6    # Pre-momentum: building, not confirmed
    elif 75 < rsi <= 78:
        rsi_pts = 3    # Overbought territory: low reward:risk
    elif 78 < rsi <= 82:
        rsi_pts = 1    # Very overbought: almost always near a top
    else:
        rsi_pts = 0    # Outside all bands (< 55 or > 82)

    score += rsi_pts
    logger.info(f"  Score after RSI ({rsi:.1f}): {score} (+{rsi_pts})")

    # ── STEP 5: VOLUME QUALITY ───────────────────────────────────────────────────────
    # Rewards volume surges on a sliding scale.
    # CHANGE: added ≥2.5× tier (14 pts) to fill the gap between ≥2.0× and ≥3.0×.
    # Previously the jump from 10 pts (2× vol) to 17 pts (3× vol) was too steep —
    # a 2.8× volume surge earned the same as a 2.1× surge, which was unfair.
    if volume_ratio >= 4.0:
        vol_pts = 20   # Climax volume — rare, very bullish
    elif volume_ratio >= 3.0:
        vol_pts = 17   # Strong institutional buying
    elif volume_ratio >= 2.5:
        vol_pts = 14   # NEW tier — solid institutional interest
    elif volume_ratio >= 2.0:
        vol_pts = 10   # Good volume confirmation
    elif volume_ratio >= 1.5:
        vol_pts = 5    # Moderate — acceptable for intraday, weak for daily
    elif volume_ratio >= 1.2:
        vol_pts = 2    # Minimal — barely above average
    else:
        vol_pts = 0    # Below-average volume — no confirmation

    score += vol_pts
    logger.info(f"  Score after volume ({volume_ratio:.2f}x): {score} (+{vol_pts})")

    # ── STEP 6: TREND STRENGTH ───────────────────────────────────────────────────────
    # Rewards stocks in a well-structured, confirmed uptrend.
    # Components: MA alignment (6 pts), ADX strength (2 pts), MACD (2 pts) = 10 pts max.
    if ticker is not None and latest is not None:
        trend_pts = 0

        # EMA20 > SMA50: short-term trend above medium-term (3 pts)
        e20 = float(latest.get("EMA20", 0) or 0)
        s50 = float(latest.get("SMA50", 0) or 0)
        if e20 > 0 and s50 > 0 and e20 > s50:
            trend_pts += 3
            logger.info(f"  +3 {tag}EMA20 > SMA50 (bull alignment)")

        # SMA50 > SMA200: golden cross confirmed (3 pts)
        s200 = float(latest.get("SMA200", 0) or 0)
        if s50 > 0 and s200 > 0 and s50 > s200:
            trend_pts += 3
            logger.info(f"  +3 {tag}SMA50 > SMA200 (golden cross)")

        # ADX trend strength (0–2 pts)
        # ADX ≥ 25: strong directional trend
        # ADX 22–24: established but not strong trend (just qualifies disqualifier gate)
        if "ADX" in ticker.columns:
            adx_val = float(latest.get("ADX", 0) or 0)
            if adx_val >= 25:
                trend_pts += 2
                logger.info(f"  +2 {tag}ADX {adx_val:.1f} ≥ 25 (strong trend)")
            elif adx_val >= 22:
                trend_pts += 1
                logger.info(f"  +1 {tag}ADX {adx_val:.1f} ≥ 22 (established trend)")

        # MACD above signal line (2 pts)
        if "MACD" in ticker.columns and "MACD_SIGNAL" in ticker.columns:
            macd_val = float(latest.get("MACD", 0) or 0)
            macd_sig = float(latest.get("MACD_SIGNAL", 0) or 0)
            if macd_val > macd_sig:
                trend_pts += 2
                logger.info(f"  +2 {tag}MACD bullish ({macd_val:.4f} > {macd_sig:.4f})")

        trend_pts = min(trend_pts, 10)   # Hard cap at 10
        score += trend_pts
        logger.info(f"  Score after trend: {score} (+{trend_pts})")

    # ── STEP 7: BONUS MODIFIERS ───────────────────────────────────────────────────────
    if ticker is not None and latest is not None:
        bonuses = bonus_modifiers(ticker, latest, volume_ratio, symbol)
        score   += bonuses
        logger.info(f"  Score after bonuses: {score} ({'+' if bonuses >= 0 else ''}{bonuses})")

    final_score = max(0, min(score, 100))
    logger.info(f"  📊 Final score: {final_score}")
    return final_score
