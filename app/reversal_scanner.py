# =====================================================================================
# app/reversal_scanner.py (SCHEDULER READY)
# DEEP DISCOUNT & MEAN REVERSION SCANNER (With Valuation Metrics)
# =====================================================================================
import pandas as pd
import logging
from zoneinfo import ZoneInfo
from datetime import datetime

from technical_indicators import apply_indicators
from telegram_engine import send_telegram_message
from message_formatter import build_message
from database import init_db, save_alert_if_new, upsert_fetch_error
from price_cache import fetch_watchlist_data
from watchlist_cache import get_watchlist
from config import WATCHLIST_PATH, CLIMAX_VOLUME_LOOKBACK, MIN_CANDLE_RANGE_PCT, MIN_STOCK_PRICE
from sl_target_helper import compute_sl_and_target
from delivery_data import fetch_previous_day_delivery


logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
CHUNK_SIZE = 10

# ── REVERSAL PARAMETERS ──────────────────────────────────────────────────────────────
MIN_DROP_FROM_52W_HIGH = 18.0
MAX_DROP_FROM_52W_HIGH = 60.0
RSI_OVERSOLD_THRESHOLD = 35
RSI_CURL_MIN           = 40
MIN_VOLUME_RATIO       = 1.5

# ── QUALITY FILTERS (high-quality stocks only) ───────────────────────────────────────
# MIN_STOCK_PRICE imported from config (₹100)
MIN_AVG_DAILY_VOLUME   = 300_000  # ~₹3Cr+ liquidity at ₹100 price
MIN_ROE                = 12.0     # return on equity threshold (%)
MIN_YOY_REVENUE_GROWTH = 8.0      # min revenue growth % (skip shrinking businesses)
MAX_DROP_BELOW_SMA200  = 25.0     # don't catch falling knives too far below SMA200
# ─────────────────────────────────────────────────────────────────────────────────────

# ── REVERSAL SCORE THRESHOLDS ────────────────────────────────────────────────────────
MIN_REVERSAL_SCORE = 72   # minimum to generate an alert (out of 100)

# =====================================================================================
# REVERSAL-SPECIFIC SCORING
#
# Unlike breakout scanners that use scoring_engine.py, reversals have different
# quality dimensions. This scores based on:
#   Volume conviction  — 25 pts max  (higher surge = more institutional interest)
#   SMA200 proximity   — 20 pts max  (closer to SMA200 = less falling-knife risk)
#   MACD momentum      — 15 pts max  (stronger MACD flip = stronger reversal)
#   RSI curl quality   — 15 pts max  (faster RSI recovery from oversold)
#   Drop sweet spot    — 10 pts max  (25-45% drop is ideal; too shallow/deep penalized)
#   Category quality   — 10 pts max  (fundamental tier from daily builder)
#   R:R quality        —  5 pts max  (reward > 2.5:1 risk-reward setups)
# =====================================================================================

_REV_CATEGORY_SCORES = {
    "Debt-Free Cash Generator": 10, "Wealth Compounder": 10, "Top Bank/NBFC": 10,
    "Long Term Compounder": 10,
    "Dividend Aristocrat": 9,
    "Capital Efficient": 9, "Efficient Lender": 9,
    "Undervalued Growth": 8, "High Momentum": 8, "Fast Growing Financial": 8,
    "Consistent Performer": 6,
    "Blue Chip Stable": 5, "Blue Chip Financial": 5,
    "Recovery Play": 3, "Financial Recovery": 3,
}

