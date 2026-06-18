from __future__ import annotations
import os
import time
import logging
import threading
import pandas as pd
from typing import Optional, Tuple
# Ensure tzcache writable location before importing yfinance (robust import to support different cwd)
try:
    import app.yf_bootstrap
except Exception:
    try:
        import yf_bootstrap
    except Exception:
        pass
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

def fetch_nifty_macro_state() -> Tuple[Optional[float], Optional[float]]:
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
            ret_6m = ((end_price - start_price) / start_price) * 100.0 if start_price > 0 else 0.0
        else:
            ret_6m = _nifty_cache["ret_6m"]
            
        high_52w = hist['High'].max()
        end_price_1y = hist['Close'].iloc[-1]
        dist_52w = ((high_52w - end_price_1y) / high_52w) * 100.0
        
        _nifty_cache = {"ret_6m": ret_6m, "dist_52w": dist_52w, "ts": time.time()}
        return (ret_6m, dist_52w)
    except Exception as e:
        logger.error(f"Failed to fetch Nifty Macro State: {e}")
        return (_nifty_cache["ret_6m"], _nifty_cache["dist_52w"])

# =====================================================================================
# PER-STOCK TECHNICAL OVERLAY
# =====================================================================================

def calculate_wealth_technicals(symbol: str, nifty_6m_ret: float) -> dict:
    """Fetch MAs, 6-month RS vs Nifty, distance to 52W high, and Liquidity."""
    defaults = {"sma_200": None, "sma_50": None, "ema_20": None, "cmp": None, "rs_6m": None, "dist_52w_high": None, "liquidity": 0.0}
    for attempt in range(RETRY_ATTEMPTS):
        try:
            hist = fetch_historical_data(symbol, period="1y", resolution="1d", dataset_key="price_1d")
            if hist is None or hist.empty or len(hist) < 120:
                return defaults

            hist['sma_200'] = hist['Close'].rolling(window=200).mean()
            hist['sma_50']  = hist['Close'].rolling(window=50).mean()
            hist['ema_20']  = hist['Close'].ewm(span=20, adjust=False).mean()

            last_row = hist.iloc[-1]
            cmp = float(last_row['Close'])

            # 6-Month Relative Strength vs Nifty
            hist_6m = hist.tail(126)
            if len(hist_6m) > 0:
                start_6m = hist_6m['Close'].iloc[0]
                stock_6m_ret = ((cmp - start_6m) / start_6m) * 100.0 if start_6m > 0 else 0.0
                rs_6m = None if nifty_6m_ret is None else stock_6m_ret - nifty_6m_ret
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
                "sma_50":  float(last_row['sma_50']) if not pd.isna(last_row['sma_50']) else None,
                "ema_20":  float(last_row['ema_20']) if not pd.isna(last_row['ema_20']) else None,
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
# 100-POINT SCORING ENGINE (v3 — With Valuation & Drawdown Protection)
# =====================================================================================
#
#   Factor        | Weight | Rationale
#   --------------|--------|----------------------------------------------------------
#   Quality       |   25   | ROE, ROCE, Debt — Capital efficiency & safety
#   Growth        |   25   | YoY Revenue & Profit — Business velocity
#   Valuation     |   10   | PEG, P/E vs sector — Prevents overpaying (NEW)
#   Momentum      |   20   | RS vs Nifty, 52W proximity, >200 SMA — Price leadership (reduced from 30)
#   Ownership     |   10   | Inst Accumulation tags — Smart money footprint
#   Cash Flow     |   10   | FCF Margin — Catches accounting red flags (Satyam/DHFL)
#
#   Total         |  100
#
# =====================================================================================

