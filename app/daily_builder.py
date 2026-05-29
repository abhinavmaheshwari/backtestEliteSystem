# =====================================================================================
# app/daily_builder.py
# =====================================================================================
#
# ARCHITECTURE
# ------------
# All data is fetched in ONE TradingView Screener API call — no yfinance.
#
# COLUMN CHOICES (verified against live fields docs)
# ---------------------------------------------------
# Growth columns that actually exist in the TradingView API:
#
#   gross_profit_yoy_growth_ttm   → Gross Profit TTM YoY  (best revenue proxy)
#   gross_profit_qoq_growth_fq    → Gross Profit QoQ
#   gross_profit_yoy_growth_fq    → Gross Profit Quarterly YoY
#
#   earnings_per_share_diluted_yoy_growth_ttm  → EPS TTM YoY  (profit proxy)
#   earnings_per_share_diluted_qoq_growth_fq   → EPS QoQ
#   earnings_per_share_diluted_yoy_growth_fq   → EPS Quarterly YoY
#
# Why NOT revenue_yoy_growth_ttm / net_income_yoy_growth_ttm:
#   Those column names do NOT exist in the TradingView India endpoint.
#   The API returns HTTP 400 "Unknown field" for them (confirmed by runtime error).
#   gross_profit and EPS growth are the closest confirmed substitutes.
#
# =====================================================================================

import os
import pandas as pd

from datetime import datetime
from tradingview_screener import Query, col

from config import WATCHLIST_PATH

# =====================================================================================
# OUTPUT FILES
# =====================================================================================

OUTPUT_PARQUET = WATCHLIST_PATH
OUTPUT_CSV     = WATCHLIST_PATH.replace(".parquet", ".csv")
EXCLUSION_CSV  = OUTPUT_CSV.replace(".csv", "_excluded.csv")

# =====================================================================================
# BASE FILTERS
# =====================================================================================

MIN_PRICE         = 50
MIN_MARKET_CAP    = 5_000_000_000    # ₹500 Cr
MIN_TRADED_VALUE  = 100_000_000      # ₹10 Cr/day
MIN_ROE           = 10               # %
MIN_OPM           = 8                # %

# =====================================================================================
# GROWTH THRESHOLDS  (TV returns percent values, e.g. 15.0 = 15%)
# Note: TV growth columns are in PERCENT (15.0), not decimal (0.15)
# =====================================================================================

HIGH_GROWTH_YOY  = 15.0    # 15%
HIGH_GROWTH_QOQ  =  5.0    #  5%
COMPOUNDER_YOY   =  3.0    #  3%

# =====================================================================================
# EXCLUSION LOG
# =====================================================================================

EXCLUSION_LOG: list[dict] = []