def _score_reversal(
    vol_ratio: float,
    drop_pct: float,
    current_rsi: float,
    past_10_rsi_min: float,
    macd_hist: float | None,
    pct_below_sma200: float | None,
    category: str,
    rr_ratio: float | None,
    obv_trend: int | None = None,
    delivery_pct: float | None = None,
) -> int:
    """Score a reversal setup from 0-100 based on quality dimensions."""
    score = 0

    # ── Volume conviction (25 pts) ──
    if vol_ratio >= 5.0:   score += 25
    elif vol_ratio >= 3.5: score += 20
    elif vol_ratio >= 2.5: score += 15
    elif vol_ratio >= 2.0: score += 10
    elif vol_ratio >= 1.5: score += 5

    # ── SMA200 proximity (20 pts) — closer = safer entry ──
    if pct_below_sma200 is not None:
        if pct_below_sma200 <= 3.0:    score += 20  # very close to SMA200
        elif pct_below_sma200 <= 8.0:  score += 15
        elif pct_below_sma200 <= 15.0: score += 10
        elif pct_below_sma200 <= 20.0: score += 5
        # > 20% below SMA200: no bonus (falling knife territory)
    else:
        score += 10  # no SMA200 data — give benefit of doubt

    # ── MACD momentum (15 pts) ──
    if macd_hist is not None:
        try:
            mh = float(macd_hist)
            if mh > 0.5:   score += 15   # strong bullish histogram
            elif mh > 0.2: score += 10
            elif mh > 0:   score += 5    # just turned positive
        except (TypeError, ValueError):
            pass

    # ── RSI curl quality (15 pts) — bigger recovery = stronger signal ──
    rsi_recovery = current_rsi - past_10_rsi_min
    if rsi_recovery >= 20:   score += 15   # explosive recovery from deep oversold
    elif rsi_recovery >= 12: score += 12
    elif rsi_recovery >= 8:  score += 8
    elif rsi_recovery >= 5:  score += 5

    # ── Drop sweet spot (10 pts) — 25-45% is ideal for reversals ──
    if 25.0 <= drop_pct <= 45.0:    score += 10   # sweet spot
    elif 20.0 <= drop_pct < 25.0:   score += 7    # slightly shallow
    elif 45.0 < drop_pct <= 55.0:   score += 5    # deep but recoverable
    elif 18.0 <= drop_pct < 20.0:   score += 3    # very shallow
    # > 55% or < 18%: no bonus

    # ── Category quality (10 pts) ──
    for cat_label, cat_pts in _REV_CATEGORY_SCORES.items():
        if cat_label in category:
            score += cat_pts
            break

    # ── R:R quality (5 pts) ──
    if rr_ratio is not None:
        if rr_ratio >= 3.5:   score += 5
        elif rr_ratio >= 2.5: score += 3
        elif rr_ratio >= 2.0: score += 1

    # ── OBV confirmation bonus (5 pts) — volume confirming reversal ──
    if obv_trend is not None and obv_trend == 1:
        score += 5  # OBV rising = accumulation (institutional buying into reversal)

    # ── Delivery conviction bonus (5 pts) — institutional accumulation ──
    if delivery_pct is not None:
        if delivery_pct >= 50.0:   score += 5   # strong institutional accumulation
        elif delivery_pct >= 35.0: score += 3   # moderate positional buying
        elif delivery_pct >= 25.0: score += 1   # mild conviction

    return min(score, 100)