def calculate_valuation_score(r, sector_stats: dict = None) -> int:
    """
    Valuation scoring module (10 pts max).
    Prevents overpaying for quality stocks.
    
    Metrics:
    - PEG Ratio (6 pts max): Ideal < 1.0 (growth justified by valuation)
    - P/E vs Sector (4 pts max): Discount to sector median = quality value
    """
    score = 0
    
    def _safe_float(val, default=0.0):
        if val is None: return default
        try:
            f = float(val)
            return default if pd.isna(f) else f
        except (ValueError, TypeError):
            return default
    
    peg = _safe_float(r.get("PEG Ratio"), None)
    pe = _safe_float(r.get("P/E Ratio"), None)
    
    # PEG scoring (6 pts max)
    if peg is not None:
        if peg < 1.0:
            score += 6  # Excellent: growth > valuation
        elif peg < 1.5:
            score += 3  # Good: growth roughly justified
        # else: 0 pts (overvalued relative to growth)
    
    # P/E vs sector scoring (4 pts max)
    if pe is not None and sector_stats:
        sector = str(r.get("Sector", "Unknown"))
        sector_median_pe = sector_stats.get(sector, {}).get("median_pe", None)
        
        if sector_median_pe is not None and sector_median_pe > 0:
            pe_discount = (sector_median_pe - pe) / sector_median_pe
            
            if pe_discount > 0.15:  # 15%+ discount to sector = value opportunity
                score += 4
            elif pe_discount > 0.05:  # 5%+ discount
                score += 2
            # else: 0 pts (trading at or above sector median)
    
    return min(10, score)


def position_size_calculator(fm_score: int, portfolio_bucket: str, portfolio_total: float = 10_000_000) -> dict:
    """
    Kelly-inspired position sizing formula.
    
    Args:
        fm_score: 0-100 score
        portfolio_bucket: "Core", "Growth", "Opportunistic", "Quality-On-Sale"
        portfolio_total: Total portfolio value (default ₹1 Cr)
    
    Returns:
        {
            "position_pct": 2.5,  # % of portfolio
            "position_amount": 250000,  # Rupees
            "kelly_adjusted": True,
            "rationale": "Score 82 Core = 2.5% allocation"
        }
    """
    base_pct = {
        "Core": 0.03,                    # 3% base
        "Growth": 0.04,                  # 4% base
        "Quality-On-Sale": 0.035,        # 3.5% base
        "Opportunistic": 0.015,          # 1.5% base (risky)
    }
    
    base = base_pct.get(portfolio_bucket, 0.025)
    
    # Kelly multiplier: Adjust by how strong the score is
    # At FM_Score=60 (minimum): 0.5x multiplier (conservative)
    # At FM_Score=100 (perfect): 1.0x multiplier (full allocation)
    kelly_multiplier = max(0.5, (fm_score - 60) / 40)
    
    raw_pct = base * kelly_multiplier
    
    # Hard cap at 5% per position (risk management)
    position_pct = min(raw_pct, 0.05)
    position_amount = position_pct * portfolio_total
    
    return {
        "position_pct": round(position_pct * 100, 2),
        "position_amount": round(position_amount, 0),
        "kelly_multiplier": round(kelly_multiplier, 2),
        "rationale": f"Score {fm_score} {portfolio_bucket} = {round(position_pct * 100, 2)}% (Kelly: {round(kelly_multiplier, 2)}x)"
    }


