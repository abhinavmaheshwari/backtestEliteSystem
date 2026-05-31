# =====================================================================================
# app/daily_builder.py  —  v3 (Financial sector dual-path)
# =====================================================================================
#
# ARCHITECTURE
# ------------
# All data is fetched in ONE TradingView Screener API call — no yfinance.
# Two classification paths are applied AFTER the single fetch:
#
#   PATH A — Non-financial stocks (Manufacturing, Tech, Pharma, etc.)
#             Uses gross_profit growth + EPS growth (confirmed TV fields)
#             Gates: OPM ≥ 8%, gross_profit + EPS growth fields required
#
#   PATH B — Financial sector (Banks, NBFCs, Insurance, Holding Cos.)
#             Uses total_revenue growth + net_income growth + ROA
#             Gates: ROA ≥ 1%, OPM gate SKIPPED (banks have no OPM)
#             "Gross Profit" fields will be None for banks — intentional
#
# WHY TWO PATHS:
#   Banks/NBFCs do not report Gross Profit or Operating Margin because
#   they have no COGS. Their income statement goes directly from NII
#   (Net Interest Income) to operating expenses. TradingView correctly
#   returns None for gross_profit_* fields on these entities.
#   Using OPM to gate them is a category error — we'd be excluding
#   HDFC Bank and SBI simply because they're banks, not because they're
#   bad businesses.
#
# TV FIELD MAPPING (confirmed working)
# ─────────────────────────────────────────────────────────────────────
#   All stocks (growth):
#     total_revenue_yoy_growth_ttm        — Revenue TTM YoY %   (TV confirmed)
#     total_revenue_qoq_growth_fq         — Revenue QoQ %
#     net_income_yoy_growth_ttm           — Net Income TTM YoY %
#     net_income_qoq_growth_fq            — Net Income QoQ %
#
#   Non-financial only (used as primary growth signal for non-banks):
#     gross_profit_yoy_growth_ttm         — Gross Profit TTM YoY %
#     gross_profit_qoq_growth_fq          — Gross Profit QoQ %
#     earnings_per_share_diluted_yoy_growth_ttm
#     earnings_per_share_diluted_qoq_growth_fq
#
#   Financial only:
#     return_on_assets_fq                 — ROA (key bank quality metric)
#     (OPM skipped — not applicable to banks)
#
# NOTE on total_revenue_* vs gross_profit_*:
#   For non-financial stocks, gross_profit is the better proxy because
#   revenue can include pass-through costs (raw materials in trading cos.).
#   For banks, total_revenue ≡ NII + non-interest income, which is the
#   correct top line. We fetch both for all stocks; each path uses what
#   makes sense for that sector.
#
# =====================================================================================

import os
import traceback
import threading
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
# SECTOR ROUTING
# =====================================================================================
# TradingView sector strings for India that map to financial entities.
# These get PATH B (bank/NBFC classification logic).
# Everything else gets PATH A (standard non-financial classification).
FINANCIAL_SECTORS = {
    "Finance",
    "Banks",
    "Insurance",
    "Financial Services",
}

# =====================================================================================
# BASE FILTERS  (applied to both paths via the TV API query)
# =====================================================================================

MIN_PRICE         = 50
MIN_MARKET_CAP    = 5_000_000_000    # ₹500 Cr
MIN_TRADED_VALUE  = 100_000_000      # ₹10 Cr/day
MIN_ROE           = 10               # %  (applies to both paths)

# PATH A only
MIN_OPM_NONFIN    = 8                # %

# PATH B only
MIN_ROA_FIN       = 0.8              # %  (strong bank ROA; SBI ~1%, HDFC ~2%)

# =====================================================================================
# GROWTH THRESHOLDS  (TV returns percent values: 15.0 = 15%)
# =====================================================================================

# PATH A — non-financial
HIGH_GROWTH_YOY    = 15.0
HIGH_GROWTH_QOQ    =  5.0
COMPOUNDER_YOY     =  3.0
STEADY_YOY         = 10.0
TURNAROUND_PROFIT  = 30.0

