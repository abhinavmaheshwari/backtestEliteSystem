# =====================================================================================
# app/daily_builder.py
# =====================================================================================
#
# ELITE NSE FUNDAMENTAL WATCHLIST BUILDER
#
# BUILDS DAILY DYNAMIC ELITE UNIVERSE CONTAINING:
#
# 1. HIGH GROWTH STOCKS
# 2. ELITE COMPOUNDERS
# 3. MATURE QUALITY GIANTS
#
# OUTPUT:
# data/elite_fundamental_watchlist.parquet
#
# =====================================================================================

import os
import pandas as pd
import yfinance as yf
import concurrent.futures

from tqdm import tqdm
from datetime import datetime
from tradingview_screener import Query, col

# =====================================================================================
# CONFIG
# =====================================================================================

OUTPUT_DIR = "data"

OUTPUT_CSV = f"{OUTPUT_DIR}/elite_fundamental_watchlist.csv"

OUTPUT_PARQUET = f"{OUTPUT_DIR}/elite_fundamental_watchlist.parquet"

MAX_WORKERS = 5

# =====================================================================================
# BASE FILTERS
# =====================================================================================

MIN_PRICE = 50

MIN_MARKET_CAP = 5_000_000_000      # 500 Cr

MIN_TRADED_VALUE = 100_000_000      # 10 Cr/day

MIN_ROE = 10

MIN_OPM = 8

# =====================================================================================
# GROWTH THRESHOLDS
# =====================================================================================

HIGH_GROWTH_YOY = 0.15

HIGH_GROWTH_QOQ = 0.05

COMPOUNDER_YOY = 0.03

# =====================================================================================
# FETCH BASE UNIVERSE
# =====================================================================================

def fetch_base_universe():

    print("\n📡 Fetching NSE stocks from TradingView...\n")

    fields = [

        "name",
        "close",
        "market_cap_basic",
        "average_volume_30d_calc",

        "return_on_equity_fy",
        "operating_margin",
        "debt_to_equity_fq",

        "earnings_per_share_basic_ttm",

        "sector"
    ]

    q = (

        Query()

        .set_markets("india")

        .select(*fields)

        .where(

            # NSE ONLY
            col("exchange") == "NSE",

            # REMOVE PENNY STOCKS
            col("close") >= MIN_PRICE,

            # REMOVE MICROCAP JUNK
            col("market_cap_basic") >= MIN_MARKET_CAP,

            # PROFITABLE COMPANIES ONLY
            col("earnings_per_share_basic_ttm") > 0,

            # BASIC QUALITY
            col("return_on_equity_fy") >= MIN_ROE,

            # DECENT OPERATING BUSINESS
            col("operating_margin") >= MIN_OPM
        )

        .limit(5000)
    )

    total, df = q.get_scanner_data()

    print(f"✅ Base universe fetched: {total} stocks")

    return df

# =====================================================================================
# ANALYZE STOCK
# =====================================================================================