def calculate_100_point_score(r) -> int:
    """Calculates a strict 100-point Fund Manager score for a single stock."""
    
    def _safe_float(val, default=0.0):
        if val is None: return default
        try:
            f = float(val)
            return default if pd.isna(f) else f
        except (ValueError, TypeError):
            return default

    score = 0

    # ── QUALITY (25 pts) ──────────────────────────────────────────────────────
    roe  = _safe_float(r.get("ROE %"), 0)
    roce = _safe_float(r.get("ROCE %"), 0)
    de   = _safe_float(r.get("Debt/Equity"), 0)

    if roe >= 15:  score += 8
    elif roe >= 10: score += 4
    if roce >= 20:  score += 9
    elif roce >= 15: score += 5
    if de <= 0.1:   score += 8
    elif de <= 0.5:  score += 4

    # ── GROWTH (25 pts) ───────────────────────────────────────────────────────
    yoy_sales  = _safe_float(r.get("YOY Revenue %"), 0)
    yoy_profit = _safe_float(r.get("YOY Profit %"), 0)

    if yoy_sales >= 20:   score += 13
    elif yoy_sales >= 12:  score += 8
    if yoy_profit >= 20:   score += 12
    elif yoy_profit >= 15: score += 8

    # ── VALUATION (10 pts) — NEW: Prevents overpaying for growth (scores PEG & P/E)
    valuation_score = calculate_valuation_score(r)
    score += valuation_score
    # Prefer preserving None for unavailable fields so we do not implicitly award/penalize
    rs_6m_raw = r.get("rs_6m")
    rs_6m = None if rs_6m_raw is None else _safe_float(rs_6m_raw, 0)
    rs_rating_raw = r.get("RS_Rating")
    rs_rating = None if rs_rating_raw is None or (isinstance(rs_rating_raw, float) and pd.isna(rs_rating_raw)) else _safe_float(rs_rating_raw, 0)
    dist_52w  = _safe_float(r.get("dist_52w_high"), 100)
    cmp_price = _safe_float(r.get("cmp"), 0)
    sma_200_raw = r.get("sma_200")
    sma_200   = None if sma_200_raw is None else _safe_float(sma_200_raw, 0)

    # RS Rating buckets (only if available)
    if rs_rating is not None:
        if rs_rating > 90: score += 8   # Reduced from 12
        elif rs_rating > 80: score += 5  # Reduced from 8
        elif rs_rating > 60: score += 2  # Reduced from 4

    if dist_52w <= 5:  score += 5    # Reduced from 8
    elif dist_52w <= 10: score += 3   # Reduced from 5
    elif dist_52w <= 15: score += 2   # Reduced from 3

    # Price > SMA200 only when SMA200 is known
    if sma_200 is not None and cmp_price > sma_200 and sma_200 > 0:
        score += 5  # Reduced from 10

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

# =====================================================================================
# DAILY HOLD SCORE ENGINE (0-100)
# =====================================================================================
def calculate_hold_score(r: pd.Series) -> int:
    """
    Evaluates existing holdings based on a 100-point exit rubric.
    Score < 45 = SELL REVIEW
    
    NEW: Includes drawdown circuit breaker (hard stop at 30% loss).
    """
    score = 0
    
    # 1. DRAWDOWN CIRCUIT BREAKER (NEW - Added Phase 1)
    cmp = r.get("cmp", 0) or 0
    entry_price = r.get("entry_price", 0) or 0
    
    if entry_price > 0 and cmp > 0:
        drawdown_pct = ((entry_price - cmp) / entry_price) * 100
        
        # CATASTROPHIC STOP: >30% loss
        if drawdown_pct > 30:
            return 0  # Instant SELL signal (Hold_Score = 0 < 45)
        
        # WARNING: >20% loss  
        if drawdown_pct > 20:
            score -= 25  # Force below 45 → SELL REVIEW
    
    # 2. Technical Health (40 pts)
    ema20 = r.get("ema_20", 0) or 0
    sma50 = r.get("sma_50", 0) or 0
    sma200 = r.get("sma_200", 0) or 0
    rs_6m = r.get("rs_6m", 0) or 0
    
    if cmp > ema20 and ema20 > 0: score += 10
    if cmp > sma50 and sma50 > 0: score += 10
    if cmp > sma200 and sma200 > 0: score += 10
    if rs_6m > 0: score += 10
    
    # 3. Fundamental Integrity (30 pts)
    # Mapping Piotroski/Fundamentals to our existing FM_Score
    fm_score = r.get("FM_Score", 0) or 0
    if fm_score >= 70: score += 15
    elif fm_score >= 50: score += 5
    
    pledge = r.get("Promoter_Pledge", 0)
    if pledge is None or pledge == 0: score += 10
    
    yoy_profit = r.get("YOY Profit %", 0) or 0
    if yoy_profit > 0: score += 5
    
    # 4. Sector & Momentum Regime (15 pts)
    # Using 6-month RS Rating (Percentile)
    rs_rating = r.get("RS_Rating", 0) or 0
    if rs_rating > 80: score += 15
    elif rs_rating > 50: score += 5
    
    # 5. Portfolio Context / Alpha Adjustments (15 pts)
    ai_conf = r.get("AI_Confidence", 0) or 0
    if ai_conf >= 7: score += 15
    elif ai_conf >= 4: score += 5
    
    return min(100, max(0, score))


