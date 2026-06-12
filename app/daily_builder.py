# =====================================================================================
# app/daily_builder.py  —  v4 (Ultimate Long-Term & Momentum Hybrid)
# SILENT EXECUTION + EMAIL DISPATCH WITH TELEGRAM FALLBACK + FORENSIC ACCOUNTING
# =====================================================================================

import os
import traceback
import threading
import pandas as pd
import logging
import requests
from datetime import datetime

from tradingview_screener import Query, col
from config import WATCHLIST_PATH

# ── SEND GUARD — persisted in Postgres so restarts never re-send ────────────────────

def _already_sent_today() -> bool:
    """Returns True if the fundamental watchlist was already dispatched today."""
    try:
        from database import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS daily_send_log (
                        send_date TEXT PRIMARY KEY
                    )
                    """
                )
                cur.execute(
                    "SELECT 1 FROM daily_send_log WHERE send_date = %s",
                    (datetime.now().strftime("%Y-%m-%d"),)
                )
                return cur.fetchone() is not None
    except Exception:
        return False  # if DB unreachable, allow send rather than silently suppress

def _mark_sent_today():
    """Persist today's date to Postgres so restarts don't re-send."""
    try:
        from database import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS daily_send_log (
                        send_date TEXT PRIMARY KEY
                    )
                    """
                )
                cur.execute(
                    "INSERT INTO daily_send_log (send_date) VALUES (%s) ON CONFLICT DO NOTHING",
                    (datetime.now().strftime("%Y-%m-%d"),)
                )
                conn.commit()   # FIX: was missing — insert was never persisted
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"⚠️ Could not write send guard to DB: {e}")

# ── LOGGING SETUP ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# =====================================================================================
# OUTPUT FILES
# =====================================================================================

OUTPUT_PARQUET = WATCHLIST_PATH
OUTPUT_CSV     = WATCHLIST_PATH.replace(".parquet", ".csv")
EXCLUSION_CSV  = OUTPUT_CSV.replace(".csv", "_excluded.csv")

# =====================================================================================
# SECTOR ROUTING
# =====================================================================================
FINANCIAL_SECTORS = {
    "Finance",
    "Banks",
    "Insurance",
    "Financial Services",
}

# =====================================================================================
# CATEGORY DICTIONARY (PLAIN ENGLISH EXPLANATIONS)
# =====================================================================================
CAT_DESCRIPTIONS = {
    # Non-Financial (PATH A)
    "High Momentum":            "Explosive recent sales & profit growth with expanding margins.",
    "Wealth Compounder":        "High ROE, strong margins, low debt — proven wealth creator.",
    "Blue Chip Stable":         "Large cap (₹5000Cr+) with consistent returns and low risk.",
    "Recovery Play":            "Recovering from downturn with expanding margins and improving ROE.",
    "Consistent Performer":     "Steady 10%+ growth with stable ROE and healthy margins.",
    "Long Term Compounder":     "💎 5Y+ consistent revenue & earnings growth, fair valuation (PEG ≤ 1.5), cash flow positive.",
    "Debt-Free Cash Generator": "Zero debt, large cap, and generating high cash flow with strong ROE.",
    "Undervalued Growth":       "Growing fast (>15% YoY) but trading at a deep discount (PEG < 1.0).",
    "Capital Efficient":        "Asset-light business model with exceptional ROE (≥25%).",
    "Dividend Aristocrat":      "High dividend yield (≥3.0%) with strong ROE and stability.",
    # Financial (PATH B)
    "Fast Growing Financial":   "Explosive recent NII & profit momentum in banking/NBFC.",
    "Top Bank/NBFC":            "High ROE & ROA with strong consistent loan book growth.",
    "Blue Chip Financial":      "Large cap bank/NBFC with highly stable returns.",
    "Financial Recovery":       "Recovering financial with improving asset quality and profitability.",
    "Efficient Lender":         "Top-tier banking quality with exceptional ROA (≥2.0%).",
}

# =====================================================================================
# BASE FILTERS 
# =====================================================================================

MIN_PRICE         = 100              # no penny stocks — ₹100 minimum for institutional interest
MIN_MARKET_CAP    = 10_000_000_000   # ₹1,000 Cr — exclude micro-caps
MIN_TRADED_VALUE  = 150_000_000      # ₹15 Cr/day — reliable fills on breakout entries
MIN_ROE           = 12               # 12% ROE = above cost of equity, genuine moat

