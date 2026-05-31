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
#   Bonus modifiers   — up to +21 pts (sustained vol, bull stack, RSI accel, close pos,
#                                      climax, ATR quality, delivery conviction)
#
# HARD DISQUALIFIERS (returns 0 immediately):
#   1. Avg volume < 50K (illiquid — unreliable fills)
#   2. Volume spike on bearish close (distribution — smart money selling)
#   3. Upper wick > 40% of range (rejection candle — buyers lost control)
#   4. ADX < 22 (no directional trend — choppy market)
#   5. RSI divergence: price ↑ but RSI ↓ over lookback window (hidden weakness)
#   6. Price above BB upper with volume ratio < 1.8 (overextension without conviction)
#   7. 3 doji/narrow candles in last 4 bars (pre-breakout exhaustion)
#
# CHANGES FROM PREVIOUS VERSION:
#   + FIX GAP 1: SCORE_CATEGORY expanded to include all Financial path categories
#     produced by daily_builder.py. Previously "Financial Compounder", "Financial High
#     Growth", "Financial Mature Quality", and "Financial Turnaround" all scored 0
#     category points, putting every Financial sector stock at a ~30-point disadvantage
#     vs non-financial peers at the same quality tier.
#
#     Financial weights are set equal to their non-financial analogues:
#       "Financial High Growth"    → 22 pts  (same as "High Growth")
#       "Financial Compounder"     → 30 pts  (same as "Elite Compounder")
#       "Financial Mature Quality" → 14 pts  (same as "Mature Quality")
#       "Financial Turnaround"     →  8 pts  (same as "Turnaround")
#       "Turnaround"               →  8 pts  (added; was also missing from old map)
#       "Steady Compounder"        → 18 pts  (between High Growth and Mature Quality)
#
#     Matching is still first-match-wins (dict ordered highest-points-first) so a
#     stock categorised "Financial Compounder + Financial Mature Quality" earns only
#     30 pts (the Financial Compounder score) — no double-counting.
#
#   + timeframe parameter added to calculate_score() — scoring engine now knows which
#     scanner called it and adjusts behaviour accordingly
#   + EOD disqualifier #5 (RSI divergence) aligned with eod_scanner hidden divergence
#     check: uses 5-bar lookback on daily, 6-bar on intraday/1H (was always 6-bar)
#   + Gap-up chase penalty (-5) skipped for EOD timeframe — the ATR filter in
#     eod_scanner already hard-rejects exhaustion moves before scoring. Keeping the
#     flat 8% penalty on daily would double-penalise a condition already upstream-gated
#   + NEW BONUS: ATR quality bonus (+3) — move of 1.0–2.0× ATR on high volume is the
#     ideal breakout signature. Rewards sustainable breakouts over marginal ones
#   + NEW BONUS: Delivery conviction bonus (up to +6 pts EOD, +3 pts intraday/1H).
#     EOD uses same-day bhavcopy; intraday/1H use previous day's bhavcopy as a proxy
#     for overnight positional conviction. Requires delivery_pct passed from scanners.
#   + FIX: SCORE_CATEGORY now uses exact key matching instead of substring matching.
#     Previously, a category string containing multiple labels (e.g. "Elite Compounder
#     & High Growth") would have matched both keys and double-counted the points.
#     Now only the first matching key is used (highest points wins, dict is ordered).
#   + Disqualifier #8 (unsustained volume hard block) removed — was already softened
#     to -8 penalty in previous version; now lives only in bonus_modifiers as penalty
#   + All disqualifiers now log the specific value that triggered them
# =====================================================================================

import logging

logger = logging.getLogger(__name__)

# =====================================================================================
# CATEGORY WEIGHTS
#
# FIX GAP 1: Expanded from 3 keys to 10 to cover all categories daily_builder.py
# can produce (both PATH A non-financial and PATH B financial).
#
# Matching uses substring search so "Financial Compounder + Financial Mature Quality"
# hits "Financial Compounder" first (highest points) and stops — no double-counting.
# Keys are ordered highest-points-first to make first-match = best-match.
#
# Why these point values?
#   Financial Compounder = Elite Compounder (30): both represent the highest-quality
#     compounding businesses — one in manufacturing/tech, the other in banking/NBFC.
#     HDFC Bank meets the same quality bar as INFY; the scoring engine should treat them
#     equivalently. Using a lower weight would systematically undercount financial
#     sector breakouts for no fundamental reason.
#   Financial High Growth = High Growth (22): same logic — fast NII/profit growth for
#     a bank is equivalent to fast gross-profit growth for a tech company.
#   Financial Mature Quality = Mature Quality (14): large stable bank, same archetype
#     as a large stable conglomerate or blue-chip manufacturer.
#   Steady Compounder (18): positioned between High Growth and Mature Quality because
#     it implies consistent double-digit growth without the acceleration of High Growth.
#   Financial Turnaround = Turnaround (8): recovery plays — rewards real earnings
#     improvement without over-rewarding turnarounds (which carry more uncertainty).
# =====================================================================================

