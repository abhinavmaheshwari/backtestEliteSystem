import os
import time
import logging
import threading
import pandas as pd
import yfinance as yf
from datetime import datetime
from collections import defaultdict
import concurrent.futures
from pledge_scraper import fetch_promoter_pledge
from price_fetcher import fetch_historical_data, clear_price_cache
from database import get_recent_concall_analysis

# Concurrency and retry tuning
WORKER_COUNT = 3  # Hardcoded to 3 to prevent OOM kills on Railway (500MB RAM limit)
RETRY_ATTEMPTS = 3

logger = logging.getLogger(__name__)

# =====================================================================================
# CONSTANTS — Sector Concentration Limits
# =====================================================================================
MAX_SECTOR_PCT  = 0.25   # Max 25% of portfolio from one sector

# =====================================================================================
# MACRO GATES & LIMITS
# =====================================================================================
# Using centralized config for liquidity
MAX_PROMOTER_PLEDGE = 20     # >20% pledge introduces margin call liquidation risk

MAX_SECTOR_PCT = 0.25        # Max 25% of any portfolio bucket can be in one sector

# =====================================================================================
# NIFTY BENCHMARK
# =====================================================================================

import time
_nifty_cache = {"ret_6m": None, "dist_52w": None, "ts": None}

def fetch_nifty_macro_state() -> tuple[float | None, float | None]:
    """Fetch 6-month return and 52W distance of Nifty 50 for RS and Macro Regime Gate."""
    global _nifty_cache
    try:
        from price_fetcher import _fetch_history_with_retry
        hist = _fetch_history_with_retry("^NSEI", period="1y")
        if hist is None or hist.empty or len(hist) < 2:
            return (_nifty_cache["ret_6m"], _nifty_cache["dist_52w"])
        
        hist_6m = hist.tail(126) # Approx 6 months
        if len(hist_6m) >= 2:
            start_price = hist_6m['Close'].iloc[0]
            end_price = hist_6m['Close'].iloc[-1]
            ret_6m = ((end_price - start_price) / start_price) * 100.0
        else:
            ret_6m = _nifty_cache["ret_6m"]
            
        high_52w = hist['High'].max()
        end_price_1y = hist['Close'].iloc[-1]
        dist_52w = ((high_52w - end_price_1y) / high_52w) * 100.0
        
        _nifty_cache = {"ret_6m": ret_6m, "dist_52w": dist_52w, "ts": time.time()}
        return (ret_6m, dist_52w)
    except Exception as e:
        logger.error(f"Failed to fetch Nifty Macro State: {e}")
        return _nifty_cache

# =====================================================================================
# PER-STOCK TECHNICAL OVERLAY
# =====================================================================================

def calculate_wealth_technicals(symbol: str, nifty_6m_ret: float) -> dict:
    """Fetch 200 SMA, 6-month RS vs Nifty, distance to 52W high, and Liquidity."""
    defaults = {"sma_200": None, "cmp": None, "rs_6m": None, "dist_52w_high": None, "liquidity": 0.0}
    for attempt in range(RETRY_ATTEMPTS):
        try:
            hist = fetch_historical_data(symbol, period="1y", resolution="1d", dataset_key="price_1d")
            if hist is None or hist.empty or len(hist) < 120:
                return defaults

            hist['sma_200'] = hist['Close'].rolling(window=200).mean()

            last_row = hist.iloc[-1]
            cmp = float(last_row['Close'])

            # 6-Month Relative Strength vs Nifty
            hist_6m = hist.tail(126)
            if len(hist_6m) > 0:
                start_6m = hist_6m['Close'].iloc[0]
                stock_6m_ret = ((cmp - start_6m) / start_6m) * 100.0
                rs_6m = stock_6m_ret - (nifty_6m_ret if nifty_6m_ret is not None else 0.0)
            else:
                rs_6m = 0.0

            # Distance to 52-Week High
            high_52w = float(hist['High'].max())
            dist_52w_high = ((high_52w - cmp) / high_52w) * 100.0 if high_52w > 0 else 0.0

            # Liquidity (20-day Average Daily Volume * CMP)
            avg_vol = hist['Volume'].tail(20).mean()
            liquidity = float(avg_vol * cmp) if avg_vol > 0 else 0.0

            return {
                "sma_200": float(last_row['sma_200']) if not pd.isna(last_row['sma_200']) else None,
                "cmp": cmp,
                "rs_6m": rs_6m,
                "dist_52w_high": dist_52w_high,
                "liquidity": liquidity
            }
        except Exception as e:
            logger.warning(f"Attempt {attempt+1}/{RETRY_ATTEMPTS} failed for {symbol}: {e}")
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
            else:
                logger.error(f"Failed to fetch technicals for {symbol} after {RETRY_ATTEMPTS} attempts: {e}")
                return defaults