# PATH A only
MIN_OPM_NONFIN    = 10               # 10% OPM — excludes commodity-level margins

# PATH B only
MIN_ROA_FIN       = 0.8              

# =====================================================================================
# GROWTH THRESHOLDS 
# =====================================================================================

# PATH A — non-financial
HIGH_GROWTH_YOY    = 15.0
HIGH_GROWTH_QOQ    =  5.0
COMPOUNDER_YOY     =  3.0
STEADY_YOY         = 10.0
TURNAROUND_PROFIT  = 30.0

# PATH B — financial
FIN_HIGH_GROWTH_YOY   = 15.0   
FIN_COMPOUNDER_YOY    =  5.0   
FIN_TURNAROUND_PROFIT = 25.0   

# =====================================================================================
# ANOMALY GUARDS
# =====================================================================================

MIN_YOY = -90.0
MAX_YOY = 500.0

# =====================================================================================
# EXCLUSION LOG
# =====================================================================================

EXCLUSION_LOG: list[dict] = []
_exclusion_lock = threading.Lock()

def log_exclusion(symbol: str, reason: str) -> None:
    with _exclusion_lock:
        EXCLUSION_LOG.append({
            "Stock":     symbol,
            "Reason":    reason,
            "Scan Time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

# =====================================================================================
# FETCH UNIVERSE
# =====================================================================================

def fetch_universe() -> pd.DataFrame:
    logger.info("📡 Fetching NSE stocks and forensic accounting data from TradingView...")

    fields = [
        "name", "sector", "close", "average_volume_30d_calc",
        "market_cap_basic", "return_on_equity_fy", "operating_margin", 
        "debt_to_equity_fq", "return_on_assets_fq", "earnings_per_share_basic_ttm",
        "gross_profit_yoy_growth_ttm", "gross_profit_qoq_growth_fq",
        "earnings_per_share_diluted_yoy_growth_ttm", "earnings_per_share_diluted_qoq_growth_fq",
        "total_revenue_yoy_growth_ttm", "total_revenue_qoq_growth_fq",
        "net_income_yoy_growth_ttm", "net_income_qoq_growth_fq",
        # NEW LONG-TERM METRICS
        "price_earnings_ttm", 
        "total_revenue_5y_growth",
        "earnings_per_share_basic_5y_growth",
        "free_cash_flow_margin_ttm",
        "dividend_yield_recent"
    ]

    q = (
        Query()
        .set_markets("india")
        .select(*fields)
        .where(
            col("exchange")                     == "NSE",
            col("close")                        >= MIN_PRICE,
            col("market_cap_basic")             >= MIN_MARKET_CAP,
            col("earnings_per_share_basic_ttm") >  0,
            col("return_on_equity_fy")          >= MIN_ROE,
        )
        .limit(5000)
    )

    total, df = q.get_scanner_data()
    logger.info(f"✅ Universe fetched: {total} stocks")
    return df

# =====================================================================================
# SHARED UTILITIES
# =====================================================================================

def _fval(row: pd.Series, col_name: str) -> float | None:
    v = row.get(col_name)
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None

def _is_financial(sector: str) -> bool:
    return sector in FINANCIAL_SECTORS

def _anomaly_check(symbol: str, yoy_rev: float, yoy_profit: float) -> str | None:
    if yoy_rev < MIN_YOY or yoy_profit < MIN_YOY:
        return f"Structural collapse: YoY Revenue={yoy_rev:.1f}%, YoY Profit={yoy_profit:.1f}%"
    if yoy_rev > MAX_YOY or yoy_profit > MAX_YOY:
        return f"Extreme base-effect anomaly: YoY Revenue={yoy_rev:.1f}%, YoY Profit={yoy_profit:.1f}%"
    return None

# =====================================================================================
# PATH A — NON-FINANCIAL CLASSIFICATION
# =====================================================================================

def _classify_nonfin(row: pd.Series, symbol: str) -> dict | None:
    def fv(c): return _fval(row, c)
    def skip(r): return log_exclusion(symbol, r) or None

    close_price = fv("close")
    avg_volume  = fv("average_volume_30d_calc")
    market_cap  = fv("market_cap_basic")
    roe         = fv("return_on_equity_fy")
    opm         = fv("operating_margin")

    _raw_de     = fv("debt_to_equity_fq")
    debt_equity = _raw_de if _raw_de is not None else 0.0
    debt_missing = _raw_de is None

    yoy_sales   = fv("gross_profit_yoy_growth_ttm")
    qoq_sales   = fv("gross_profit_qoq_growth_fq")
    yoy_profit  = fv("earnings_per_share_diluted_yoy_growth_ttm")
    qoq_profit  = fv("earnings_per_share_diluted_qoq_growth_fq")

    # Long-term specific metrics
    pe          = fv("price_earnings_ttm")
    rev_5y      = fv("total_revenue_5y_growth")
    eps_5y      = fv("earnings_per_share_basic_5y_growth")
    fcf_margin  = fv("free_cash_flow_margin_ttm")
    div_yield   = fv("dividend_yield_recent")

    missing = [
        name for name, val in [
            ("close", close_price), ("average_volume_30d_calc", avg_volume),
            ("market_cap_basic", market_cap), ("return_on_equity_fy", roe),
            ("operating_margin", opm), ("gross_profit_yoy_growth_ttm", yoy_sales),
            ("gross_profit_qoq_growth_fq", qoq_sales),
            ("earnings_per_share_diluted_yoy_growth_ttm", yoy_profit),
            ("earnings_per_share_diluted_qoq_growth_fq", qoq_profit),
        ] if val is None
    ]
    if missing:
        return skip(f"Missing data: {', '.join(missing)}")

    MEGA_CAP_BYPASS = 100_000_000_000   
    is_mega_cap = (market_cap is not None and market_cap >= MEGA_CAP_BYPASS)
    if opm < MIN_OPM_NONFIN and not is_mega_cap:
        return skip(f"OPM too low: {opm:.1f}% (min {MIN_OPM_NONFIN}%)")

    traded_value = avg_volume * close_price
    if traded_value < MIN_TRADED_VALUE:
        return skip(f"Low liquidity: ₹{traded_value/1e7:.1f} Cr/day")

    anomaly = _anomaly_check(symbol, yoy_sales, yoy_profit)
    if anomaly:
        return skip(anomaly)

    # ── PEG RATIO CALCULATION ──
    peg = None
    if pe is not None and pe > 0 and yoy_profit > 0:
        peg = pe / yoy_profit

    yoy_margin_expanding = (yoy_profit >= yoy_sales)
    qoq_margin_expanding = (qoq_profit > 0 and qoq_profit >= qoq_sales)
    # FIX: When D/E data is missing, don't assume debt-free.
    # Missing D/E → low_debt = False (uncertain, can't confirm low debt).
    # Categories that require low_debt (Wealth Compounder, Blue Chip Stable) won't
    # falsely include stocks with unknown debt levels.
    low_debt = (not debt_missing) and (debt_equity <= 1.0)

    high_growth = (yoy_sales > HIGH_GROWTH_YOY and yoy_profit > HIGH_GROWTH_YOY and yoy_margin_expanding)
    elite_compounder = (yoy_sales > COMPOUNDER_YOY and yoy_profit > COMPOUNDER_YOY and roe >= 15 and opm >= 12 and low_debt)
    mature_quality = (roe >= 15 and low_debt and market_cap >= 50_000_000_000 and (opm >= 15 or is_mega_cap))
    turnaround = (yoy_profit >= TURNAROUND_PROFIT and yoy_margin_expanding and opm >= 12 and yoy_sales >= -10.0 and roe >= 12)
    steady_compounder = (yoy_sales >= STEADY_YOY and yoy_profit >= STEADY_YOY and roe >= 14 and opm >= 10)

    # NEW: Safest wealth creators
    debt_free_cash = (debt_equity <= 0.1 and roe >= 20 and market_cap >= 100_000_000_000 and (fcf_margin is None or fcf_margin > 0))
    # NEW: Undervalued growth
    undervalued_growth = (yoy_sales > 15.0 and yoy_profit > 15.0 and peg is not None and 0 < peg < 1.0)
    # NEW: Capital efficient
    capital_efficient = (roe >= 25.0 and opm >= 15.0 and low_debt)
    
    # NEW: Dividend Aristocrat
    div_val = div_yield if div_yield is not None else 0.0
    dividend_aristocrat = (div_val >= 3.0 and roe >= 15.0 and low_debt and market_cap >= 50_000_000_000)

    # ── DIAMOND HOLD (LONG TERM) LOGIC ──
    diamond_hold = False
    if rev_5y is not None and eps_5y is not None and peg is not None:
        fcf_ok = (fcf_margin is None) or (fcf_margin > 0)
        if rev_5y >= 12.0 and eps_5y >= 15.0 and peg <= 1.5 and fcf_ok:
            diamond_hold = True

    if not any([high_growth, elite_compounder, mature_quality, turnaround, steady_compounder, diamond_hold, debt_free_cash, undervalued_growth, capital_efficient, dividend_aristocrat]):
        return skip(f"No category — YoY Sales={yoy_sales:.1f}%, YoY Profit={yoy_profit:.1f}%")

    cats = []
    if diamond_hold:       cats.append("Long Term Compounder")
    if dividend_aristocrat:cats.append("Dividend Aristocrat")
    if debt_free_cash:     cats.append("Debt-Free Cash Generator")
    if undervalued_growth: cats.append("Undervalued Growth")
    if capital_efficient:  cats.append("Capital Efficient")
    if high_growth:        cats.append("High Momentum")
    if elite_compounder:   cats.append("Wealth Compounder")
    if mature_quality:     cats.append("Blue Chip Stable")
    if turnaround:         cats.append("Recovery Play")
    if steady_compounder:  cats.append("Consistent Performer")

    score = _score_nonfin(yoy_sales, yoy_profit, qoq_sales, qoq_profit, roe, opm, debt_equity, yoy_margin_expanding, qoq_margin_expanding, mature_quality, elite_compounder, turnaround)

    return _build_row(
        symbol=symbol, cats=cats, path="Non-Financial", row=row, close_price=close_price,
        market_cap=market_cap, roe=roe, opm=opm, debt_equity=debt_equity, debt_missing=debt_missing,
        qoq_rev=qoq_sales, yoy_rev=yoy_sales, qoq_profit=qoq_profit, yoy_profit=yoy_profit, score=score, peg=peg
    )

# =====================================================================================
# PATH B — FINANCIAL CLASSIFICATION 
# =====================================================================================

def _classify_fin(row: pd.Series, symbol: str) -> dict | None:
    def fv(c): return _fval(row, c)
    def skip(r): return log_exclusion(symbol, r) or None

    close_price = fv("close")
    avg_volume  = fv("average_volume_30d_calc")
    market_cap  = fv("market_cap_basic")
    roe         = fv("return_on_equity_fy")
    roa         = fv("return_on_assets_fq")

    _raw_de      = fv("debt_to_equity_fq")
    debt_equity  = _raw_de if _raw_de is not None else 0.0
    debt_missing = _raw_de is None

    yoy_rev    = fv("total_revenue_yoy_growth_ttm")
    qoq_rev    = fv("total_revenue_qoq_growth_fq")
    yoy_profit = fv("net_income_yoy_growth_ttm")
    qoq_profit = fv("net_income_qoq_growth_fq")

    pe          = fv("price_earnings_ttm")
    rev_5y      = fv("total_revenue_5y_growth")
    eps_5y      = fv("earnings_per_share_basic_5y_growth")
    div_yield   = fv("dividend_yield_recent")

    missing = [
        name for name, val in [
            ("close", close_price), ("average_volume_30d_calc", avg_volume),
            ("market_cap_basic", market_cap), ("return_on_equity_fy", roe),
            ("return_on_assets_fq", roa), ("total_revenue_yoy_growth_ttm", yoy_rev),
            ("total_revenue_qoq_growth_fq", qoq_rev), ("net_income_yoy_growth_ttm", yoy_profit),
            ("net_income_qoq_growth_fq", qoq_profit),
        ] if val is None
    ]
    if missing:
        return skip(f"Missing data (financial path): {', '.join(missing)}")

    if roa < MIN_ROA_FIN:
        return skip(f"ROA too low: {roa:.2f}% (min {MIN_ROA_FIN}%)")

    traded_value = avg_volume * close_price
    if traded_value < MIN_TRADED_VALUE:
        return skip(f"Low liquidity: ₹{traded_value/1e7:.1f} Cr/day")

    anomaly = _anomaly_check(symbol, yoy_rev, yoy_profit)
    if anomaly:
        return skip(anomaly)
        
    peg = None
    if pe is not None and pe > 0 and yoy_profit > 0:
        peg = pe / yoy_profit

    yoy_margin_expanding = (yoy_profit >= yoy_rev)

    fin_high_growth = (yoy_rev > FIN_HIGH_GROWTH_YOY and yoy_profit > FIN_HIGH_GROWTH_YOY and yoy_margin_expanding)
    fin_compounder = (yoy_rev > FIN_COMPOUNDER_YOY and yoy_profit > FIN_COMPOUNDER_YOY and roe >= 15 and roa >= 1.0)
    fin_mature_quality = (roe >= 15 and roa >= 1.0 and market_cap >= 50_000_000_000)
    fin_turnaround = (yoy_profit >= FIN_TURNAROUND_PROFIT and yoy_margin_expanding and yoy_rev >= -10.0 and roe >= 10 and roa >= 0.8)

    # NEW: Efficient lender
    efficient_lender = (roa >= 2.0 and roe >= 15.0 and market_cap >= 50_000_000_000)

    # NEW: Dividend Aristocrat
    div_val = div_yield if div_yield is not None else 0.0
    dividend_aristocrat = (div_val >= 3.0 and roe >= 15.0 and market_cap >= 50_000_000_000)

    diamond_hold = False
    if rev_5y is not None and eps_5y is not None and peg is not None:
        if rev_5y >= 12.0 and eps_5y >= 15.0 and peg <= 1.5:
            diamond_hold = True

    if not any([fin_high_growth, fin_compounder, fin_mature_quality, fin_turnaround, diamond_hold, efficient_lender, dividend_aristocrat]):
        return skip(f"No financial category — YoY NII={yoy_rev:.1f}%, YoY Profit={yoy_profit:.1f}%")

    cats = []
    if diamond_hold:       cats.append("Long Term Compounder")
    if dividend_aristocrat:cats.append("Dividend Aristocrat")
    if efficient_lender:   cats.append("Efficient Lender")
    if fin_high_growth:    cats.append("Fast Growing Financial")
    if fin_compounder:     cats.append("Top Bank/NBFC")
    if fin_mature_quality: cats.append("Blue Chip Financial")
    if fin_turnaround:     cats.append("Financial Recovery")

    score = _score_fin(yoy_rev, yoy_profit, qoq_rev, qoq_profit, roe, roa, yoy_margin_expanding, fin_mature_quality, fin_compounder)

    return _build_row(
        symbol=symbol, cats=cats, path="Financial", row=row, close_price=close_price,
        market_cap=market_cap, roe=roe, opm=None, debt_equity=debt_equity, debt_missing=debt_missing,
        qoq_rev=qoq_rev, yoy_rev=yoy_rev, qoq_profit=qoq_profit, yoy_profit=yoy_profit, score=score, roa=roa, peg=peg
    )

# =====================================================================================
# SCORING
# =====================================================================================

def _score_nonfin(yoy_sales, yoy_profit, qoq_sales, qoq_profit, roe, opm, debt_equity, yoy_margin, qoq_margin, mature_quality, elite_compounder, turnaround) -> int:
    score = 0
    if yoy_sales >= 20: score += 20
    elif yoy_sales >= 10: score += 10
    if yoy_profit >= 25: score += 25
    elif yoy_profit >= 10: score += 12
    if qoq_sales >= 10: score += 8
    elif qoq_sales >= 5: score += 4
    if qoq_profit >= 10: score += 12
    elif qoq_profit >= 5: score += 6
    if roe >= 25: score += 15
    elif roe >= 20: score += 10
    elif roe >= 15: score += 5
    if opm >= 20: score += 10
    elif opm >= 15: score += 7
    elif opm >= 10: score += 3
    if yoy_margin: score += 5
    if qoq_margin: score += 3
    if debt_equity == 0.0 or debt_equity <= 0.1: score += 10
    elif debt_equity <= 0.5: score += 7
    elif debt_equity <= 1.0: score += 3
    if mature_quality: score += 10
    if elite_compounder: score += 5
    if turnaround: score += 3
    return score


def _score_fin(yoy_rev, yoy_profit, qoq_rev, qoq_profit, roe, roa, yoy_margin, fin_mature, fin_compounder) -> int:
    score = 0
    if yoy_rev >= 20: score += 20
    elif yoy_rev >= 10: score += 10
    elif yoy_rev >= 5: score += 5
    if yoy_profit >= 25: score += 25
    elif yoy_profit >= 15: score += 15
    elif yoy_profit >= 5: score += 8
    if qoq_rev >= 10: score += 8
    elif qoq_rev >= 5: score += 4
    if qoq_profit >= 10: score += 12
    elif qoq_profit >= 5: score += 6
    if roe >= 20: score += 15
    elif roe >= 15: score += 10
    elif roe >= 12: score += 5
    if roa >= 2.0: score += 15 
    elif roa >= 1.5: score += 10
    elif roa >= 1.0: score += 5
    if yoy_margin: score += 5
    if fin_mature: score += 10
    if fin_compounder: score += 5
    return score

# =====================================================================================
# ROW BUILDER (shared)
# =====================================================================================

def _build_row(*, symbol, cats, path, row, close_price, market_cap, roe, opm, debt_equity, debt_missing, qoq_rev, yoy_rev, qoq_profit, yoy_profit, score, roa=None, peg=None) -> dict:
    desc_list = [CAT_DESCRIPTIONS.get(c, "") for c in cats]
    cat_desc = " | ".join(filter(None, desc_list))

    return {
        "Stock":                symbol,
        "Category":             " + ".join(cats),
        "Category Explanation": cat_desc,
        "Path":                 path,
        "Sector":               row.get("sector", "Unknown"),
        "CMP":                  round(close_price, 2),
        "Market Cap Cr":        round(market_cap / 10_000_000, 2),
        "PEG Ratio":            round(peg, 2) if peg is not None else None,
        "ROE %":                round(roe, 2),
        "ROA %":                round(roa, 2) if roa is not None else None,
        "OPM %":                round(opm, 2) if opm is not None else None,
        "Debt/Equity":          round(debt_equity, 2),
        "D/E Missing":          debt_missing,
        "QOQ Revenue %":        round(qoq_rev,    2),
        "YOY Revenue %":        round(yoy_rev,    2),
        "QOQ Profit %":         round(qoq_profit, 2),
        "YOY Profit %":         round(yoy_profit, 2),
        "Fundamental Score":    score,
        "Scan Time":            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

# =====================================================================================
# DISPATCHER
# =====================================================================================

def classify_stock(row: pd.Series) -> dict | None:
    symbol = str(row.get("name", "UNKNOWN"))
    sector = str(row.get("sector", ""))
    try:
        if _is_financial(sector):
            return _classify_fin(row, symbol)
        else:
            return _classify_nonfin(row, symbol)
    except Exception as e:
        logger.error(f"❌ EXCEPTION [{symbol}]: {e}")
        return None

# =====================================================================================
# MAIN
# =====================================================================================

def main():
    from database import upsert_scanner_health
    try:
        upsert_scanner_health("DAILY_BUILDER", "OK", error_msg=None)
    except Exception:
        logger.warning("⚠️ Could not mark Daily Builder as OK")

    try:
        _main_impl()
        
        try:
            from zoneinfo import ZoneInfo
            upsert_scanner_health(
                "DAILY_BUILDER", "OK",
                last_success=datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S"),
                error_msg=None
            )
        except Exception:
            logger.warning("⚠️ Could not update Daily Builder success heartbeat")
    except Exception as exc:
        logger.exception("❌ CRITICAL ERROR in daily builder")
        try:
            upsert_scanner_health("DAILY_BUILDER", "DOWN", error_msg=str(exc)[:500])
        except Exception:
            pass
        raise


def _main_impl():
    with _exclusion_lock:
        EXCLUSION_LOG.clear()  
        
    logger.info("🚀 ELITE FUNDAMENTAL SCAN STARTED")

    os.makedirs(os.path.dirname(OUTPUT_PARQUET), exist_ok=True)

    universe_df = fetch_universe()

    if universe_df.empty:
        logger.error("❌ No stocks returned from TradingView")
        return

    fin_mask = universe_df["sector"].isin(FINANCIAL_SECTORS)
    logger.info(f"📊 Classifying {len(universe_df)} stocks... (Path A: {(~fin_mask).sum()} | Path B: {fin_mask.sum()})")

    results = [classify_stock(row) for _, row in universe_df.iterrows()]
    winners = [r for r in results if r is not None]

    if EXCLUSION_LOG:
        with _exclusion_lock:
            exclusion_snapshot = list(EXCLUSION_LOG)
        pd.DataFrame(exclusion_snapshot).to_csv(EXCLUSION_CSV, index=False)
        logger.info(f"📋 Exclusion log saved to {EXCLUSION_CSV} ({len(exclusion_snapshot)} skipped)")

    if not winners:
        logger.warning("❌ No qualifying stocks after classification")
        return

    final_df = (
        pd.DataFrame(winners)
        .sort_values(
            by=["Fundamental Score", "ROE %", "YOY Profit %"],
            ascending=False,
        )
        .reset_index(drop=True)
    )

    final_df.to_csv(OUTPUT_CSV, index=False)
    final_df.to_parquet(OUTPUT_PARQUET, index=False)

    logger.info(f"✅ FINAL WATCHLIST SAVED: {len(final_df)} stocks")

    print("\n── Top 10 ──────────────────────────────────────\n")
    print(final_df.head(10).to_string(index=False))

    # =================================================================================
    # EMAIL DISPATCH WITH TELEGRAM FALLBACK (EXACTLY ONCE PER DAY)
    # =================================================================================
    if _already_sent_today():
        logger.info("📧 Fundamental watchlist already dispatched today — skipping send (restart guard).")
        return

    try:
        from email_engine import send_html_email
        logger.info("📧 Attempting to email fundamental watchlist...")
        
        email_df = final_df[['Stock', 'Category', 'Category Explanation', 'PEG Ratio', 'Sector', 'CMP', 'Fundamental Score']]
        table_html = email_df.to_html(index=False, border=0, classes="styled-table", justify="left")
        
        html_content = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; background-color: #f4f7f6; color: #333; padding: 20px; }}
                .container {{ max-width: 900px; margin: 0 auto; background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.05); }}
                h1 {{ color: #2c3e50; text-align: center; border-bottom: 2px solid #3498db; padding-bottom: 10px; }}
                .styled-table {{ border-collapse: collapse; margin: 15px 0; font-size: 0.85em; width: 100%; }}
                .styled-table thead tr {{ background-color: #34495e; color: #ffffff; text-align: left; }}
                .styled-table th, .styled-table td {{ padding: 12px 10px; border-bottom: 1px solid #dddddd; }}
                .styled-table td:nth-child(3) {{ color: #555; font-style: italic; max-width: 250px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🌟 Daily Fundamental Watchlist</h1>
                <p style="text-align: center; color: #7f8c8d;">Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
                <h2>📋 Elite Universe ({len(final_df)} Stocks)</h2>
                {table_html}
                
                <hr style="margin-top: 30px; border: 0; border-top: 1px solid #ddd;">
                <p style="font-size: 12px; color: #7f8c8d; text-align: center;">
                    <strong>PEG Ratio Legend:</strong> < 1.0 (Deep Value) | 1.0 to 1.5 (Fair Value) | > 2.0 (Overvalued)
                </p>
            </div>
        </body>
        </html>
        """
        
        subject = f"🌟 Daily Fundamental Watchlist - {datetime.now().strftime('%Y-%m-%d')}"
        
        # ── 1. ATTEMPT EMAIL ──────────────────────────────────────────────────────
        email_success = send_html_email(subject, html_content, attachment_path=OUTPUT_CSV)
        
        # ── 2. TELEGRAM FALLBACK ──────────────────────────────────────────────────
        if not email_success:
            logger.warning("⚠️ Email delivery failed or timed out. Activating Telegram Fallback...")
            
            bot_token = os.getenv("BOT_TOKEN")
            chat_id   = os.getenv("CHAT_ID")
            
            if bot_token and chat_id and os.path.exists(OUTPUT_CSV):
                url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
                caption = f"🌟 *Daily Fundamental Watchlist*\nDate: {datetime.now().strftime('%Y-%m-%d')}\nTotal Stocks: {len(final_df)}\n\n_Email delivery blocked. CSV attached._"
                
                with open(OUTPUT_CSV, 'rb') as doc:
                    resp = requests.post(url, data={'chat_id': chat_id, 'caption': caption, 'parse_mode': 'Markdown'}, files={'document': doc}, timeout=15)
                
                if resp.status_code == 200:
                    logger.info("✅ Watchlist CSV successfully delivered to Telegram.")
                else:
                    logger.error(f"❌ Telegram fallback failed: {resp.text}")
            else:
                logger.error("❌ Cannot execute Telegram fallback: Missing BOT_TOKEN/CHAT_ID or CSV file.")
                
    except Exception as e:
        logger.error(f"❌ CRITICAL ERROR in dispatch block: {e}")
        logger.error(traceback.format_exc())
        return

    # Mark as sent AFTER successful dispatch so a crash mid-send doesn't suppress the next attempt
    _mark_sent_today()
    logger.info("✅ Send guard updated — will not re-send today on restart")

# =====================================================================================
# ALIAS
build_watchlist = main

if __name__ == "__main__":
    main()