def log_exclusion(symbol: str, reason: str) -> None:
    EXCLUSION_LOG.append({
        "Stock":     symbol,
        "Reason":    reason,
        "Scan Time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

# =====================================================================================
# FETCH UNIVERSE  —  single API call, all columns included
# =====================================================================================

def fetch_universe() -> pd.DataFrame:
    """
    One TradingView Screener call returns every qualifying NSE stock
    with all fundamental + growth columns pre-computed server-side.

    Growth columns used (all verified to exist in the TV fields schema):
      gross_profit_yoy_growth_ttm      — TTM gross profit YoY %
      gross_profit_qoq_growth_fq       — quarterly gross profit QoQ %
      earnings_per_share_diluted_yoy_growth_ttm — TTM EPS YoY %
      earnings_per_share_diluted_qoq_growth_fq  — quarterly EPS QoQ %
    """

    print("\n📡 Fetching NSE stocks (single API call)...\n")

    fields = [
        # identity
        "name",
        "sector",

        # price / liquidity
        "close",
        "average_volume_30d_calc",

        # size
        "market_cap_basic",

        # quality
        "return_on_equity_fy",
        "operating_margin",
        "debt_to_equity_fq",

        # profitability gate
        "earnings_per_share_basic_ttm",

        # ── growth (confirmed fields, TV percent scale) ────────────────
        # Sales proxy: Gross Profit growth
        "gross_profit_yoy_growth_ttm",   # TTM YoY  ← primary YoY signal
        "gross_profit_qoq_growth_fq",    # QoQ
        "gross_profit_yoy_growth_fq",    # Quarterly YoY (same-quarter vs last year)

        # Profit proxy: EPS Diluted growth
        "earnings_per_share_diluted_yoy_growth_ttm",  # TTM YoY
        "earnings_per_share_diluted_qoq_growth_fq",   # QoQ
        "earnings_per_share_diluted_yoy_growth_fq",   # Quarterly YoY
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
            col("operating_margin")             >= MIN_OPM,
        )
        .limit(5000)
    )

    total, df = q.get_scanner_data()

    print(f"✅ Universe fetched: {total} stocks")

    return df

# =====================================================================================
# CLASSIFY + SCORE  —  pure in-memory, zero network calls
# =====================================================================================

def classify_stock(row: pd.Series) -> dict | None:

    symbol = str(row.get("name", "UNKNOWN"))

    def fval(col_name: str) -> float | None:
        v = row.get(col_name)
        try:
            f = float(v)
            return None if pd.isna(f) else f
        except (TypeError, ValueError):
            return None

    def skip(reason: str):
        log_exclusion(symbol, reason)
        return None

    # ── extract values ────────────────────────────────────────────────
    close_price  = fval("close")
    avg_volume   = fval("average_volume_30d_calc")
    market_cap   = fval("market_cap_basic")
    roe          = fval("return_on_equity_fy")
    opm          = fval("operating_margin")
    debt_equity  = fval("debt_to_equity_fq") or 0.0

    # Sales growth (gross profit as proxy)
    yoy_sales    = fval("gross_profit_yoy_growth_ttm")
    qoq_sales    = fval("gross_profit_qoq_growth_fq")

    # Profit growth (EPS diluted as proxy)
    yoy_profit   = fval("earnings_per_share_diluted_yoy_growth_ttm")
    qoq_profit   = fval("earnings_per_share_diluted_qoq_growth_fq")

    # ── guard: required fields ────────────────────────────────────────
    missing = [
        name for name, val in [
            ("close",                close_price),
            ("average_volume_30d_calc", avg_volume),
            ("market_cap_basic",     market_cap),
            ("return_on_equity_fy",  roe),
            ("operating_margin",     opm),
            ("gross_profit_yoy_growth_ttm",               yoy_sales),
            ("gross_profit_qoq_growth_fq",                qoq_sales),
            ("earnings_per_share_diluted_yoy_growth_ttm", yoy_profit),
            ("earnings_per_share_diluted_qoq_growth_fq",  qoq_profit),
        ] if val is None
    ]

    if missing:
        return skip(f"Missing data: {', '.join(missing)}")

    # ── liquidity ─────────────────────────────────────────────────────
    traded_value = avg_volume * close_price
    if traded_value < MIN_TRADED_VALUE:
        return skip(
            f"Low liquidity: ₹{traded_value/1e7:.1f} Cr/day "
            f"(min ₹{MIN_TRADED_VALUE/1e7:.0f} Cr)"
        )

    # ── margin-improving proxy ────────────────────────────────────────
    # EPS grew faster than gross profit → net margin expanded
    margin_improving = qoq_profit >= qoq_sales

    # ── category filters ──────────────────────────────────────────────
    high_growth = (
        (qoq_sales > HIGH_GROWTH_QOQ or yoy_sales > HIGH_GROWTH_YOY)
        and
        (qoq_profit > HIGH_GROWTH_QOQ or yoy_profit > HIGH_GROWTH_YOY)
        and
        margin_improving
    )

    elite_compounder = (
        yoy_sales  > COMPOUNDER_YOY
        and yoy_profit > COMPOUNDER_YOY
        and roe        >= 15
        and opm        >= 12
        and (debt_equity <= 1.5 or debt_equity == 0)
    )

    mature_quality = (
        roe        >= 18
        and opm        >= 15
        and (debt_equity <= 1.5 or debt_equity == 0)
        and market_cap >= 50_000_000_000
    )

    if not (high_growth or elite_compounder or mature_quality):
        return skip(
            f"No category — "
            f"YoY Sales={yoy_sales:.1f}%, QoQ Sales={qoq_sales:.1f}%, "
            f"YoY Profit={yoy_profit:.1f}%, QoQ Profit={qoq_profit:.1f}%, "
            f"ROE={roe:.1f}%, OPM={opm:.1f}%"
        )

    # ── category label ────────────────────────────────────────────────
    cats = []
    if high_growth:      cats.append("High Growth")
    if elite_compounder: cats.append("Elite Compounder")
    if mature_quality:   cats.append("Mature Quality")

    # ── score ─────────────────────────────────────────────────────────
    score = 0
    if yoy_sales   > 20:  score += 20
    if yoy_profit  > 25:  score += 25
    if qoq_sales   > 10:  score += 10
    if qoq_profit  > 10:  score += 15
    if roe         > 20:  score += 15
    if opm         > 15:  score += 10
    if margin_improving:  score += 5
    if debt_equity <= 0.5: score += 10
    if mature_quality:    score += 10

    return {
        "Stock":             symbol,
        "Category":          " + ".join(cats),
        "Sector":            row.get("sector", "Unknown"),
        "CMP":               round(close_price, 2),
        "Market Cap Cr":     round(market_cap / 10_000_000, 2),
        "ROE %":             round(roe, 2),
        "OPM %":             round(opm, 2),
        "Debt/Equity":       round(debt_equity, 2),
        "QOQ Sales %":       round(qoq_sales,  2),
        "YOY Sales %":       round(yoy_sales,  2),
        "QOQ Profit %":      round(qoq_profit, 2),
        "YOY Profit %":      round(yoy_profit, 2),
        "Fundamental Score": score,
        "Scan Time":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

# =====================================================================================
# MAIN
# =====================================================================================

def main():

    print("\n🚀 ELITE FUNDAMENTAL SCAN STARTED\n")

    # Ensure output directory exists
    os.makedirs(os.path.dirname(OUTPUT_PARQUET), exist_ok=True)

    universe_df = fetch_universe()

    if universe_df.empty:
        print("❌ No stocks returned from TradingView")
        return

    print(f"\n📊 Classifying {len(universe_df)} stocks...\n")

    results = [classify_stock(row) for _, row in universe_df.iterrows()]

    winners = [r for r in results if r is not None]

    # ── save exclusion log ────────────────────────────────────────────
    if EXCLUSION_LOG:
        pd.DataFrame(EXCLUSION_LOG).to_csv(EXCLUSION_CSV, index=False)
        print(f"📋 Exclusion log → {EXCLUSION_CSV}  ({len(EXCLUSION_LOG)} skipped)")

    if not winners:
        print("❌ No qualifying stocks after classification")
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

    print("\n================================================")
    print(f"✅ FINAL WATCHLIST: {len(final_df)} stocks")
    print("================================================\n")
    print(final_df.head(20).to_string(index=False))
    print(f"\n💾 CSV Saved:     {OUTPUT_CSV}")
    print(f"💾 PARQUET Saved: {OUTPUT_PARQUET}")

# =====================================================================================

if __name__ == "__main__":
    main()