from datetime import date, timedelta

LTCG_THRESHOLD_DAYS = 365  # Indian LTCG: > 12 months
LTCG_BONUS_WINDOW   = 30   # Apply bonus in final 30 days before 1-year mark

def compute_tax_hold_bonus(entry_date: date, unrealized_pnl_pct: float) -> dict:
    today = date.today()
    holding_days = (today - entry_date).days
    days_to_ltcg = LTCG_THRESHOLD_DAYS - holding_days

    harvest_signal = False
    if unrealized_pnl_pct < -10 and holding_days < LTCG_THRESHOLD_DAYS:
        harvest_signal = True

    if holding_days >= LTCG_THRESHOLD_DAYS:
        return {"bonus": 0, "reason": "Already LTCG — no penalty for selling", "harvest_signal": harvest_signal}
    
    if 0 < days_to_ltcg <= LTCG_BONUS_WINDOW:
        bonus = round(10 * (days_to_ltcg / LTCG_BONUS_WINDOW), 1)
        return {
            "bonus": bonus,
            "reason": f"LTCG in {days_to_ltcg}d",
            "ltcg_date": entry_date + timedelta(days=LTCG_THRESHOLD_DAYS),
            "telegram_alert": days_to_ltcg in [30, 15, 7],
            "harvest_signal": harvest_signal
        }
    
    return {"bonus": 0, "reason": "Normal STCG zone", "harvest_signal": harvest_signal}


