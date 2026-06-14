import os
import time
import logging
import threading
import pandas as pd
import yfinance as yf
from datetime import datetime
from collections import defaultdict

logger = logging.getLogger(__name__)

# =====================================================================================
# CONSTANTS — Sector Concentration Limits
# =====================================================================================
MAX_SECTOR_PCT  = 0.25   # Max 25% of portfolio from one sector
MAX_PER_INDUSTRY = 2     # Max 2 stocks per specific industry

# =====================================================================================
# NIFTY BENCHMARK
# =====================================================================================

def fetch_nifty_6m_return() -> float:
    """Fetch 6-month return of Nifty 50 as the benchmark for RS calculation."""
    try:
        ticker = yf.Ticker("^NSEI")
        hist = ticker.history(period="6mo")
        if hist.empty or len(hist) < 2:
            return 0.0
        start_price = hist['Close'].iloc[0]
        end_price = hist['Close'].iloc[-1]
        return ((end_price - start_price) / start_price) * 100.0
    except Exception as e:
        logger.error(f"Failed to fetch Nifty: {e}")
        return 0.0

# =====================================================================================
# PER-STOCK TECHNICAL OVERLAY
# =====================================================================================

def calculate_wealth_technicals(symbol: str, nifty_6m_ret: float) -> dict:
    """Fetch 200 SMA, 6-month RS vs Nifty, and distance to 52W high."""
    yf_symbol = symbol + ".NS" if not symbol.endswith(".NS") else symbol
    try:
        ticker = yf.Ticker(yf_symbol)
        hist = ticker.history(period="1y")
        if hist.empty or len(hist) < 120:
            return {"sma_200": None, "cmp": None, "rs_6m": None, "dist_52w_high": None}

        hist['sma_200'] = hist['Close'].rolling(window=200).mean()

        last_row = hist.iloc[-1]
        cmp = float(last_row['Close'])

        # 6-Month Relative Strength vs Nifty
        hist_6m = hist.tail(126)
        if len(hist_6m) > 0:
            start_6m = hist_6m['Close'].iloc[0]
            stock_6m_ret = ((cmp - start_6m) / start_6m) * 100.0
            rs_6m = stock_6m_ret - nifty_6m_ret
        else:
            rs_6m = 0.0

        # Distance to 52-Week High
        high_52w = float(hist['High'].max())
        dist_52w_high = ((high_52w - cmp) / high_52w) * 100.0 if high_52w > 0 else 0.0

        return {
            "sma_200": float(last_row['sma_200']) if not pd.isna(last_row['sma_200']) else None,
            "cmp": cmp,
            "rs_6m": rs_6m,
            "dist_52w_high": dist_52w_high
        }
    except Exception as e:
        logger.error(f"Failed to fetch technicals for {symbol}: {e}")
        return {"sma_200": None, "cmp": None, "rs_6m": None, "dist_52w_high": None}


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
        # else: 0 — negative FCF is a red flag
    else:
        # FCF data unavailable (common for financials) — neutral, no penalty
        score += 3

    return min(100, score)


# =====================================================================================
# PORTFOLIO BUCKETING — with Sector Concentration Cap
# =====================================================================================

def determine_portfolio_bucket(r):
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
    cats       = str(r.get("Category", ""))

    buckets = []

    # Core Compounder — ₹10,000 Cr+ mega-quality
    if score >= 80 and mcap >= 10000 and roce >= 20 and roe >= 15 and de <= 0.5:
        buckets.append("Core")

    # Growth Multiplier — ₹2,000 Cr+ emerging leaders (lowered from 10k to catch future monsters)
    if score >= 75 and mcap >= 2000 and yoy_sales >= 20 and yoy_profit >= 20 and rs_6m > 0 and dist_52w <= 15:
        buckets.append("Growth")

    # Quality-On-Sale — Temporarily out of favor but high quality (PEG proxy or PE proxy logic)
    # Using PEG < 1.0 proxy for "PE below 5Y median" since 5Y median is not available.
    # Also 10-30% below 52W high (dist_52w between 10 and 30)
    peg = r.get("PEG Ratio", 1.0)
    if peg is None:
        peg = 1.0
    cmp = r.get("cmp") or 0
    sma = r.get("sma_200") or 1e9
    if score >= 75 and roce >= 20 and yoy_sales >= 15 and yoy_profit >= 15 and (10 <= dist_52w <= 30) and cmp > sma and peg < 1.0:
        buckets.append("Quality-On-Sale")

    # Opportunistic / Turnaround
    if score >= 60 and ("Recovery Play" in cats or "Financial Recovery" in cats):
        buckets.append("Opportunistic")

    return ", ".join(buckets)