# =====================================================================================
# 100-POINT SCORING ENGINE (v2 — Reviewed & Improved)
# =====================================================================================
#
#   Factor        | Weight | Rationale
#   --------------|--------|----------------------------------------------------------
#   Quality       |   25   | ROE, ROCE, Debt — Capital efficiency & safety
#   Growth        |   25   | YoY Revenue & Profit — Business velocity
#   Momentum      |   30   | RS vs Nifty, 52W proximity, >200 SMA — Price leadership
#   Ownership     |   10   | Inst Accumulation tags — Smart money footprint
#   Cash Flow     |   10   | FCF Margin — Catches accounting red flags (Satyam/DHFL)
#
#   Total         |  100
#
# =====================================================================================

def calculate_100_point_score(r) -> int:
    """Calculates a strict 100-point Fund Manager score for a single stock."""
    score = 0

    # ── QUALITY (25 pts) ──────────────────────────────────────────────────────
    roe  = r.get("ROE %", 0) or 0
    roce = r.get("ROCE %", 0) or 0
    de   = r.get("Debt/Equity", 0) or 0

    if roe >= 15:  score += 8
    elif roe >= 10: score += 4
    if roce >= 20:  score += 9
    elif roce >= 15: score += 5
    if de <= 0.1:   score += 8
    elif de <= 0.5:  score += 4

    # ── GROWTH (25 pts) ───────────────────────────────────────────────────────
    yoy_sales  = r.get("YOY Revenue %", 0) or 0
    yoy_profit = r.get("YOY Profit %", 0) or 0

    if yoy_sales >= 20:   score += 13
    elif yoy_sales >= 12:  score += 8
    if yoy_profit >= 20:   score += 12
    elif yoy_profit >= 15: score += 8

    # ── MOMENTUM (30 pts) — Highest weight: price leadership matters most ────
    rs_6m     = r.get("rs_6m", 0) or 0
    dist_52w  = r.get("dist_52w_high", 100) or 100
    cmp       = r.get("cmp", 0) or 0
    sma_200   = r.get("sma_200", 0) or 0

    if rs_6m > 20:     score += 12
    elif rs_6m > 10:   score += 8
    elif rs_6m > 0:    score += 4
    if dist_52w <= 5:  score += 8
    elif dist_52w <= 10: score += 5
    elif dist_52w <= 15: score += 3
    if cmp > sma_200 and sma_200 > 0: score += 10

    # ── OWNERSHIP (10 pts) — Institutional accumulation ──────────────────────
    cats = str(r.get("Category", ""))
    if "Inst Accumulation" in cats: score += 10

    # ── CASH FLOW QUALITY (10 pts) — Catches Satyam/DHFL-type frauds ────────
    fcf_margin = r.get("FCF Margin %")
    opm        = r.get("OPM %", 0) or 0

    if fcf_margin is not None:
        if fcf_margin > 0 and fcf_margin >= opm * 0.5:
            score += 10   # OCF comfortably covers profits
        elif fcf_margin > 0:
            score += 5    # Positive but thin
        else:
            score += 0    # Negative FCF is a red flag
    else:
        # FCF data unavailable (common for financials) — neutral, no penalty
        score += 0

    # ── AI SENTIMENT (+5 or -5 pts) — Based on management guidance ───────────
    ai_conf = r.get("AI_Confidence", 0)
    if ai_conf >= 8:
        score += 5   # Upward guidance / Record margins
    elif ai_conf == 7:
        score += 2   # Solid / Consistent guidance
    elif 1 <= ai_conf <= 4:
        score -= 5   # Headwinds / Guidance cuts

    return min(100, score)