def run_wealth_scan():
    """Runs a single iteration of the Wealth Engine scan."""
    from config import WATCHLIST_PATH, DATA_DIR, MIN_DAILY_LIQUIDITY_RUPEES_WEALTH
    from database import upsert_scanner_health

    WEALTH_PATH = os.path.join(DATA_DIR, "elite_wealth_system.parquet")
    logger.info("💰 Fund Manager Wealth Engine v2 Started Scan.")
    upsert_scanner_health("Wealth Engine", "IDLE", last_success=None, today_alerts=0)

    try:
        if not os.path.exists(WATCHLIST_PATH):
            logger.warning("⚠️ Watchlist not found. Wealth Engine is forcing the Daily Builder to run.")
            try:
                from daily_builder import build_watchlist
                build_watchlist()
            except Exception as e:
                logger.error(f"❌ Wealth Engine failed to build watchlist: {e}")
                upsert_scanner_health("Wealth Engine", "IDLE", error_msg="Watchlist build failed")
                return


        from database import download_parquet_from_db, upload_parquet_to_db
        
        # If cold boot (no local file), try to restore from DB instantly so dashboard isn't blank
        if not os.path.exists(WEALTH_PATH):
            download_parquet_from_db("wealth_engine", WEALTH_PATH)

        prev_wealth_df = pd.DataFrame()
        if os.path.exists(WEALTH_PATH):
            try:
                prev_wealth_df = pd.read_parquet(WEALTH_PATH)
            except Exception as e:
                logger.error(f"Failed to load prev_wealth_df: {e}")

        df = pd.read_parquet(WATCHLIST_PATH)

        # INJECT ORPHANED OPEN POSITIONS: If a stock is currently held but fell out of the fundamental watchlist,
        # we MUST still evaluate it so it can trigger a SELL signal.
        try:
            from database import get_connection
            from psycopg2.extras import RealDictCursor
            with get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT DISTINCT symbol FROM wealth_buy_alert WHERE is_closed = FALSE")
                    open_symbols = [row['symbol'] for row in cur.fetchall()]
            
            missing_symbols = [sym for sym in open_symbols if sym not in df["Stock"].values]
            if missing_symbols:
                logger.info(f"Injecting {len(missing_symbols)} orphaned open positions back into evaluation pipeline...")
                missing_df = pd.DataFrame([{"Stock": sym} for sym in missing_symbols])
                df = pd.concat([df, missing_df], ignore_index=True)
        except Exception as e:
            logger.warning(f"Failed to fetch open positions for injection: {e}")

        logger.info(f"💰 [WEALTH ENGINE] Calculating Fund Manager v2 metrics for {len(df)} elite stocks...")

        nifty_6m_ret, nifty_dist_52w = fetch_nifty_macro_state()
        if nifty_6m_ret is None:
            logger.info("Nifty Macro: UNAVAILABLE — suppressing macro gates")
        else:
            logger.info(f"💰 [WEALTH ENGINE] Nifty 6M Return: {nifty_6m_ret:.1f}%")

        clear_price_cache()
        rejection_counts = {}

        def process_symbol(idx, row):
            try:
                sym = row["Stock"]
                tech = calculate_wealth_technicals(sym, nifty_6m_ret)
                
                # Fallback if Yahoo Finance fails
                if tech.get("cmp") is None and not prev_wealth_df.empty and sym in prev_wealth_df["Stock"].values:
                    prev_row = prev_wealth_df[prev_wealth_df["Stock"] == sym].iloc[0]
                    tech["cmp"] = prev_row.get("cmp")
                    tech["sma_50"] = prev_row.get("sma_50")
                    tech["sma_200"] = prev_row.get("sma_200")
                    tech["rs_6m"] = prev_row.get("rs_6m")
                    tech["dist_52w_high"] = prev_row.get("dist_52w_high")
                    tech["liquidity"] = prev_row.get("liquidity", 0.0)
                    rejection_counts["stale_data"] = rejection_counts.get("stale_data", 0) + 1
                    try:
                        from database import upsert_fetch_error
                        upsert_fetch_error('yfinance', 'WEALTH', sym, '1d', 'stale_data', 'using_yesterdays_cache')
                    except Exception:
                        pass
                    logger.warning(f"⚠️ YFinance failed for {sym}, using cached technicals from yesterday.")
                elif tech.get("cmp") is None:
                    rejection_counts["no_data"] = rejection_counts.get("no_data", 0) + 1
                    try:
                        from database import upsert_fetch_error
                        upsert_fetch_error('yfinance', 'WEALTH', sym, '1d', 'no_data', 'missing_data_no_fallback')
                    except Exception:
                        pass
                    
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
            except Exception as e:
                logger.exception(f"❌ Error processing {row['Stock']}")
                try:
                    from database import upsert_fetch_error
                    upsert_fetch_error('yfinance', 'WEALTH', row.get('Stock', 'UNKNOWN'), '1d', 'processing_error', str(e))
                except Exception as e:
                    logger.exception(f"Failed to process {row['Stock']}: {e}")
                
                rejection_counts["processing_error"] = rejection_counts.get("processing_error", 0) + 1
                return {"Stock": row.get("Stock", "UNKNOWN")}

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
        if not tech_df.empty and (tech_df.get("cmp") is None or tech_df["cmp"].isnull().all() or (tech_df["cmp"] == 0).all()):
            raise Exception("YFinance returned 0 prices. API might be down or rate-limited.")

        wealth_df = pd.merge(df, tech_df, on="Stock", how="left")

        if "rs_6m" in wealth_df.columns:
            wealth_df["RS_Rating"] = wealth_df["rs_6m"].rank(pct=True, ascending=True) * 100
        else:
            wealth_df["RS_Rating"] = 0

        # Apply 100-point score
        wealth_df["FM_Score"] = wealth_df.apply(calculate_100_point_score, axis=1)
        
        # Calculate valuation score separately for dashboard visibility
        wealth_df["Valuation_Score"] = wealth_df.apply(lambda r: calculate_valuation_score(r), axis=1)
        wealth_df["Portfolio_Bucket"] = wealth_df.apply(lambda r: determine_portfolio_bucket(r, nifty_dist_52w), axis=1)

        if nifty_dist_52w is None:
            logger.warning("Using NO Nifty benchmark — macro gates suppressed")

        # Load manual portfolio to apply tax hold bonuses
        from database import get_manual_portfolio
        try:
            portfolio = get_manual_portfolio()
            portfolio_dict = {p['symbol']: p for p in portfolio}
        except Exception as e:
            logger.warning(f"Failed to load manual portfolio: {e}")
            portfolio_dict = {}

        def apply_hold_score_with_tax(r):
            base_hold_score = calculate_hold_score(r)
            sym = r.get("Stock")
            if sym in portfolio_dict:
                p = portfolio_dict[sym]
                try:
                    from datetime import datetime
                    entry_date = datetime.strptime(p['entry_date'], "%Y-%m-%d").date()
                    cmp_price = r.get("cmp", p['entry_price']) or p['entry_price']
                    pnl_pct = ((cmp_price - p['entry_price']) / p['entry_price']) * 100 if p['entry_price'] > 0 else 0
                    tax_info = compute_tax_hold_bonus(entry_date, pnl_pct)
                    return min(100, base_hold_score + tax_info['bonus'])
                except Exception:
                    pass
            return base_hold_score

        # Apply Hold Score evaluation
        wealth_df["Hold_Score"] = wealth_df.apply(apply_hold_score_with_tax, axis=1)

        # Buy/Sell Signals
        def get_signal(r):
            score = r.get("FM_Score", 0)
            hold_score = r.get("Hold_Score", 0)
            cmp = r.get("cmp", 0) or 0
            sma = r.get("sma_200", 0) or 0
            rs = r.get("rs_6m", 0) or 0
            sym = r.get("Stock")
            
            # Check for Tax-Loss Harvesting signal
            if sym in portfolio_dict:
                p = portfolio_dict[sym]
                try:
                    from datetime import datetime
                    entry_date = datetime.strptime(p['entry_date'], "%Y-%m-%d").date()
                    cmp_price = r.get("cmp", p['entry_price']) or p['entry_price']
                    pnl_pct = ((cmp_price - p['entry_price']) / p['entry_price']) * 100 if p['entry_price'] > 0 else 0
                    tax_info = compute_tax_hold_bonus(entry_date, pnl_pct)
                    if tax_info.get("harvest_signal"):
                        # Only flag if not already a hard sell
                        if hold_score >= 45 and rs >= -40 and not (sma > 0 and cmp > 0 and cmp < (0.75 * sma)):
                            return f"HOLD (Tax-Loss Harvest Opportunity: {pnl_pct:.1f}%)"
                except Exception:
                    pass
            
            # Strict Buy rules
            if score >= 85 and cmp > sma and sma > 0:
                if nifty_dist_52w is not None and nifty_dist_52w > 15:
                    return "SUPPRESS (Macro Bear)"
                else:
                    return f"BUY (Score: {score})"
                    
            bucket = r.get("Portfolio_Bucket", "") or ""
            if "Quality-On-Sale" in bucket and nifty_dist_52w is not None and nifty_dist_52w > 15:
                return f"BUY (Deep Value / Bear Market)"
                
            # Exit Logic (The Hold Score Engine)
            if hold_score < 45:
                return f"SELL REVIEW (Hold Score: {hold_score}/100)"
                
            # Catastrophic Trend Breakdown (Only if it's really bad, else hold)
            if rs < -40:
                return f"SELL (Catastrophic RS Collapse)"
            if sma > 0 and cmp > 0 and cmp < (0.75 * sma):
                return f"SELL (Catastrophic Trend Breakdown)"
                
            return ""

        wealth_df["Signal"] = wealth_df.apply(get_signal, axis=1)
        
        # Calculate position sizing for all BUY signals
        def calculate_position_sizing(r):
            signal = r.get("Signal", "")
            if "BUY" in signal:
                fm_score = r.get("FM_Score", 60)
                bucket = r.get("Portfolio_Bucket", "Growth")
                sizing = position_size_calculator(fm_score, bucket)
                r["position_pct"] = sizing["position_pct"]
                r["position_amount"] = sizing["position_amount"]
                cmp = r.get("cmp", 0)
                r["position_shares"] = int(sizing["position_amount"] / cmp) if cmp and cmp > 0 else 0
            else:
                r["position_pct"] = None
                r["position_amount"] = None
                r["position_shares"] = None
            return r
        
        wealth_df = wealth_df.apply(calculate_position_sizing, axis=1)

        # Apply sector caps to Core bucket for the dashboard
        core_capped = apply_sector_cap(wealth_df, "Portfolio_Bucket", "Core", max_stocks=15)
        core_symbols = set(core_capped["Stock"].tolist()) if not core_capped.empty else set()
        wealth_df["Core_Selected"] = wealth_df["Stock"].apply(lambda s: s in core_symbols)

        # Save BUY signals to wealth_buy_alert table for historical tracking
        try:
            from database import save_wealth_buy_alert, close_position, update_position_real_time_prices
            buy_signals = wealth_df[wealth_df["Signal"].str.contains("BUY", na=False)]
            for _, row in buy_signals.iterrows():
                symbol = row.get("Stock")
                cmp = row.get("cmp")
                fm_score = row.get("FM_Score")
                breakout = "Strength" if row.get("dist_52w_high", 100) > 5 else "Value"
                position_pct = row.get("position_pct")
                position_amount = row.get("position_amount")
                portfolio_bucket = row.get("Portfolio_Bucket", "Unknown")
                valuation_score = row.get("Valuation_Score", 0)
                position_shares = int(position_amount / cmp) if cmp and cmp > 0 and position_amount else 0
                if symbol and cmp:
                    save_wealth_buy_alert(
                        symbol, 
                        cmp, 
                        breakout_type=breakout, 
                        fm_score=fm_score,
                        position_pct=position_pct,
                        position_amount=position_amount,
                        position_shares=position_shares,
                        portfolio_bucket=portfolio_bucket,
                        valuation_score=valuation_score
                    )
            
            # Fetch REAL-TIME prices for all open positions (for accurate P&L calculation)
            try:
                open_symbols = wealth_df["Stock"].unique().tolist()
                realtime_metrics = {}
                if open_symbols:
                    # Fetch all prices in parallel using yfinance
                    for symbol in open_symbols:
                        try:
                            ticker = yf.Ticker(f"{symbol}.NS")
                            info = ticker.info
                            current_price = info.get("currentPrice") or info.get("regularMarketPrice")
                            
                            symbol_row = wealth_df[wealth_df["Stock"] == symbol]
                            current_score = None
                            if not symbol_row.empty:
                                val = symbol_row.iloc[0].get("Hold_Score")
                                if pd.notna(val):
                                    current_score = float(val)

                            if current_price and current_price > 0:
                                realtime_metrics[symbol] = {"price": float(current_price), "score": current_score}
                        except Exception:
                            pass  # Skip symbols that fail price fetch
                
                # Update all open positions with real-time metrics
                if realtime_metrics:
                    update_position_real_time_prices(realtime_metrics)
            except Exception as e:
                logger.warning(f"⚠️  Could not fetch real-time prices: {e}")
            
            # Auto-close positions when SELL signal detected
            sell_signals = wealth_df[wealth_df["Signal"].str.contains("SELL", na=False)]
            for _, row in sell_signals.iterrows():
                symbol = row.get("Stock")
                cmp = row.get("cmp")
                signal_text = row.get("Signal")
                if symbol and cmp:
                    close_position(symbol, cmp, signal_text)
        except Exception as e:
            logger.warning(f"⚠️  Could not process buy/sell alerts: {e}")

        wealth_df.to_parquet(WEALTH_PATH, index=False)
        upload_parquet_to_db("wealth_engine", WEALTH_PATH)

        buy_count = len(wealth_df[wealth_df["Signal"].str.contains("BUY", na=False)])
        core_count = len(core_capped)
        logger.info(f"✅ [WEALTH ENGINE] Updated | Core: {core_count} | Buys: {buy_count} | Total: {len(wealth_df)}")
        
        upsert_scanner_health("Wealth Engine", "OK", last_success=datetime.now().isoformat(), today_alerts=buy_count)

        # Weekly Telegram Alert removed (2026-06-17)

    except Exception as e:
        logger.exception(f"❌ [WEALTH ENGINE] Scan crashed: {e}")
        try:
            upsert_scanner_health("Wealth Engine", "DOWN", error_msg=str(e))
        except Exception:
            pass
        raise e