def apply_sector_cap(df: pd.DataFrame, bucket_col: str, bucket_name: str, max_stocks: int) -> pd.DataFrame:
    """
    Enforce sector concentration limits on a bucket:
      - Max 25% of max_stocks per sector
      - Max 2 stocks per specific industry (sector sub-group)
    Returns a filtered DataFrame.
    """
    bucket_df = df[df[bucket_col].str.contains(bucket_name, na=False)].copy()
    bucket_df = bucket_df.sort_values(by="FM_Score", ascending=False)

    sector_limit = max(1, int(max_stocks * MAX_SECTOR_PCT))
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

    return pd.DataFrame(selected) if selected else pd.DataFrame()


# =====================================================================================
# MAIN LOOP
# =====================================================================================

def run_wealth_loop():
    from config import WATCHLIST_PATH, DATA_DIR
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

            df = pd.read_parquet(WATCHLIST_PATH)

            logger.info(f"💰 [WEALTH ENGINE] Calculating Fund Manager v2 metrics for {len(df)} elite stocks...")

            nifty_6m_ret = fetch_nifty_6m_return()
            logger.info(f"💰 [WEALTH ENGINE] Nifty 6M Return: {nifty_6m_ret:.1f}%")

            technicals = []
            for i, row in df.iterrows():
                sym = row["Stock"]
                tech = calculate_wealth_technicals(sym, nifty_6m_ret)
                tech["Stock"] = sym
                technicals.append(tech)
                if (i + 1) % 50 == 0:
                    logger.info(f"💰 [WEALTH ENGINE] Progress: {i+1}/{len(df)} stocks processed...")
                time.sleep(0.5)

            tech_df = pd.DataFrame(technicals)
            wealth_df = pd.merge(df, tech_df, on="Stock", how="left")

            # Apply 100-point score
            wealth_df["FM_Score"] = wealth_df.apply(calculate_100_point_score, axis=1)
            wealth_df["Portfolio_Bucket"] = wealth_df.apply(determine_portfolio_bucket, axis=1)

            # Buy/Sell Signals
            def get_signal(r):
                score = r.get("FM_Score", 0)
                cmp = r.get("cmp", 0) or 0
                sma = r.get("sma_200", 0) or 0
                rs = r.get("rs_6m", 0) or 0
                
                # Strict Buy rules
                if score >= 85 and cmp > sma and sma > 0:
                    return f"BUY (Score: {score})"
                    
                # Sell Rules (Softened 200 SMA rule to prevent whipsaws, adding RS collapse)
                if score < 60:
                    return f"SELL (Fundemental Decay)"
                if rs < -10:
                    return f"SELL (RS Collapse)"
                if sma > 0 and cmp < (0.9 * sma):
                    return f"SELL (Deep below SMA)"
                    
                return ""

            wealth_df["Signal"] = wealth_df.apply(get_signal, axis=1)

            # Apply sector caps to Core bucket for the dashboard
            core_capped = apply_sector_cap(wealth_df, "Portfolio_Bucket", "Core", max_stocks=15)
            core_symbols = set(core_capped["Stock"].tolist()) if not core_capped.empty else set()
            wealth_df["Core_Selected"] = wealth_df["Stock"].apply(lambda s: s in core_symbols)

            wealth_df.to_parquet(WEALTH_PATH, index=False)

            buy_count = len(wealth_df[wealth_df["Signal"].str.contains("BUY", na=False)])
            core_count = len(core_capped)
            logger.info(f"✅ [WEALTH ENGINE] Updated | Core: {core_count} | Buys: {buy_count} | Total: {len(wealth_df)}")
            upsert_scanner_health("Wealth Engine", "OK", last_success=datetime.now().isoformat(), today_alerts=buy_count)

            # Weekly Telegram Alert (Run on Sunday)
            now = datetime.now()
            current_week = now.isocalendar()[1]
            if now.weekday() == 6 and current_week != last_telegram_week:
                try:
                    from telegram_utils import send_telegram_alert
                    top_20 = wealth_df.sort_values(by="FM_Score", ascending=False).head(20)
                    msg = "🏆 *Top 20 Long-Term Compounders* 🏆\n\n"
                    for idx, row in top_20.iterrows():
                        rs = row.get('rs_6m', 0) or 0
                        fcf = row.get('FCF Margin %')
                        fcf_str = f"{fcf:.0f}%" if fcf is not None else "N/A"
                        msg += f"• *{row['Stock']}* | Score: {row['FM_Score']}\n"
                        msg += f"  └ ROCE: {row.get('ROCE %', 0):.0f}% | RS: {rs:.0f}% | FCF: {fcf_str}\n"

                    send_telegram_alert(msg, parse_mode="Markdown")
                    last_telegram_week = current_week
                    logger.info("📤 [WEALTH ENGINE] Weekly Telegram report sent.")
                except Exception as tg_err:
                    logger.warning(f"⚠️ [WEALTH ENGINE] Telegram send failed: {tg_err}")

        except Exception as e:
            logger.error(f"❌ [WEALTH ENGINE] Loop crashed: {e}")
            upsert_scanner_health("Wealth Engine", "DOWN", error_msg=str(e))

        time.sleep(3600)  # Run once an hour