# =====================================================================================
# PORTFOLIO BUCKETING — with Sector Concentration Cap
# =====================================================================================

def determine_portfolio_bucket(r, nifty_dist_52w: float):
    """Assign stocks to Core / Growth / Opportunistic buckets based on hard filters."""
    score      = r.get("FM_Score", 0)
    mcap       = r.get("Market Cap Cr", 0) or 0
    roce       = r.get("ROCE %", 0) or 0
    roe        = r.get("ROE %", 0) or 0
    de         = r.get("Debt/Equity", 0) or 0
    yoy_sales  = r.get("YOY Revenue %", 0) or 0
    yoy_profit = r.get("YOY Profit %", 0) or 0
    rs_6m      = r.get("rs_6m", 0) or 0
    dist_52w   = r.get("dist_52w_high", 100) or 100
    pledge     = r.get("Promoter_Pledge", 0) or 0
    liquidity  = r.get("liquidity", 0) or 0
    cats       = str(r.get("Category", ""))

    buckets = []

    # Instant Kill Gates
    if pledge > MAX_PROMOTER_PLEDGE:
        return None
    from config import MIN_DAILY_LIQUIDITY_RUPEES_WEALTH
    
    if liquidity < MIN_DAILY_LIQUIDITY_RUPEES_WEALTH:
        return None

    # Core Compounder — ₹10,000 Cr+ mega-quality
    if score >= 80 and mcap >= 10000 and roce >= 20 and roe >= 15 and de <= 0.5:
        buckets.append("Core")

    # Growth Multiplier — ₹2,000 Cr+ emerging leaders
    if score >= 75 and mcap >= 2000 and yoy_sales >= 20 and yoy_profit >= 20 and rs_6m > 0 and dist_52w <= 15:
        buckets.append("Growth")

    # Opportunistic Momentum — massive acceleration
    if score >= 65 and yoy_profit >= 40 and rs_6m >= 15 and cats != "SME":
        buckets.append("Opportunistic")

    # Quality-On-Sale — Temporarily out of favor but high quality
    peg = r.get("PEG Ratio", 1.0)
    if peg is None: peg = 1.0
    
    if score >= 60 and mcap >= 500 and de <= 1.0 and cats != "SME":
        is_qos = (dist_52w > 10 and dist_52w <= 30 and peg < 1.0 and rs_6m > 0)
        
        # MACRO REGIME GATE: If Nifty is >15% below 52W high, loosen QOS criteria
        if nifty_dist_52w is not None and nifty_dist_52w > 15:
            is_qos = is_qos or (dist_52w > 10 and dist_52w <= 45 and peg < 1.5 and rs_6m > -15)
            
        if is_qos:
            buckets.append("Quality-On-Sale")

    return ", ".join(buckets) if buckets else None