def _run_scan():
    """Execute a single reversal scan pass. Called inside the scheduling loop."""
    init_db()

    ist_now = datetime.now(IST)
    logger.info("=" * 80)
    logger.info(f"🔄 MEAN REVERSION SCAN | {ist_now.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 80)

    prev_delivery_map = fetch_previous_day_delivery()

    try:
        watchlist = get_watchlist()
    except Exception:
        logger.error("Failed to load watchlist, skipping run.")
        return
    # Pulling 1y data to ensure we catch the 52W High correctly
    all_ticker_data = fetch_watchlist_data(watchlist, period="1y", interval="1d")

    if not all_ticker_data:
        raise Exception("YFinance returned 0 data. API might be down or rate-limited.")

    alerts_by_category = {}
    total_alerts = 0

    for _, row in watchlist.iterrows():
        symbol   = row["Stock"]
        category = row["Category"]
        try:

            if symbol not in all_ticker_data or all_ticker_data[symbol].empty:
                continue

            ticker = all_ticker_data[symbol].copy()
            if isinstance(ticker.columns, pd.MultiIndex):
                ticker.columns = ticker.columns.get_level_values(0)
            ticker = ticker.dropna(subset=["Open", "High", "Low", "Close", "Volume"])

            if len(ticker) < 100:
                continue

            ticker = apply_indicators(ticker, timeframe="1d")
            if ticker is None or ticker.empty:
                continue

            latest   = ticker.iloc[-1]
            required = ["Close", "High", "Low", "Open", "Volume", "RSI", "EMA20", "MACD", "MACD_SIGNAL", "HIGH_52W"]
            if not all(col in ticker.columns for col in required):
                continue
            if pd.isna(latest["RSI"]) or pd.isna(latest["MACD"]):
                continue

            close_price = float(latest["Close"])
            high_52w    = float(latest["HIGH_52W"])

            if high_52w <= 0:
                continue
            drop_pct = ((high_52w - close_price) / high_52w) * 100

            if drop_pct < MIN_DROP_FROM_52W_HIGH or drop_pct > MAX_DROP_FROM_52W_HIGH:
                continue

            # ── QUALITY FILTER 1: minimum price ─────────────────────────────────────
            if close_price < MIN_STOCK_PRICE:
                continue

            # ── QUALITY FILTER 2: minimum liquidity ─────────────────────────────────
            avg_vol = float(ticker["Volume"].iloc[-21:-1].mean())
            if avg_vol < MIN_AVG_DAILY_VOLUME:
                continue

            # ── QUALITY FILTER 3: not a falling knife — must be within x% of SMA200 ─
            if "SMA200" in ticker.columns and not pd.isna(latest.get("SMA200")):
                sma200 = float(latest["SMA200"])
                if sma200 > 0:
                    pct_below_sma200 = (sma200 - close_price) / sma200 * 100
                    if pct_below_sma200 > MAX_DROP_BELOW_SMA200:
                        continue

            # ── QUALITY FILTER 4: fundamentals (from watchlist columns) ─────────────
            roe     = row.get("ROE %")
            yoy_rev = row.get("YOY Revenue %")
            if roe is not None and not pd.isna(roe):
                try:
                    if float(roe) < MIN_ROE:
                        continue
                except (ValueError, TypeError):
                    pass
            if yoy_rev is not None and not pd.isna(yoy_rev):
                try:
                    if float(yoy_rev) < MIN_YOY_REVENUE_GROWTH:
                        continue
                except (ValueError, TypeError):
                    pass

            current_rsi = float(latest["RSI"])
            past_10_rsi = ticker["RSI"].iloc[-11:-1].min()

            if current_rsi < RSI_CURL_MIN or past_10_rsi > RSI_OVERSOLD_THRESHOLD:
                continue

            ema20 = float(latest["EMA20"])
            if close_price < ema20:
                continue

            vol_now = float(latest["Volume"])
            vol_avg = float(ticker["Volume"].iloc[-21:-1].mean())
            if vol_avg <= 0:
                continue

            vol_ratio = vol_now / vol_avg
            if vol_ratio < MIN_VOLUME_RATIO:
                continue

            macd     = float(latest["MACD"])
            macd_sig = float(latest["MACD_SIGNAL"])
            if macd < macd_sig or macd > 2.0:
                continue

            reversal_signals = [
                f"📉 -{drop_pct:.1f}% from 52W High",
                "📈 RSI Oversold Curl",
                "🎯 Closed above 20 EMA",
                "📊 MACD Bullish Cross"
            ]

            today_str  = ist_now.strftime("%Y-%m-%d")
            dedup_key  = f"{category}|{symbol}|{today_str}|REVERSAL"

            candle_range   = float(latest["High"]) - float(latest["Low"])
            candle_high    = float(latest["High"])
            candle_low     = float(latest["Low"])
            atr_val        = float(latest["ATR"]) if "ATR" in ticker.columns and not pd.isna(latest.get("ATR")) else None

            # ── v5: CLIMAX TOP DISQUALIFIER ───────────────────────────────────────
            # Operators push beaten-down stocks to a fake bounce high with massive
            # volume, then dump. Same climax top pattern as breakout scanners.
            lookback_ct = min(CLIMAX_VOLUME_LOOKBACK, len(ticker) - 1)
            if lookback_ct >= 5:
                latest_vol_ct = float(latest["Volume"])
                max_vol_ct    = float(ticker["Volume"].iloc[-lookback_ct - 1:-1].max())
                candle_rng_ct = candle_high - candle_low
                if candle_rng_ct > 0 and latest_vol_ct > max_vol_ct:
                    upper_wick_pct = (candle_high - close_price) / candle_rng_ct
                    close_pos_ct   = (close_price - candle_low) / candle_rng_ct
                    if upper_wick_pct > 0.25 and close_pos_ct < 0.40:
                        logger.debug(
                            f"  ⊘ {symbol} climax top on reversal candle — skipping"
                        )
                        continue

            # ── v5: THIN SPREAD TRAP ─────────────────────────────────────────────
            # Reversal candle with tiny range = no conviction, possible manipulation.
            if close_price > 0 and candle_range > 0:
                range_pct = candle_range / close_price
                if range_pct < MIN_CANDLE_RANGE_PCT:
                    logger.debug(
                        f"  ⊘ {symbol} thin spread reversal ({range_pct:.3%}) — skipping"
                    )
                    continue

            # ── Dynamic S/R and Indicator-based SL + Target (REVERSAL mode) ───────
            # Reversal scanner: targets are mean-reversion levels (EMA20, SMA50),
            # NOT overhead resistance. SL is widest buffer (anti-trap for volatile stocks).
            sl_result = compute_sl_and_target(
                entry_price=close_price,
                atr=atr_val,
                candle_range=candle_range,
                mode="REVERSAL",
                adx=latest.get("ADX"),
                rsi=current_rsi,
                macd_hist=latest.get("MACD_HIST"),
                atr_pct=latest.get("ATR_PCT"),
                swing_low=latest.get("SWING_LOW"),
                swing_high=latest.get("SWING_HIGH"),
                bb_upper=latest.get("BB_UPPER"),
                bb_lower=latest.get("BB_LOWER"),
                bb_mid=latest.get("BB_MID"),
                s1=latest.get("S1"),
                s2=latest.get("S2"),
                r1=latest.get("R1"),
                r2=latest.get("R2"),
                swing_low_raw=latest.get("SWING_LOW_RAW"),
                swing_high_raw=latest.get("SWING_HIGH_RAW"),
                candle_low=candle_low,
                vwap=latest.get("VWAP"),
                # Mean-reversion specific targets
                ema20=latest.get("EMA20"),
                sma50=latest.get("SMA50"),
            )
            suggested_stop = sl_result["stop_loss"]
            target_price   = sl_result["target_1"]

            signal_str = ", ".join(reversal_signals)

            # ── DYNAMIC REVERSAL SCORING ──────────────────────────────────────────
            pct_below_200 = None
            if "SMA200" in ticker.columns and not pd.isna(latest.get("SMA200")):
                _sma200 = float(latest["SMA200"])
                if _sma200 > 0:
                    pct_below_200 = (_sma200 - close_price) / _sma200 * 100

            # Read OBV trend for scoring bonus
            obv_trend_val = None
            if "OBV_TREND" in ticker.columns:
                try:
                    obv_trend_val = int(latest.get("OBV_TREND", 0) or 0)
                except (TypeError, ValueError):
                    obv_trend_val = 0

            delivery_pct = prev_delivery_map.get(symbol, None)

            reversal_score = _score_reversal(
                vol_ratio=vol_ratio,
                drop_pct=drop_pct,
                current_rsi=current_rsi,
                past_10_rsi_min=float(past_10_rsi),
                macd_hist=latest.get("MACD_HIST"),
                pct_below_sma200=pct_below_200,
                category=category,
                rr_ratio=sl_result.get("rr_ratio"),
                obv_trend=obv_trend_val,
                delivery_pct=delivery_pct,
            )

            if reversal_score < MIN_REVERSAL_SCORE:
                logger.debug(f"  ⊘ {symbol} reversal score {reversal_score} < {MIN_REVERSAL_SCORE} — skipping")
                continue
            # ─────────────────────────────────────────────────────────────────────

            above_ema20  = bool(close_price >= float(latest["EMA20"])) if "EMA20" in ticker.columns and not pd.isna(latest.get("EMA20")) else None
            above_sma50  = bool(close_price >= float(latest["SMA50"])) if "SMA50" in ticker.columns and not pd.isna(latest.get("SMA50")) else None
            golden_cross = bool(float(latest["SMA50"]) >= float(latest["SMA200"])) if ("SMA50" in ticker.columns and "SMA200" in ticker.columns and not pd.isna(latest.get("SMA50")) and not pd.isna(latest.get("SMA200"))) else None
            body_ratio   = round(abs(close_price - float(latest["Open"])) / candle_range * 100) if candle_range > 0 else 0

            context = {
                "technicals": {
                    "above_ema20":      above_ema20,
                    "above_sma50":      above_sma50,
                    "golden_cross":     golden_cross,
                    "body_ratio":       round(body_ratio, 2),
                    "delivery_pct":     round(delivery_pct, 1) if delivery_pct is not None else None,
                    "rsi":              round(current_rsi, 1),
                    "volume_ratio":     round(vol_ratio, 2)
                },
                "session": {
                    "open":             round(float(latest["Open"]), 2),
                    "day_high":         round(float(latest["High"]), 2),
                    "day_low":          round(float(latest["Low"]), 2)
                },
                "fundamentals": {
                    "peg":              row.get("PEG Ratio"),
                    "yoy_rev":          row.get("YOY Revenue %"),
                    "yoy_profit":       row.get("YOY Profit %"),
                    "roe":              row.get("ROE %")
                },
                "execution": {
                    "sl_method":        sl_result.get("sl_method"),
                    "t_method":         sl_result.get("t_method"),
                    "trail_note":       sl_result.get("trail_note")
                }
            }


            saved, cap_alloc, shares = save_alert_if_new(
                symbol,
                dedup_key,
                ist_now.strftime("%Y-%m-%d %H:%M:%S"),
                scanner="REVERSAL",
                category=category,
                entry_price=round(close_price, 2),
                signals=signal_str,
                score=reversal_score,
                rsi=round(current_rsi, 1),
                volume_ratio=round(vol_ratio, 2),
                stop_loss=suggested_stop,
                target_price=target_price,
                context=context,
            )
            if not saved:
                continue

            alerts_by_category.setdefault(category, []).append({
                "symbol":           symbol,
                "category":         category,
                "breakout_signals": reversal_signals,
                "price":            round(close_price, 2),
                "open":             round(float(latest["Open"]), 2),
                "day_high":         round(float(latest["High"]), 2),
                "day_low":          round(float(latest["Low"]), 2),
                "rsi":              round(current_rsi, 1),
                "volume_ratio":     round(vol_ratio, 2),
                "body_ratio":       round(abs(close_price - float(latest["Open"])) / candle_range * 100)
                                    if candle_range > 0 else 0,
                "score":            reversal_score,
                "above_ema20":      True,
                "atr_stop":         suggested_stop,
                "target_price":     target_price,
                "target_2":         sl_result.get("target_2"),
                "target_3":         sl_result.get("target_3"),
                "sl_method":        sl_result.get("sl_method"),
                "t_method":         sl_result.get("t_method"),
                "rr_ratio":         sl_result.get("rr_ratio"),
                "trail_note":       sl_result.get("trail_note"),
                "delivery_pct":     round(delivery_pct, 1) if delivery_pct is not None else None,
                "yoy_rev":          row.get("YOY Revenue %"),
                "yoy_profit":       row.get("YOY Profit %"),
                "roe":              row.get("ROE %"),
                "capital_allocated": cap_alloc,
                "shares_bought":     shares
            })
            total_alerts += 1

        except Exception as e:
            logger.exception(f'❌ Error processing {symbol}')
            upsert_fetch_error('yfinance', 'REVERSAL', symbol, '1d', 'processing_error', str(e))

    if total_alerts > 0:
        for cat in sorted(alerts_by_category.keys()):
            cat_alerts = sorted(alerts_by_category[cat], key=lambda x: x["symbol"])
            chunks = [cat_alerts[i:i + CHUNK_SIZE] for i in range(0, len(cat_alerts), CHUNK_SIZE)]

            for chunk_num, chunk in enumerate(chunks, start=1):
                msg = build_message(
                    "REVERSAL", cat, chunk, chunk_num, len(chunks),
                    ist_now.strftime("%Y-%m-%d %H:%M:%S")
                )
                send_telegram_message(msg, scan_type="REVERSAL")

    logger.info(f"✅ REVERSAL SCAN DONE | Found {total_alerts} bottoming stocks.")
    try:
        from database import upsert_scanner_health
        upsert_scanner_health(
            scanner_name="REVERSAL",
            status="OK",
            last_success=ist_now.strftime("%Y-%m-%d %H:%M:%S"),
            today_alerts=total_alerts
        )
    except Exception:
        logger.exception("❌ Failed to update scanner health for REVERSAL")
    return total_alerts


def start() -> int:
    """
    Single-shot scan. Called once by main.py at the 18:30 window.
    Returns the number of alerts generated (0 = no setups found).
    Raises on failure so main.py can send a Telegram crash alert.
    """
    init_db()
    try:
        return _run_scan()
    except Exception as e:
        logger.exception("❌ CRITICAL REVERSAL SCAN ERROR")
        try:
            from database import upsert_scanner_health
            upsert_scanner_health("REVERSAL", "DOWN", error_msg=str(e))
        except Exception:
            pass
        raise