SCORE_CATEGORY = {
    # ── Non-financial (PATH A) ────────────────────────────────────────────────────
    "Elite Compounder":     30,   # High ROE + clean growth + low debt
    "High Growth":          22,   # YoY sales + profit > 15%
    "Steady Compounder":    18,   # YoY sales + profit > 10% with solid ROE
    "Mature Quality":       14,   # Large cap, ROE ≥ 15%, low debt
    "Turnaround":            8,   # Profit recovery ≥ 30% with improving margins

    # ── Financial (PATH B) — set equal to non-financial analogues ─────────────────
    "Financial Compounder":     30,   # Steady NII growth + ROE ≥ 15% + ROA ≥ 1%
    "Financial High Growth":    22,   # NII YoY ≥ 15% + net income YoY ≥ 15%
    "Financial Mature Quality": 14,   # Large-cap bank, ROE ≥ 15%, ROA ≥ 1%
    "Financial Turnaround":      8,   # Profit recovery ≥ 25% YoY with stable NII
}

# =====================================================================================
# RSI DIVERGENCE LOOKBACK — per timeframe
#
# EOD uses 5 bars (= 1 trading week) to match eod_scanner's hidden divergence check.
# Intraday and 1H use 6 bars — the previous default, appropriate for shorter bars
# where 5 bars is too short a window to be statistically meaningful.
# =====================================================================================

RSI_DIVERGENCE_LOOKBACK = {
    "1d":  5,
    "1h":  6,
    "15m": 6,
}

# =====================================================================================
# DELIVERY CONVICTION THRESHOLDS
#
# EOD: same-day bhavcopy — full bonus (max +6 pts)
# Intraday / 1H: previous-day bhavcopy — halved bonus (max +3 pts)
#
# These bands determine how many base points a stock earns based on its NSE
# delivery percentage. The intraday/1H bonus is halved because prior-day data
# is one session stale.
#
# Why these thresholds?
#   < 25%: The day's volume was dominated by intraday traders and F&O hedgers.
#          The volume ratio looks impressive but doesn't represent real buyers
#          accumulating stock. No bonus — the volume signal is partially misleading.
#   25–39%: Mixed participation. Some positional interest but not institutional-grade.
#           Small bonus to acknowledge the partial confirmation.
#   40–59%: Solid delivery. More than one-third of the day's volume was positional.
#           This is the threshold most experienced swing traders look for. Good bonus.
#   ≥ 60%:  High conviction delivery. Institutions and HNIs took delivery aggressively.
#           Combined with 2× volume and strong candle structure, this is as good as it
#           gets for a daily breakout setup. Maximum bonus.
# =====================================================================================