# PATH B — financial
FIN_HIGH_GROWTH_YOY   = 15.0   # revenue YoY for a fast-growing bank/NBFC
FIN_COMPOUNDER_YOY    =  5.0   # steady NII grower
FIN_TURNAROUND_PROFIT = 25.0   # profit recovery for a bank

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
    print(f"⛔ SKIP [{symbol}]: {reason}")

# =====================================================================================
# FETCH UNIVERSE  —  single API call, ALL columns for both paths
# =====================================================================================

def fetch_universe() -> pd.DataFrame:
    """
    Single TradingView Screener call. Fetches every column needed by
    both PATH A and PATH B. The API returns None for fields that don't
    apply to a given stock's sector (e.g. gross_profit_* for banks),
    which is expected and handled in the classifier.

    The universe filter at the API level is kept broad:
      — OPM filter is NOT applied here (would incorrectly exclude banks)
      — ROE ≥ 10 still applied (universal quality gate)
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

        # quality — universal
        "return_on_equity_fy",
        "operating_margin",          # will be None for banks; used only in PATH A
        "debt_to_equity_fq",

        # quality — financial path
        "return_on_assets_fq",       # ROA: key bank quality metric

        # profitability gate (EPS > 0 means profitable)
        "earnings_per_share_basic_ttm",

        # ── PATH A growth fields (non-financial) ──────────────────────
        "gross_profit_yoy_growth_ttm",
        "gross_profit_qoq_growth_fq",
        "earnings_per_share_diluted_yoy_growth_ttm",
        "earnings_per_share_diluted_qoq_growth_fq",

        # ── PATH B growth fields (financial) + universal fallback ─────
        # total_revenue works for ALL sectors (NII for banks, revenue for others)
        # net_income works for ALL sectors
        "total_revenue_yoy_growth_ttm",
        "total_revenue_qoq_growth_fq",
        "net_income_yoy_growth_ttm",
        "net_income_qoq_growth_fq",
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
            # NOTE: OPM filter deliberately omitted — would wrongly drop banks
        )
        .limit(5000)
    )

    total, df = q.get_scanner_data()
    print(f"✅ Universe fetched: {total} stocks")
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
    """Returns a skip reason string if anomaly detected, else None."""
    if yoy_rev < MIN_YOY or yoy_profit < MIN_YOY:
        return (f"Structural collapse: "
                f"YoY Revenue={yoy_rev:.1f}%, YoY Profit={yoy_profit:.1f}%")
    if yoy_rev > MAX_YOY or yoy_profit > MAX_YOY:
        return (f"Extreme base-effect anomaly: "
                f"YoY Revenue={yoy_rev:.1f}%, YoY Profit={yoy_profit:.1f}%")
    return None

# =====================================================================================
# PATH A — NON-FINANCIAL CLASSIFICATION
# =====================================================================================

def _classify_nonfin(row: pd.Series, symbol: str) -> dict | None:
    """
    Standard classification for non-financial stocks.
    Uses gross_profit growth (primary) + EPS growth (profit proxy).
    OPM is a hard gate here because it's meaningful for these companies.
    """

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

    # ── required field check ──────────────────────────────────────────
    missing = [
        name for name, val in [
            ("close",                   close_price),
            ("average_volume_30d_calc", avg_volume),
            ("market_cap_basic",        market_cap),
            ("return_on_equity_fy",     roe),
            ("operating_margin",        opm),
            ("gross_profit_yoy_growth_ttm",               yoy_sales),
            ("gross_profit_qoq_growth_fq",                qoq_sales),
            ("earnings_per_share_diluted_yoy_growth_ttm", yoy_profit),
            ("earnings_per_share_diluted_qoq_growth_fq",  qoq_profit),
        ] if val is None
    ]
    if missing:
        return skip(f"Missing data: {', '.join(missing)}")

    # ── OPM gate (meaningful only for non-financials) ─────────────────
    # Mega-cap bypass: high-volume/low-margin businesses (retail, auto,
    # FMCG) with market cap ≥ ₹1,000 Cr are exempt from the hard OPM
    # floor — their thin margins are a structural feature, not a defect.
    # The 8% floor still applies to everyone below that size threshold.
    MEGA_CAP_BYPASS = 100_000_000_000   # ₹1,000 Cr
    is_mega_cap = (market_cap is not None and market_cap >= MEGA_CAP_BYPASS)
    if opm < MIN_OPM_NONFIN and not is_mega_cap:
        return skip(f"OPM too low: {opm:.1f}% (min {MIN_OPM_NONFIN}%)")

    # ── liquidity ─────────────────────────────────────────────────────
    traded_value = avg_volume * close_price
    if traded_value < MIN_TRADED_VALUE:
        return skip(f"Low liquidity: ₹{traded_value/1e7:.1f} Cr/day "
                    f"(min ₹{MIN_TRADED_VALUE/1e7:.0f} Cr)")

    # ── anomaly guard ─────────────────────────────────────────────────
    anomaly = _anomaly_check(symbol, yoy_sales, yoy_profit)
    if anomaly:
        return skip(anomaly)

    # ── margin signals ────────────────────────────────────────────────
    yoy_margin_expanding = (yoy_profit >= yoy_sales)
    qoq_margin_expanding = (qoq_profit > 0 and qoq_profit >= qoq_sales)
    low_debt             = (debt_equity <= 1.5 or debt_equity == 0.0)

    # ── category checks ───────────────────────────────────────────────
    # MEGA-CAP BYPASS: businesses like Titan, Maruti, DMart operate on
    # intentionally thin margins because of high-volume/low-markup models.
    # Their OPM (8-9%) is structurally low, not a sign of poor quality.
    # For market caps ≥ ₹1,000 Cr, we relax OPM gates on mature_quality
    # and let size + ROE carry the quality signal instead.

    high_growth = (
        yoy_sales  > HIGH_GROWTH_YOY
        and yoy_profit > HIGH_GROWTH_YOY
        and yoy_margin_expanding
    )
    elite_compounder = (
        yoy_sales  > COMPOUNDER_YOY
        and yoy_profit > COMPOUNDER_YOY
        and roe    >= 15
        and opm    >= 10    # relaxed from 12% — allows high-volume retail/auto
        and low_debt
    )
    mature_quality = (
        roe        >= 15    # relaxed from 18% — mega-caps earn lower headline ROE
        and low_debt
        and market_cap >= 50_000_000_000
        # MEGA-CAP BYPASS: if market cap ≥ ₹1,000 Cr, OPM rule is waived;
        # otherwise require OPM ≥ 15% as normal
        and (opm >= 15 or is_mega_cap)
    )
    turnaround = (
        yoy_profit >= TURNAROUND_PROFIT
        and yoy_margin_expanding
        and opm    >= 10
        and yoy_sales >= -20.0
        and roe    >= 10
    )
    steady_compounder = (
        yoy_sales  >= STEADY_YOY
        and yoy_profit >= STEADY_YOY
        and roe    >= 12
        and opm    >= 10
    )

    if not any([high_growth, elite_compounder, mature_quality,
                turnaround, steady_compounder]):
        return skip(
            f"No category — "
            f"YoY Sales={yoy_sales:.1f}%, QoQ Sales={qoq_sales:.1f}%, "
            f"YoY Profit={yoy_profit:.1f}%, QoQ Profit={qoq_profit:.1f}%, "
            f"ROE={roe:.1f}%, OPM={opm:.1f}%"
        )

    cats = []
    if high_growth:       cats.append("High Growth")
    if elite_compounder:  cats.append("Elite Compounder")
    if mature_quality:    cats.append("Mature Quality")
    if turnaround:        cats.append("Turnaround")
    if steady_compounder: cats.append("Steady Compounder")

    score = _score_nonfin(yoy_sales, yoy_profit, qoq_sales, qoq_profit,
                          roe, opm, debt_equity, yoy_margin_expanding,
                          qoq_margin_expanding, mature_quality, elite_compounder,
                          turnaround)

    return _build_row(
        symbol       = symbol,
        cats         = cats,
        path         = "Non-Financial",
        row          = row,
        close_price  = close_price,
        market_cap   = market_cap,
        roe          = roe,
        opm          = opm,
        debt_equity  = debt_equity,
        debt_missing = debt_missing,
        qoq_rev      = qoq_sales,
        yoy_rev      = yoy_sales,
        qoq_profit   = qoq_profit,
        yoy_profit   = yoy_profit,
        score        = score,
    )

# =====================================================================================
# PATH B — FINANCIAL CLASSIFICATION  (Banks, NBFCs, Insurance)
# =====================================================================================

def _classify_fin(row: pd.Series, symbol: str) -> dict | None:
    """
    Bank/NBFC/Insurance classification.

    What changes vs PATH A:
    ─────────────────────────────────────────────────────────────────
    METRIC          PATH A (non-fin)          PATH B (financial)
    ─────────────────────────────────────────────────────────────────
    Revenue proxy   gross_profit_yoy          total_revenue_yoy (NII)
    Profit proxy    EPS diluted yoy           net_income_yoy
    Margin quality  OPM ≥ 8%  (hard gate)     SKIPPED (not applicable)
    Extra quality   —                          ROA ≥ 0.8% (hard gate)
    Debt/Equity     Used for Compounder gate   SKIPPED (banks are levered
                                               by design; D/E is meaningless)
    ─────────────────────────────────────────────────────────────────

    Categories for financial path:
      Financial High Growth    — NII YoY ≥15% + net income YoY ≥15%
      Financial Compounder     — steady NII growth ≥5% + ROE ≥15% + ROA ≥1%
      Financial Mature Quality — large-cap bank with ROE ≥15% + ROA ≥1%
      Financial Turnaround     — profit recovery ≥25% YoY with stable NII
    """

    def fv(c): return _fval(row, c)
    def skip(r): return log_exclusion(symbol, r) or None

    close_price = fv("close")
    avg_volume  = fv("average_volume_30d_calc")
    market_cap  = fv("market_cap_basic")
    roe         = fv("return_on_equity_fy")
    roa         = fv("return_on_assets_fq")

    # Debt/equity kept for display only — not used in gates for financials
    _raw_de      = fv("debt_to_equity_fq")
    debt_equity  = _raw_de if _raw_de is not None else 0.0
    debt_missing = _raw_de is None

    # Revenue = NII + non-interest income for banks
    yoy_rev    = fv("total_revenue_yoy_growth_ttm")
    qoq_rev    = fv("total_revenue_qoq_growth_fq")
    yoy_profit = fv("net_income_yoy_growth_ttm")
    qoq_profit = fv("net_income_qoq_growth_fq")

    # ── required field check ──────────────────────────────────────────
    missing = [
        name for name, val in [
            ("close",                         close_price),
            ("average_volume_30d_calc",        avg_volume),
            ("market_cap_basic",               market_cap),
            ("return_on_equity_fy",            roe),
            ("return_on_assets_fq",            roa),
            ("total_revenue_yoy_growth_ttm",   yoy_rev),
            ("total_revenue_qoq_growth_fq",    qoq_rev),
            ("net_income_yoy_growth_ttm",      yoy_profit),
            ("net_income_qoq_growth_fq",       qoq_profit),
        ] if val is None
    ]
    if missing:
        return skip(f"Missing data (financial path): {', '.join(missing)}")

    # ── ROA gate — the single most important bank quality metric ──────
    if roa < MIN_ROA_FIN:
        return skip(f"ROA too low: {roa:.2f}% (min {MIN_ROA_FIN}%)")

    # ── liquidity ─────────────────────────────────────────────────────
    traded_value = avg_volume * close_price
    if traded_value < MIN_TRADED_VALUE:
        return skip(f"Low liquidity: ₹{traded_value/1e7:.1f} Cr/day "
                    f"(min ₹{MIN_TRADED_VALUE/1e7:.0f} Cr)")

    # ── anomaly guard ─────────────────────────────────────────────────
    anomaly = _anomaly_check(symbol, yoy_rev, yoy_profit)
    if anomaly:
        return skip(anomaly)

    # ── margin signal (revenue vs profit trend) ───────────────────────
    yoy_margin_expanding = (yoy_profit >= yoy_rev)

    # ── financial categories ──────────────────────────────────────────
    fin_high_growth = (
        yoy_rev    > FIN_HIGH_GROWTH_YOY
        and yoy_profit > FIN_HIGH_GROWTH_YOY
        and yoy_margin_expanding
    )
    fin_compounder = (
        yoy_rev    > FIN_COMPOUNDER_YOY
        and yoy_profit > FIN_COMPOUNDER_YOY
        and roe    >= 15
        and roa    >= 1.0
    )
    fin_mature_quality = (
        roe        >= 15
        and roa    >= 1.0
        and market_cap >= 50_000_000_000
    )
    fin_turnaround = (
        yoy_profit >= FIN_TURNAROUND_PROFIT
        and yoy_margin_expanding
        and yoy_rev  >= -10.0       # NII shouldn't be collapsing
        and roe    >= 10
        and roa    >= 0.8
    )

    if not any([fin_high_growth, fin_compounder,
                fin_mature_quality, fin_turnaround]):
        return skip(
            f"No financial category — "
            f"YoY NII={yoy_rev:.1f}%, QoQ NII={qoq_rev:.1f}%, "
            f"YoY Profit={yoy_profit:.1f}%, QoQ Profit={qoq_profit:.1f}%, "
            f"ROE={roe:.1f}%, ROA={roa:.2f}%"
        )

    cats = []
    if fin_high_growth:    cats.append("Financial High Growth")
    if fin_compounder:     cats.append("Financial Compounder")
    if fin_mature_quality: cats.append("Financial Mature Quality")
    if fin_turnaround:     cats.append("Financial Turnaround")

    score = _score_fin(yoy_rev, yoy_profit, qoq_rev, qoq_profit,
                       roe, roa, yoy_margin_expanding,
                       fin_mature_quality, fin_compounder)

    # OPM is None for banks — display "N/A" stored as -1 sentinel
    return _build_row(
        symbol       = symbol,
        cats         = cats,
        path         = "Financial",
        row          = row,
        close_price  = close_price,
        market_cap   = market_cap,
        roe          = roe,
        opm          = None,         # not applicable
        debt_equity  = debt_equity,
        debt_missing = debt_missing,
        qoq_rev      = qoq_rev,
        yoy_rev      = yoy_rev,
        qoq_profit   = qoq_profit,
        yoy_profit   = yoy_profit,
        score        = score,
        roa          = roa,
    )

# =====================================================================================
# SCORING
# =====================================================================================

def _score_nonfin(yoy_sales, yoy_profit, qoq_sales, qoq_profit,
                  roe, opm, debt_equity, yoy_margin, qoq_margin,
                  mature_quality, elite_compounder, turnaround) -> int:
    score = 0
    if yoy_sales   >= 20:  score += 20
    elif yoy_sales >= 10:  score += 10
    if yoy_profit  >= 25:  score += 25
    elif yoy_profit >= 10: score += 12
    if qoq_sales   >= 10:  score += 8
    elif qoq_sales >=  5:  score += 4
    if qoq_profit  >= 10:  score += 12
    elif qoq_profit >=  5: score += 6
    if roe  >= 25:  score += 15
    elif roe >= 20: score += 10
    elif roe >= 15: score += 5
    if opm  >= 20:  score += 10
    elif opm >= 15: score += 7
    elif opm >= 10: score += 3
    if yoy_margin:  score += 5
    if qoq_margin:  score += 3
    if debt_equity == 0.0 or debt_equity <= 0.1: score += 10
    elif debt_equity <= 0.5:                      score += 7
    elif debt_equity <= 1.0:                      score += 3
    if mature_quality:    score += 10
    if elite_compounder:  score += 5
    if turnaround:        score += 3
    return score


def _score_fin(yoy_rev, yoy_profit, qoq_rev, qoq_profit,
               roe, roa, yoy_margin, fin_mature, fin_compounder) -> int:
    score = 0
    if yoy_rev    >= 20:  score += 20
    elif yoy_rev  >= 10:  score += 10
    elif yoy_rev  >=  5:  score += 5
    if yoy_profit >= 25:  score += 25
    elif yoy_profit >= 15: score += 15
    elif yoy_profit >= 5:  score += 8
    if qoq_rev    >= 10:  score += 8
    elif qoq_rev  >=  5:  score += 4
    if qoq_profit >= 10:  score += 12
    elif qoq_profit >= 5: score += 6
    if roe  >= 20:  score += 15
    elif roe >= 15: score += 10
    elif roe >= 12: score += 5
    if roa  >= 2.0: score += 15     # exceptional bank ROA
    elif roa >= 1.5: score += 10
    elif roa >= 1.0: score += 5
    if yoy_margin:  score += 5
    if fin_mature:  score += 10
    if fin_compounder: score += 5
    return score

# =====================================================================================
# ROW BUILDER (shared)
# =====================================================================================

def _build_row(*, symbol, cats, path, row, close_price, market_cap,
               roe, opm, debt_equity, debt_missing, qoq_rev, yoy_rev,
               qoq_profit, yoy_profit, score, roa=None) -> dict:
    return {
        "Stock":             symbol,
        "Category":          " + ".join(cats),
        "Path":              path,
        "Sector":            row.get("sector", "Unknown"),
        "CMP":               round(close_price, 2),
        "Market Cap Cr":     round(market_cap / 10_000_000, 2),
        "ROE %":             round(roe, 2),
        "ROA %":             round(roa, 2) if roa is not None else None,
        "OPM %":             round(opm, 2) if opm is not None else None,
        "Debt/Equity":       round(debt_equity, 2),
        "D/E Missing":       debt_missing,
        "QOQ Revenue %":     round(qoq_rev,    2),
        "YOY Revenue %":     round(yoy_rev,    2),
        "QOQ Profit %":      round(qoq_profit, 2),
        "YOY Profit %":      round(yoy_profit, 2),
        "Fundamental Score": score,
        "Scan Time":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
        tb = traceback.format_exc()
        log_exclusion(symbol, f"Unhandled exception: {e}\n{tb}")
        print(f"❌ EXCEPTION [{symbol}]: {e}\n{tb}")
        return None

# =====================================================================================
# MAIN
# =====================================================================================

def main():
    with _exclusion_lock:
        EXCLUSION_LOG.clear()  # Reset on every run — thread-safe clear
    # when build_watchlist() is called repeatedly in the same long-running process.
    print("\n🚀 ELITE FUNDAMENTAL SCAN STARTED\n")

    os.makedirs(os.path.dirname(OUTPUT_PARQUET), exist_ok=True)

    universe_df = fetch_universe()

    if universe_df.empty:
        print("❌ No stocks returned from TradingView")
        return

    # Show sector routing breakdown before classifying
    fin_mask = universe_df["sector"].isin(FINANCIAL_SECTORS)
    print(f"\n📊 Classifying {len(universe_df)} stocks...")
    print(f"   └─ PATH A (Non-Financial): {(~fin_mask).sum()} stocks")
    print(f"   └─ PATH B (Financial):     {fin_mask.sum()} stocks\n")

    results = [classify_stock(row) for _, row in universe_df.iterrows()]
    winners = [r for r in results if r is not None]

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

    # ── summary breakdown by path ──────────────────────────────────────
    print("\n================================================")
    print(f"✅ FINAL WATCHLIST: {len(final_df)} stocks")
    print("================================================")
    for path, group in final_df.groupby("Path"):
        print(f"\n  {path} ({len(group)} stocks):")
        for cat, sub in group.groupby("Category"):
            print(f"    {cat}: {len(sub)}")

    print("\n── Top 20 ──────────────────────────────────────\n")
    print(final_df.head(20).to_string(index=False))
    print(f"\n💾 CSV Saved:     {OUTPUT_CSV}")
    print(f"💾 PARQUET Saved: {OUTPUT_PARQUET}")

# =====================================================================================
# ALIAS — main.py and scanners import build_watchlist(); this is the same function.

build_watchlist = main

# =====================================================================================

if __name__ == "__main__":
    main()
