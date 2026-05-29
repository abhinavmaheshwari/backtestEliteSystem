# =====================================================================================
# app/daily_builder.py
# =====================================================================================
#
# ARCHITECTURE NOTE
# -----------------
# All fundamental data — including QoQ/YoY growth — is fetched in a SINGLE
# TradingView Screener API call.  yfinance has been removed entirely.
#
# Why this matters:
#   Old approach : 1 TV call to get symbols  +  N yfinance calls (one per stock)
#                  → rate-limited at scale, inconsistent row names, slow
#   New approach : 1 TV call that returns everything including pre-computed growth
#                  → no rate limits, consistent column names, ~instant
#
# TradingView growth column naming convention (verified from live field docs):
#   *_yoy_growth_ttm  = TTM vs prior-TTM  (best for YoY — removes seasonality)
#   *_yoy_growth_fq   = latest quarter vs same quarter last year
#   *_qoq_growth_fq   = latest quarter vs previous quarter
#
# =====================================================================================

import pandas as pd

from datetime import datetime
from tradingview_screener import Query, col

from config import WATCHLIST_PATH

# =====================================================================================
# OUTPUT FILES
# =====================================================================================

OUTPUT_PARQUET = WATCHLIST_PATH

OUTPUT_CSV = WATCHLIST_PATH.replace(".parquet", ".csv")

EXCLUSION_CSV = OUTPUT_CSV.replace(".csv", "_excluded.csv")

# =====================================================================================
# BASE FILTERS  (applied server-side inside the TradingView query)
# =====================================================================================

MIN_PRICE             = 50
MIN_MARKET_CAP        = 5_000_000_000   # ₹500 Cr
MIN_TRADED_VALUE      = 100_000_000     # ₹10 Cr / day
MIN_ROE               = 10              # %
MIN_OPM               = 8               # %

# =====================================================================================
# GROWTH THRESHOLDS  (TV returns growth as a decimal, e.g. 0.15 = 15 %)
# =====================================================================================

HIGH_GROWTH_YOY  = 0.15    # 15 %
HIGH_GROWTH_QOQ  = 0.05    #  5 %
COMPOUNDER_YOY   = 0.03    #  3 %

# =====================================================================================
# FETCH FULL UNIVERSE  (single API call — no yfinance, no per-stock loops)
# =====================================================================================