DELIVERY_BONUS_TIERS = [
    (60.0, 6),   # ≥ 60% delivery → +6 pts (institutional conviction)
    (40.0, 4),   # ≥ 40% delivery → +4 pts (solid positional interest)
    (25.0, 2),   # ≥ 25% delivery → +2 pts (moderate delivery)
    (0.0,  0),   # < 25% delivery → +0 pts (intraday churn, no bonus)
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
    min_vol      : int         — minimum 20-bar average volume threshold (timeframe-aware).
                                 Pass from config.SCAN_CONFIG[timeframe]["MIN_VOLUME_AVG"]
                                 so the daily 50K floor isn't applied to 15m bars.

    Returns
    -------
    (True, reason_string)  if the stock is disqualified
    (False, None)          if all checks pass
    """

    tag = f"[{symbol}] " if symbol else ""

    # ── DISQUALIFIER 1: ILLIQUID STOCK ──────────────────────────────────────────────
    # GAP 1 FIX: Use iloc[-21:-1] (20 bars before current) to avoid including the
    # current breakout candle in the average, which deflates the ratio.
    # GAP 2 FIX: Use caller-supplied min_vol instead of hardcoded 50K, so the same
    # function works correctly for 15m bars (where 50K/bar = ~1.25M shares/day).
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
    if "ADX" in ticker.columns:
        adx_val = float(latest.get("ADX", 0) or 0)
        if 0 < adx_val < 22:
            reason = f"ADX {adx_val:.1f} < 22 (no directional trend — ranging market)"
            logger.warning(f"🚫 {tag}DISQ: {reason}")
            return True, reason

    # ── DISQUALIFIER 5: RSI BEARISH DIVERGENCE ──────────────────────────────────────
    #
    # Lookback is timeframe-aware:
    #   EOD (1d): 5 bars = 1 trading week — matches eod_scanner's hidden divergence check.
    #             Any RSI decline while price rises = hard reject (no tolerance buffer).
    #             A stock reaching this point already passed eod_scanner Filter 9B, so
    #             this is a redundant safety net — still correct to reject.
    #   1H / 15m: 6 bars. RSI must be down ≥ 3 points — small oscillations on short
    #             bars are noise, not divergence. The buffer prevents false rejections.
    #
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

    Parameters
    ----------
    ticker       : pd.DataFrame — full OHLCV + indicator data
    latest       : pd.Series   — ticker.iloc[-1]
    volume_ratio : float       — current bar volume / 20-bar average
    symbol       : str         — for logging only
    timeframe    : str         — "1d", "1h", or "15m"
    atr_val      : float|None  — ATR(14) in price units, pre-computed by eod_scanner
                                 None for intraday/1H (ATR bonus skipped)
    delivery_pct : float|None  — NSE delivery % for the relevant session.
                                 EOD: today's bhavcopy (same-day, max +6 pts).
                                 Intraday/1H: previous day's bhavcopy (max +3 pts).
                                 None means data unavailable — bonus skipped cleanly

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
      -5  Gap-up chase (>8% single-bar move) — INTRADAY/1H ONLY (EOD uses ATR filter)
      -5  Extreme overbought RSI (> 78)
    """

    bonus = 0
    tag   = f"[{symbol}] " if symbol else ""

    # ── BONUS: SUSTAINED VOLUME ───────────────────────────────────────────────────────
    if len(ticker) >= 23:
        # GAP 1 FIX: baseline excludes current bar (iloc[-21:-1])
        avg_20 = float(ticker["Volume"].iloc[-21:-1].mean())
        avg_3  = float(ticker["Volume"].tail(3).mean())
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
    #
    # Rewards the ideal breakout signature: a move that is large enough to confirm
    # institutional buying (≥ 1.0× ATR) but not so extreme that it signals exhaustion
    # (< 2.0× ATR, which is below the 3× hard reject in eod_scanner).
    #
    # Why 1.0–2.0× ATR is the sweet spot:
    #   < 1.0× ATR: the day's move is within normal noise — not a clean breakout
    #   1.0–2.0× ATR: meaningful directional move, sustainable, room for follow-through
    #   2.0–3.0× ATR: strong move, still valid (eod_scanner allows up to 3×), but
    #                  approaching exhaustion territory — no bonus
    #   > 3.0× ATR: already blocked by eod_scanner before scoring is called
    #
    # atr_val is only passed by eod_scanner.py; intraday/1H pass None.
    #
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
    #
    # EOD (timeframe == "1d"):
    #   Uses today's bhavcopy delivery % — same-day, highest confidence signal.
    #   Rewards up to +6 pts.
    #
    # Intraday / 1H (timeframe != "1d"):
    #   Uses previous trading day's delivery % as a proxy for positional conviction.
    #   High prior-day delivery = institutions held overnight and are still positioned
    #   long — a meaningful tailwind for today's momentum setup.
    #   Bonus is halved vs EOD (max +3 pts) because prior-day data is one day stale
    #   and can't speak to what institutions are doing today specifically.
    #
    # delivery_pct is None if bhavcopy unavailable or symbol not in file → skip cleanly.
    #
    if delivery_pct is not None:
        if timeframe == "1d":
            # Same-day delivery — full bonus tiers
            delivery_bonus = 0
            for threshold, pts in DELIVERY_BONUS_TIERS:
                if delivery_pct >= threshold:
                    delivery_bonus = pts
                    break
            label = "same-day"
        else:
            # Previous-day delivery — halved tiers (max +3)
            delivery_bonus = 0
            for threshold, pts in DELIVERY_BONUS_TIERS:
                if delivery_pct >= threshold:
                    delivery_bonus = pts // 2   # 6→3, 4→2, 2→1, 0→0
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
    if len(ticker) >= 4:
        # GAP 1 FIX: baseline excludes current bar (iloc[-21:-1])
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
    #
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

    Parameters
    ----------
    category         : str           — stock category (e.g. "Elite Compounder",
                                       "Financial Compounder", "Turnaround", etc.)
    breakout_count   : int           — number of breakout signals from detect_breakouts()
    rsi              : float         — current RSI value
    volume_ratio     : float         — current bar volume / 20-bar average volume
    breakout_signals : list[str]     — signal name strings, used to check for 52W signal
    ticker           : pd.DataFrame  — full OHLCV + indicator DataFrame
    latest           : pd.Series     — ticker.iloc[-1]
    symbol           : str           — ticker symbol, used only for log messages
    timeframe        : str           — "1d", "1h", or "15m" — controls divergence lookback,
                                       ATR bonus eligibility, delivery bonus eligibility,
                                       and gap-up chase penalty applicability
    atr_val          : float|None    — ATR(14) in ₹, passed by eod_scanner only (intraday/1H pass None)
    delivery_pct     : float|None    — NSE delivery %. EOD: same-day bhavcopy. Intraday/1H: prev-day bhavcopy.
    """

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
    #
    # FIX GAP 1: Iterate in order (highest-points-first) and stop at the FIRST match.
    # This prevents a category string that contains multiple key substrings
    # (e.g. "Elite Compounder + High Growth") from double-counting.
    #
    # The expanded SCORE_CATEGORY dict now covers all 9 categories from daily_builder:
    #   PATH A: Elite Compounder, High Growth, Steady Compounder, Mature Quality, Turnaround
    #   PATH B: Financial Compounder, Financial High Growth, Financial Mature Quality,
    #           Financial Turnaround
    #
    category_pts = 0
    for label, pts in SCORE_CATEGORY.items():
        if label in category:
            category_pts = pts
            break   # first (highest-value) match wins — no double-counting

    score += category_pts
    logger.debug(f"  Score after category ({category}): {score} (+{category_pts})")

    # ── STEP 3: BREAKOUT SIGNALS (WEIGHTED STRENGTH) ─────────────────────────────────
    #
    # breakout_signals is a dict {signal_name: strength_score} from breakout_engine,
    # where each score = breakout magnitude (%) × signal weight.
    #
    # SCALE PROBLEM: windowed breakouts produce small scores (0.1–9 pts typical) but
    # Volume Surge produces 200–500+ pts (vol_surge % × 1.3 — entirely different unit).
    # A raw sum() would let a single volume spike claim all 24 pts every time, making
    # the per-signal differentiation completely meaningless.
    #
    # FIX: cap each individual signal at 8 pts before summing, then cap the total at 24.
    # This matches the old flat-counter ceiling (8 pts per signal, max 3 signals = 24)
    # while still rewarding a strong 52W breakout (e.g. 9 → capped 8) more than a weak
    # daily breakout (e.g. 0.9 → contributes 0.9). Differentiation is preserved within
    # each signal type; the volume unit mismatch can no longer blow the budget.
    #
    # Fallback to flat counter handles legacy callers passing a list or None.
    #
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
    if 58 <= rsi <= 72:
        rsi_pts = 15
    elif 72 < rsi <= 75:
        rsi_pts = 10
    elif 55 <= rsi < 58:
        rsi_pts = 6
    elif 75 < rsi <= 78:
        rsi_pts = 3
    elif 78 < rsi <= 82:
        rsi_pts = 1
    else:
        rsi_pts = 0

    score += rsi_pts
    logger.debug(f"  Score after RSI ({rsi:.1f}): {score} (+{rsi_pts})")

    # ── STEP 5: VOLUME QUALITY ───────────────────────────────────────────────────────
    if volume_ratio >= 4.0:
        vol_pts = 20
    elif volume_ratio >= 3.0:
        vol_pts = 17
    elif volume_ratio >= 2.5:
        vol_pts = 14
    elif volume_ratio >= 2.0:
        vol_pts = 10
    elif volume_ratio >= 1.5:
        vol_pts = 5
    elif volume_ratio >= 1.2:
        vol_pts = 2
    else:
        vol_pts = 0

    score += vol_pts
    logger.debug(f"  Score after volume ({volume_ratio:.2f}x): {score} (+{vol_pts})")

    # ── STEP 6: TREND STRENGTH ───────────────────────────────────────────────────────
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
            if adx_val >= 25:
                trend_pts += 2
                logger.debug(f"  +2 {tag}ADX {adx_val:.1f} ≥ 25 (strong trend)")
            elif adx_val >= 22:
                trend_pts += 1
                logger.debug(f"  +1 {tag}ADX {adx_val:.1f} ≥ 22 (established trend)")

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