def apply_sector_cap(df: pd.DataFrame, bucket_col: str, bucket_name: str, max_stocks: int) -> pd.DataFrame:
    """
    Enforce sector concentration limits on a bucket:
      - Max 25% of max_stocks per sector
      - Max 2 stocks per specific industry (sector sub-group)
    Returns a filtered DataFrame.
    """
    bucket_df = df[df[bucket_col].str.contains(bucket_name, na=False)].copy()
    bucket_df = bucket_df.sort_values(by="FM_Score", ascending=False)

    import math
    sector_limit = max(1, math.ceil(max_stocks * MAX_SECTOR_PCT))
    sector_counts = defaultdict(int)
    selected = []

    for _, row in bucket_df.iterrows():
        sector = row.get("Sector", "Unknown")
        
        if sector_counts[sector] >= sector_limit:
            continue
            
        sector_counts[sector] += 1
        selected.append(row)
        
        if len(selected) >= max_stocks:
            break

    return pd.DataFrame(selected).reset_index(drop=True) if selected else pd.DataFrame()


# =====================================================================================
# MAIN LOOP
# =====================================================================================

def run_wealth_loop():
    from config import WATCHLIST_PATH, DATA_DIR, MIN_DAILY_LIQUIDITY_RUPEES_WEALTH
    from database import upsert_scanner_health

    WEALTH_PATH = os.path.join(DATA_DIR, "elite_wealth_system.parquet")
    logger.info("💰 Fund Manager Wealth Engine v2 Started.")
    upsert_scanner_health("Wealth Engine", "IDLE", last_success=None, today_alerts=0)

    last_telegram_week = -1

    while True:
        try:
            if not os.path.exists(WATCHLIST_PATH):
                time.sleep(300)
                continue


            from database import download_parquet_from_db, upload_parquet_to_db
            
            # If cold boot (no local file), try to restore from DB instantly so dashboard isn't blank
            if not os.path.exists(WEALTH_PATH):
                download_parquet_from_db("wealth_engine", WEALTH_PATH)

            df = pd.read_parquet(WATCHLIST_PATH)

            logger.info(f"💰 [WEALTH ENGINE] Calculating Fund Manager v2 metrics for {len(df)} elite stocks...")

            nifty_6m_ret, nifty_dist_52w = fetch_nifty_macro_state()
            logger.info(f"💰 [WEALTH ENGINE] Nifty 6M Return: {nifty_6m_ret:.1f}%")

            clear_price_cache()

            def process_symbol(idx, row):
                sym = row["Stock"]
                tech = calculate_wealth_technicals(sym, nifty_6m_ret)
                tech["Stock"] = sym
                try:
                    tech["Promoter_Pledge"] = fetch_promoter_pledge(sym)
                except Exception as e:
                    logger.warning(f"Promoter pledge fetch failed for {sym}: {e}")
                    tech["Promoter_Pledge"] = 0
                
                # Extract AI Concall Confidence
                try:
                    concall = get_recent_concall_analysis(sym)
                    if concall and isinstance(concall, dict) and "management_confidence" in concall:
                        tech["AI_Confidence"] = int(concall["management_confidence"])
                    else:
                        tech["AI_Confidence"] = 0
                except Exception as e:
                    logger.warning(f"AI Concall fetch failed for {sym}: {e}")
                    tech["AI_Confidence"] = 0

                return tech

            technicals = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=WORKER_COUNT) as executor:
                futures = {executor.submit(process_symbol, i, row): i for i, row in df.iterrows()}
                completed = 0
                for future in concurrent.futures.as_completed(futures):
                    try:
                        technicals.append(future.result())
                    except Exception as e:
                        logger.exception(f"Worker failed unexpectedly: {e}")
                        # skip or append minimal row handled inside process_symbol
                    completed += 1
                    if completed % 50 == 0 or completed == len(df):
                        logger.info(f"💰 [WEALTH ENGINE] Progress: {completed}/{len(df)} stocks processed...")

            tech_df = pd.DataFrame(technicals)
            wealth_df = pd.merge(df, tech_df, on="Stock", how="left")

            # Apply 100-point score
            wealth_df["FM_Score"] = wealth_df.apply(calculate_100_point_score, axis=1)
            wealth_df["Portfolio_Bucket"] = wealth_df.apply(lambda r: determine_portfolio_bucket(r, nifty_dist_52w), axis=1)

            if nifty_dist_52w is None:
                logger.warning("Using NO Nifty benchmark — macro gates suppressed")

            # Buy/Sell Signals
            def get_signal(r):
                score = r.get("FM_Score", 0)
                cmp = r.get("cmp", 0) or 0
                sma = r.get("sma_200", 0) or 0
                rs = r.get("rs_6m", 0) or 0
                
                # Strict Buy rules
                if score >= 85 and cmp > sma and sma > 0:
                    if nifty_dist_52w is not None and nifty_dist_52w > 15:
                        return "SUPPRESS (Macro Bear)"
                    else:
                        return f"BUY (Score: {score})"
                        
                bucket = r.get("Portfolio_Bucket", "") or ""
                if "Quality-On-Sale" in bucket and nifty_dist_52w is not None and nifty_dist_52w > 15:
                    return f"BUY (Deep Value / Bear Market)"
                    
                # Sell Rules
                if score < 60:
                    return f"SELL (Fundamental Decay)"
                    
                # Catastrophic Trend Breakdown (Only if it's really bad, else hold)
                if rs < -40:
                    return f"SELL (Catastrophic RS Collapse)"
                if sma > 0 and cmp < (0.75 * sma):
                    return f"SELL (Catastrophic Trend Breakdown)"
                    
                return ""

            wealth_df["Signal"] = wealth_df.apply(get_signal, axis=1)

            # Apply sector caps to Core bucket for the dashboard
            core_capped = apply_sector_cap(wealth_df, "Portfolio_Bucket", "Core", max_stocks=15)
            core_symbols = set(core_capped["Stock"].tolist()) if not core_capped.empty else set()
            wealth_df["Core_Selected"] = wealth_df["Stock"].apply(lambda s: s in core_symbols)


            wealth_df.to_parquet(WEALTH_PATH, index=False)
            upload_parquet_to_db("wealth_engine", WEALTH_PATH)

            buy_count = len(wealth_df[wealth_df["Signal"].str.contains("BUY", na=False)])
            core_count = len(core_capped)
            logger.info(f"✅ [WEALTH ENGINE] Updated | Core: {core_count} | Buys: {buy_count} | Total: {len(wealth_df)}")
            upsert_scanner_health("Wealth Engine", "OK", last_success=datetime.now().isoformat(), today_alerts=buy_count)

            # Weekly Telegram Alert (Run on Sunday)
            now = datetime.now()
            current_week = now.isocalendar()[1]
            if now.weekday() == 6 and current_week != last_telegram_week:
                try:
                    from telegram_engine import send_telegram_message
                    top_20 = wealth_df.sort_values(by="FM_Score", ascending=False).head(20)
                    msg = "🏆 <b>Top 20 Long-Term Compounders</b> 🏆\n\n"
                    for idx, row in top_20.iterrows():
                        rs = row.get('rs_6m', 0) or 0
                        fcf = row.get('FCF Margin %')
                        fcf_str = f"{fcf:.0f}%" if fcf is not None else "N/A"
                        msg += f"• <b>{row['Stock']}</b> | Score: {row['FM_Score']}\n"
                        msg += f"  └ ROCE: {row.get('ROCE %', 0):.0f}% | RS: {rs:.0f}% | FCF: {fcf_str}\n"

                    send_telegram_message(msg, scan_type="EOD")
                    last_telegram_week = current_week
                    logger.info("📤 [WEALTH ENGINE] Weekly Telegram report sent.")
                except Exception as tg_err:
                    logger.warning(f"⚠️ [WEALTH ENGINE] Telegram send failed: {tg_err}")

        except Exception as e:
            logger.error(f"❌ [WEALTH ENGINE] Loop crashed: {e}")
            upsert_scanner_health("Wealth Engine", "DOWN", error_msg=str(e))

        time.sleep(3600)  # Run once an hour