def fetch_universe() -> pd.DataFrame:
    """
    Pull every qualifying NSE stock with all required fundamental columns
    in one single TradingView Screener request.

    Growth columns (pre-computed by TradingView on their servers):
      revenue_yoy_growth_ttm   – TTM revenue vs prior-TTM  (used for YoY)
      revenue_qoq_growth_fq    – latest quarter vs previous quarter
      net_income_yoy_growth_ttm – TTM net income vs prior-TTM
      net_income_qoq_growth_fq  – latest quarter net income vs previous quarter

    All values are decimals (0.15 = 15 %).  TV returns None / NaN when data
    is unavailable; those rows are dropped in classify_stock().
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

        # profitability gate (keeps only profit-making companies)
        "earnings_per_share_basic_ttm",

        # ── growth (pre-computed by TradingView) ──────────────────────────
        # Revenue
        "revenue_yoy_growth_ttm",   # TTM YoY  ← replaces yfinance YoY calc
        "revenue_qoq_growth_fq",    # QoQ      ← replaces yfinance QoQ calc

        # Net income / profit
        "net_income_yoy_growth_ttm",
        "net_income_qoq_growth_fq",

        # Margin trend proxy: current vs prior-quarter net margin
        # TV doesn't expose a "margin QoQ delta" column directly, so we use
        # the net_income_qoq relative to revenue_qoq as a margin-improving
        # signal (profit grew faster than revenue → margin expanded).
        # Both columns are already fetched above.
    ]

    q = (
        Query()
        .set_markets("india")
        .select(*fields)
        .where(
            col("exchange")                  == "NSE",
            col("close")                     >= MIN_PRICE,
            col("market_cap_basic")          >= MIN_MARKET_CAP,
            col("earnings_per_share_basic_ttm") > 0,
            col("return_on_equity_fy")       >= MIN_ROE,
            col("operating_margin")          >= MIN_OPM,
        )
        .limit(5000)
    )

    total, df = q.get_scanner_data()

    print(f"✅ Universe fetched: {total} stocks")

    return df

# =====================================================================================
# CLASSIFY + SCORE  (pure in-memory — no I/O, no network calls)
# =====================================================================================

def classify_stock(row: pd.Series) -> dict | None:
    """
    Apply category filters and compute a Fundamental Score for one row.

    Returns a result dict on success, or None if the stock is excluded.
    Appends an entry to EXCLUSION_LOG for every skipped stock.
    """

    symbol = str(row.get("name", "UNKNOWN"))

    # ── helper: safe float extraction ────────────────────────────────────
    def fval(col_name: str) -> float | None:
        v = row.get(col_name)
        try:
            f = float(v)
            return None if pd.isna(f) else f
        except (TypeError, ValueError):
            return None

    def skip(reason: str):
        EXCLUSION_LOG.append({
            "Stock":     symbol,
            "Reason":    reason,
            "Scan Time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        return None

    # ── extract all required values ──────────────────────────────────────
    close_price   = fval("close")
    avg_volume    = fval("average_volume_30d_calc")
    market_cap    = fval("market_cap_basic")
    roe           = fval("return_on_equity_fy")
    opm           = fval("operating_margin")
    debt_equity   = fval("debt_to_equity_fq") or 0.0   # treat missing as 0

    yoy_sales     = fval("revenue_yoy_growth_ttm")
    qoq_sales     = fval("revenue_qoq_growth_fq")
    yoy_profit    = fval("net_income_yoy_growth_ttm")
    qoq_profit    = fval("net_income_qoq_growth_fq")

    # ── guard: required fields must be present ────────────────────────────
    missing = [
        name for name, val in [
            ("close",                    close_price),
            ("average_volume_30d_calc",  avg_volume),
            ("market_cap_basic",         market_cap),
            ("return_on_equity_fy",      roe),
            ("operating_margin",         opm),
            ("revenue_yoy_growth_ttm",   yoy_sales),
            ("revenue_qoq_growth_fq",    qoq_sales),
            ("net_income_yoy_growth_ttm", yoy_profit),
            ("net_income_qoq_growth_fq", qoq_profit),
        ] if val is None
    ]

    if missing:
        return skip(f"Missing columns: {', '.join(missing)}")

    # ── liquidity check ──────────────────────────────────────────────────
    traded_value = avg_volume * close_price

    if traded_value < MIN_TRADED_VALUE:
        return skip(
            f"Liquidity too low: ₹{traded_value/1e7:.1f} Cr/day "
            f"(min ₹{MIN_TRADED_VALUE/1e7:.0f} Cr)"
        )

    # ── margin-improving proxy ───────────────────────────────────────────
    # Net income grew faster than revenue → margin expanded this quarter.
    # Uses QoQ figures (same period comparison).
    margin_improving = qoq_profit >= qoq_sales

    # ── category logic (unchanged from original) ─────────────────────────
    high_growth = (
        (qoq_sales > HIGH_GROWTH_QOQ or yoy_sales > HIGH_GROWTH_YOY)
        and
        (qoq_profit > HIGH_GROWTH_QOQ or yoy_profit > HIGH_GROWTH_YOY)
        and
        margin_improving
    )

    elite_compounder = (
        yoy_sales   > COMPOUNDER_YOY
        and yoy_profit  > COMPOUNDER_YOY
        and roe         >= 15
        and opm         >= 12
        and (debt_equity <= 1.5 or debt_equity == 0)
    )

    mature_quality = (
        roe         >= 18
        and opm         >= 15
        and (debt_equity <= 1.5 or debt_equity == 0)
        and market_cap  >= 50_000_000_000
    )

    if not (high_growth or elite_compounder or mature_quality):
        return skip(
            f"No category match — "
            f"YoY Sales={yoy_sales*100:.1f}%, "
            f"YoY Profit={yoy_profit*100:.1f}%, "
            f"QoQ Sales={qoq_sales*100:.1f}%, "
            f"QoQ Profit={qoq_profit*100:.1f}%, "
            f"ROE={roe:.1f}%, OPM={opm:.1f}%, "
            f"high_growth={high_growth}, "
            f"elite_compounder={elite_compounder}, "
            f"mature_quality={mature_quality}"
        )

    # ── category label ────────────────────────────────────────────────────
    categories = []
    if high_growth:       categories.append("High Growth")
    if elite_compounder:  categories.append("Elite Compounder")
    if mature_quality:    categories.append("Mature Quality")

    # ── score (unchanged from original) ──────────────────────────────────
    score = 0
    if yoy_sales   > 0.20: score += 20
    if yoy_profit  > 0.25: score += 25
    if qoq_sales   > 0.10: score += 10
    if qoq_profit  > 0.10: score += 15
    if roe         > 20:   score += 15
    if opm         > 15:   score += 10
    if margin_improving:   score += 5
    if debt_equity <= 0.5: score += 10
    if mature_quality:     score += 10

    return {
        "Stock":             symbol,
        "Category":          " + ".join(categories),
        "Sector":            row.get("sector", "Unknown"),
        "CMP":               round(close_price, 2),
        "Market Cap Cr":     round(market_cap / 10_000_000, 2),
        "ROE %":             round(roe, 2),
        "OPM %":             round(opm, 2),
        "Debt/Equity":       round(debt_equity, 2),
        "QOQ Sales %":       round(qoq_sales  * 100, 2),
        "YOY Sales %":       round(yoy_sales  * 100, 2),
        "QOQ Profit %":      round(qoq_profit * 100, 2),
        "YOY Profit %":      round(yoy_profit * 100, 2),
        "Fundamental Score": score,
        "Scan Time":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

# =====================================================================================
# EXCLUSION LOG  (module-level so classify_stock can append without passing it around)
# =====================================================================================

EXCLUSION_LOG: list[dict] = []

# =====================================================================================
# MAIN
# =====================================================================================

def main():

    print("\n🚀 ELITE FUNDAMENTAL SCAN STARTED\n")

    # Single network call — replaces 500 yfinance calls
    universe_df = fetch_universe()

    if universe_df.empty:
        print("❌ No stocks fetched from TradingView")
        return

    print(f"\n📊 Classifying {len(universe_df)} stocks (in-memory, no network)...\n")

    results = [classify_stock(row) for _, row in universe_df.iterrows()]

    winners = [r for r in results if r is not None]

    # ── save exclusion log ────────────────────────────────────────────────
    if EXCLUSION_LOG:
        pd.DataFrame(EXCLUSION_LOG).to_csv(EXCLUSION_CSV, index=False)
        print(f"📋 Exclusion log → {EXCLUSION_CSV}  ({len(EXCLUSION_LOG)} skipped)")

    # ── no winners ────────────────────────────────────────────────────────
    if not winners:
        print("❌ No qualifying stocks after classification")
        return

    # ── sort + save ───────────────────────────────────────────────────────
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

    # ── print summary ─────────────────────────────────────────────────────
    print("\n================================================")
    print(f"✅ FINAL WATCHLIST: {len(final_df)} stocks")
    print("================================================\n")
    print(final_df.head(20).to_string(index=False))
    print(f"\n💾 CSV Saved:     {OUTPUT_CSV}")
    print(f"💾 PARQUET Saved: {OUTPUT_PARQUET}")

# =====================================================================================

if __name__ == "__main__":
    main()