def analyze_stock(row):

    try:

        symbol = str(row["name"])

        ticker = yf.Ticker(f"{symbol}.NS")

        q = ticker.quarterly_financials

        # ============================================================================
        # NEED MINIMUM 5 QUARTERS
        # ============================================================================

        if q is None or q.empty or q.shape[1] < 5:
            return None

        # ============================================================================
        # FUZZY MATCHING
        # ============================================================================

        revenue_rows = q[
            q.index.str.contains(
                "Revenue|Sales",
                case=False,
                na=False
            )
        ]

        profit_rows = q[
            q.index.str.contains(
                "Income|Profit",
                case=False,
                na=False
            )
        ]

        if revenue_rows.empty or profit_rows.empty:
            return None

        revenue = revenue_rows.iloc[0]

        profit = profit_rows.iloc[0]

        # ============================================================================
        # EXTRACT VALUES
        # ============================================================================

        current_rev = float(revenue.iloc[0])

        prev_q_rev = float(revenue.iloc[1])

        last_year_rev = float(revenue.iloc[4])

        current_profit = float(profit.iloc[0])

        prev_q_profit = float(profit.iloc[1])

        last_year_profit = float(profit.iloc[4])

        # ============================================================================
        # SAFETY CHECKS
        # ============================================================================

        if (

            current_rev <= 0 or
            prev_q_rev <= 0 or
            last_year_rev <= 0 or

            current_profit <= 0 or
            prev_q_profit <= 0 or
            last_year_profit <= 0
        ):
            return None

        # ============================================================================
        # GROWTH CALCULATIONS
        # ============================================================================

        qoq_sales = (
            (current_rev - prev_q_rev) / prev_q_rev
        )

        yoy_sales = (
            (current_rev - last_year_rev) / last_year_rev
        )

        qoq_profit = (
            (current_profit - prev_q_profit) / prev_q_profit
        )

        yoy_profit = (
            (current_profit - last_year_profit) / last_year_profit
        )

        # ============================================================================
        # MARGIN CHECK
        # ============================================================================

        current_margin = current_profit / current_rev

        previous_margin = prev_q_profit / prev_q_rev

        margin_improving = (
            current_margin >= previous_margin
        )

        # ============================================================================
        # LIQUIDITY CHECK
        # ============================================================================

        avg_volume = float(
            row.get("average_volume_30d_calc", 0)
        )

        close_price = float(
            row.get("close", 0)
        )

        traded_value = avg_volume * close_price

        if traded_value < MIN_TRADED_VALUE:
            return None

        # ============================================================================
        # COMMON QUALITY METRICS
        # ============================================================================

        roe = float(
            row.get("return_on_equity_fy", 0)
        )

        opm = float(
            row.get("operating_margin", 0)
        )

        debt_equity = float(
            row.get("debt_to_equity_fq", 0)
        )

        # ============================================================================
        # CATEGORY 1 : HIGH GROWTH
        # ============================================================================

        high_growth = (

            (
                qoq_sales > HIGH_GROWTH_QOQ
                or
                yoy_sales > HIGH_GROWTH_YOY
            )

            and

            (
                qoq_profit > HIGH_GROWTH_QOQ
                or
                yoy_profit > HIGH_GROWTH_YOY
            )

            and

            margin_improving
        )

        # ============================================================================
        # CATEGORY 2 : ELITE COMPOUNDER
        # ============================================================================

        elite_compounder = (

            yoy_sales > COMPOUNDER_YOY

            and

            yoy_profit > COMPOUNDER_YOY

            and

            roe >= 15

            and

            opm >= 12

            and

            (
                debt_equity <= 1.5
                or
                debt_equity == 0
            )
        )

        # ============================================================================
        # CATEGORY 3 : MATURE QUALITY
        # ============================================================================

        mature_quality = (

            roe >= 18

            and

            opm >= 15

            and

            (
                debt_equity <= 1.5
                or
                debt_equity == 0
            )

            and

            float(row["market_cap_basic"]) >= 50_000_000_000

            and

            current_profit > 0
        )

        # ============================================================================
        # FINAL FILTER
        # ============================================================================

        if not (
            high_growth
            or
            elite_compounder
            or
            mature_quality
        ):
            return None

        # ============================================================================
        # CATEGORY LABELS
        # ============================================================================

        categories = []

        if high_growth:
            categories.append("High Growth")

        if elite_compounder:
            categories.append("Elite Compounder")

        if mature_quality:
            categories.append("Mature Quality")

        category = " + ".join(categories)

        # ============================================================================
        # SCORING SYSTEM
        # ============================================================================

        score = 0

        # Growth Scores
        if yoy_sales > 0.20:
            score += 20

        if yoy_profit > 0.25:
            score += 25

        if qoq_sales > 0.10:
            score += 10

        if qoq_profit > 0.10:
            score += 15

        # Quality Scores
        if roe > 20:
            score += 15

        if opm > 15:
            score += 10

        if margin_improving:
            score += 5

        # Low Debt Bonus
        if debt_equity <= 0.5:
            score += 10

        # Mature Quality Bonus
        if mature_quality:
            score += 10

        # ============================================================================
        # FINAL OUTPUT
        # ============================================================================

        return {

            "Stock": symbol,

            "Category": category,

            "Sector": row.get("sector", "Unknown"),

            "CMP": round(close_price, 2),

            "Market Cap Cr": round(
                float(row["market_cap_basic"]) / 10_000_000,
                2
            ),

            "Avg Traded Value Cr": round(
                traded_value / 10_000_000,
                2
            ),

            "ROE %": round(roe, 2),

            "OPM %": round(opm, 2),

            "Debt/Equity": round(
                debt_equity,
                2
            ),

            "QOQ Sales %": round(
                qoq_sales * 100,
                2
            ),

            "YOY Sales %": round(
                yoy_sales * 100,
                2
            ),

            "QOQ Profit %": round(
                qoq_profit * 100,
                2
            ),

            "YOY Profit %": round(
                yoy_profit * 100,
                2
            ),

            "Margin Improving": margin_improving,

            "Fundamental Score": score,

            "Scan Time": datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        }

    except Exception:
        return None

# =====================================================================================
# MAIN
# =====================================================================================

def main():

    print("\n🚀 ELITE FUNDAMENTAL WATCHLIST SCAN STARTED\n")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ============================================================================
    # FETCH BASE UNIVERSE
    # ============================================================================

    base_df = fetch_base_universe()

    if base_df.empty:
        print("❌ No stocks fetched")
        return

    # ============================================================================
    # FUNDAMENTAL ANALYSIS
    # ============================================================================

    rows = [row for _, row in base_df.iterrows()]

    print("\n📊 Running deep fundamental analysis...\n")

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        results = list(

            tqdm(
                executor.map(analyze_stock, rows),
                total=len(rows)
            )
        )

    winners = [r for r in results if r]

    # ============================================================================
    # FINAL DATAFRAME
    # ============================================================================

    final_df = pd.DataFrame(winners)

    if final_df.empty:
        print("❌ No qualifying stocks found")
        return

    # ============================================================================
    # SORTING
    # ============================================================================

    final_df = final_df.sort_values(

        by=[
            "Fundamental Score",
            "ROE %",
            "YOY Profit %"
        ],

        ascending=False
    )

    # ============================================================================
    # SAVE FILES
    # ============================================================================

    final_df.to_csv(
        OUTPUT_CSV,
        index=False
    )

    final_df.to_parquet(
        OUTPUT_PARQUET,
        index=False
    )

    # ============================================================================
    # OUTPUT
    # ============================================================================

    print("\n========================================================")
    print(f"✅ FINAL WATCHLIST: {len(final_df)} STOCKS")
    print("========================================================\n")

    print("📊 CATEGORY BREAKDOWN\n")

    exploded = (
        final_df["Category"]
        .str.split(" + ", regex=False)
        .explode()
    )

    print(
        exploded.value_counts()
    )

    print("\n========================================================\n")

    # PRINT ALL ROWS
    pd.set_option('display.max_rows', None)
    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', None)

    print(
        final_df.to_string(index=False)
    )

    print(f"\n💾 CSV Saved      : {OUTPUT_CSV}")
    print(f"💾 Parquet Saved  : {OUTPUT_PARQUET}")

# =====================================================================================

if __name__ == "__main__":
    main()
