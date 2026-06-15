# =====================================================================================
# app/sector_rotation.py
# SECTOR ROTATION ENGINE
#
# WHAT THIS FILE DOES:
#   Computes relative strength of each NSE sector vs the Nifty 50 benchmark.
#   Called ONCE per scan attempt (outside the per-stock loop) to build a
#   sector → RS classification map.  Each stock in the watchlist is then looked
#   up by symbol at zero extra cost — no additional downloads per stock.
#
# DESIGN PRINCIPLES:
#   • NEVER hard-filters stocks. Rotation status is a score modifier only.
#   • Fully graceful degradation.
#   • Sector lookup is symbol-driven, not category-driven.
#
# CHANGELOG (2026-06-01 round 1):
#   Fixed 7 delisted ETF tickers discovered via yfinance download errors:
#   • Realty:       NIFTYREALTY.NS   → MOREALTY.NS   (Motilal Oswal Nifty Realty ETF)
#   • Energy:       ENERGYIETF.NS    → ENERGYBEES.NS  (delisted in round 2, see below)
#   • Defence:      DEFEFIETF.NS     → MODEFENCE.NS   (Motilal Oswal Nifty India Defence ETF)
#   • MNC:          MNCIETF.NS       → MOMNC.NS       (Motilal Oswal Nifty MNC ETF)
#   • Capital Goods:CAPIETF.NS       → INFRAIETF.NS   (proxy; no dedicated ETF exists on NSE)
#   • Chemicals:    CHEMIETF.NS      → CHEMICAL.NS    (Kotak Nifty Chemicals ETF, launched Nov 2025)
#   • Electronics:  MANIETF.NS       → MOFMANIETF.NS  (delisted in round 2, see below)
#
# CHANGELOG (2026-06-01 round 2):
#   Fixed 2 more tickers that also failed at runtime:
#   • Energy:       ENERGYBEES.NS    → OILIETF.NS     (ICICI Prudential Nifty Oil & Gas ETF)
#   • Electronics:  MOFMANIETF.NS    → MAMFGETF.NS    (Mirae Asset Nifty India Manufacturing ETF)
#
# CHANGELOG (2026-06-01 round 4):
#   Removed Electronics from SECTOR_ETF_MAP:
#   • MAMFGETF.NS fails at runtime with YFRateLimitError / no data every cycle.
#     Both MOFMANIETF.NS and MAMFGETF.NS are now confirmed unreliable.
#     Sector omitted until a stable NSE-listed ETF with ≥6 months history is found.
#     Graceful degradation handles the missing sector — no scoring impact.
#
# CHANGELOG (2026-06-01 round 3):
#   Fixed duplicate-ticker bug in batch download:
#   • Infrastructure / Capital Goods / Railways all mapped to INFRAIETF.NS,
#     causing the same ticker to appear 3× in the yfinance batch string.
#   • all_tickers is now deduplicated before the batch call; close_map is then
#     shared across all sectors that proxy to the same ETF — no double-download,
#     no MultiIndex parse confusion.
# =====================================================================================

import logging
import yfinance as yf
from data_fetch_status import mark_success, mark_failure
import pandas as pd

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST    = ZoneInfo("Asia/Kolkata")

# =====================================================================================
# SECTOR ETF MAP
# =====================================================================================

SECTOR_ETF_MAP: dict[str, str] = {
    "IT":             "ITETF.NS",
    "Pharma":         "PHARMABEES.NS",      # Nippon India ETF Pharma BeES
    "Banking":        "BANKBEES.NS",
    "FMCG":           "FMCGIETF.NS",
    "Auto":           "AUTOIETF.NS",
    "Metal":          "METALIETF.NS",
    "Realty":         "MOREALTY.NS",        # FIXED: NIFTYREALTY.NS delisted → Motilal Oswal Nifty Realty ETF
    "Energy":         "OILIETF.NS",         # FIXED: ENERGYIETF.NS → ENERGYBEES.NS (delisted) → ICICI Prudential Nifty Oil & Gas ETF
    "Infrastructure": "INFRAIETF.NS",
    "PSU Bank":       "PSUBNKIETF.NS",
    "Defence":        "MODEFENCE.NS",       # FIXED: DEFEFIETF.NS delisted → Motilal Oswal Nifty India Defence ETF
    "MNC":            "MOMNC.NS",           # FIXED: MNCIETF.NS delisted → Motilal Oswal Nifty MNC ETF
    "Consumption":    "CONSUMBEES.NS",      # Nippon India ETF Consumption
    "Financials":     "FINIETF.NS",
    "Capital Goods":  "INFRAIETF.NS",       # FIXED: CAPIETF.NS delisted → no dedicated ETF; Infra proxy
    "Chemicals":      "CHEMICAL.NS",        # FIXED: CHEMIETF.NS delisted → Kotak Nifty Chemicals ETF (Nov 2025)
    # NOTE: CHEMICAL.NS launched Nov 2025 — only ~6 months history. Will be skipped if
    #       bars < MIN_BARS_REQUIRED. Graceful degradation applies.
    "Railways":       "INFRAIETF.NS",       # Proxy — shares ETF with Infrastructure
    # "Electronics" removed — MAMFGETF.NS fails at runtime (delisted/illiquid). Re-add when a stable ETF is available.
}

BENCHMARK_TICKER       = "^NSEI"
RS_LOOKBACK_DAYS       = 20
MOMENTUM_LOOKBACK_DAYS = 5
DOWNLOAD_PERIOD        = "60d"
MIN_BARS_REQUIRED      = RS_LOOKBACK_DAYS + MOMENTUM_LOOKBACK_DAYS + 5
HIGHLIGHT_THRESHOLD_PCT = 5.0

# =====================================================================================
# NSE SYMBOL → SECTOR MAP (SYNCHRONIZED WITH DAILY_BUILDER)
# =====================================================================================

NSE_SECTOR_MAP: dict[str, str] = {
    # ── IT ───────────────────────────────────────────────────────────────────────────
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT", "TECHM": "IT",
    "LTIM": "IT", "MPHASIS": "IT", "PERSISTENT": "IT", "COFORGE": "IT",
    "OFSS": "IT", "KPITTECH": "IT", "TATAELXSI": "IT", "MASTEK": "IT",
    "HEXAWARE": "IT", "NIITTECH": "IT", "RATEGAIN": "IT", "NEWGEN": "IT",
    "ZENSARTECH": "IT", "BIRLASOFT": "IT",
    "SONATSOFTW": "IT", "HAPPSTMNDS": "IT", "INTELLECT": "IT",
    "TANLA": "IT", "ECLERX": "IT", "ROUTE": "IT",
    "DATAMATICS": "IT", "CYIENT": "IT",

    # ── Pharma ───────────────────────────────────────────────────────────────────────
    "SUNPHARMA": "Pharma", "DRREDDY": "Pharma", "CIPLA": "Pharma",
    "DIVISLAB": "Pharma", "AUROPHARMA": "Pharma", "TORNTPHARM": "Pharma",
    "ALKEM": "Pharma", "LUPIN": "Pharma", "ABBOTINDIA": "Pharma",
    "GLAXO": "Pharma", "PFIZER": "Pharma", "SANOFI": "Pharma",
    "LALPATHLAB": "Pharma", "METROPOLIS": "Pharma", "POLYMED": "Pharma",
    "GRANULES": "Pharma", "AJANTPHARM": "Pharma", "NATCOPHARM": "Pharma",
    "IPCALAB": "Pharma", "GLAND": "Pharma", "ERIS": "Pharma",
    "SYNGENE": "Pharma", "LAURUSLABS": "Pharma",
    "BIOCON": "Pharma", "ZYDUSLIFE": "Pharma",
    "CAPLIPOINT": "Pharma", "SUVENPHAR": "Pharma",
    "WOCKPHARMA": "Pharma", "KIMS": "Pharma",
    "MEDANTA": "Pharma", "RAINBOW": "Pharma",
    "VIJAYA": "Pharma", "THYROCARE": "Pharma",
    "STARHEALTH": "Pharma",

    # ── Banking ───────────────────────────────────────────────────────────────────────
    "HDFCBANK": "Banking", "ICICIBANK": "Banking", "KOTAKBANK": "Banking",
    "AXISBANK": "Banking", "INDUSINDBK": "Banking", "FEDERALBNK": "Banking",
    "BANDHANBNK": "Banking", "IDFCFIRSTB": "Banking", "RBLBANK": "Banking",
    "AUBANK": "Banking", "YESBANK": "Banking", "DCBBANK": "Banking",
    "KARNATAKA": "Banking", "CSBBANK": "Banking",
    "CUB": "Banking", "KVB": "Banking",

    # ── PSU Bank ──────────────────────────────────────────────────────────────────────
    "SBIN": "PSU Bank", "BANKBARODA": "PSU Bank", "PNB": "PSU Bank",
    "CANBK": "PSU Bank", "UNIONBANK": "PSU Bank", "INDIANB": "PSU Bank",
    "BANKINDIA": "PSU Bank", "MAHABANK": "PSU Bank", "IOB": "PSU Bank",
    "UCOBANK": "PSU Bank", "CENTRALBK": "PSU Bank",

    # ── Financials ───────────────────────────────────────────────────────────────────
    "BAJFINANCE": "Financials", "BAJAJFINSV": "Financials",
    "CHOLAFIN": "Financials", "SHRIRAMFIN": "Financials",
    "MUTHOOTFIN": "Financials", "MANAPPURAM": "Financials",
    "LICHSGFIN": "Financials", "HDFCAMC": "Financials",
    "NAM-INDIA": "Financials", "360ONE": "Financials",
    "ANGELONE": "Financials", "MOTILALOFS": "Financials",
    "CDSL": "Financials", "BSE": "Financials",
    "KFINTECH": "Financials", "MCX": "Financials",
    "SBICARD": "Financials",

    # ── FMCG ──────────────────────────────────────────────────────────────────────────
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG", "DABUR": "FMCG", "MARICO": "FMCG",
    "COLPAL": "FMCG", "GODREJCP": "FMCG", "EMAMILTD": "FMCG",
    "VBL": "FMCG", "UBL": "FMCG", "MCDOWELL-N": "FMCG",
    "RADICO": "FMCG", "TATACONSUM": "FMCG", "JYOTHYLAB": "FMCG",
    "PAGEIND": "FMCG",

    # ── Auto ──────────────────────────────────────────────────────────────────────────
    "MARUTI": "Auto", "TATAMOTORS": "Auto", "M&M": "Auto",
    "BAJAJ-AUTO": "Auto", "HEROMOTOCO": "Auto", "EICHERMOT": "Auto",
    "TVSMOTORS": "Auto", "ASHOKLEY": "Auto", "TIINDIA": "Auto",
    "MOTHERSON": "Auto", "BOSCHLTD": "Auto", "BHARATFORG": "Auto",
    "BALKRISIND": "Auto", "APOLLOTYRE": "Auto", "MRF": "Auto",
    "CEATLTD": "Auto", "EXIDEIND": "Auto", "AMARARAJA": "Auto",
    "SUNDRMFAST": "Auto", "SUBROS": "Auto",
    "SONACOMS": "Auto", "UNOMINDA": "Auto",
    "SUPRAJIT": "Auto", "ENDURANCE": "Auto",
    "GABRIEL": "Auto", "SCHAEFFLER": "Auto",
    "FIEMIND": "Auto", "OLECTRA": "Auto",
    "GREAVESCOT": "Auto",

    # ── Metal ─────────────────────────────────────────────────────────────────────────
    "TATASTEEL": "Metal", "JSWSTEEL": "Metal", "SAIL": "Metal",
    "HINDALCO": "Metal", "VEDL": "Metal", "NMDC": "Metal",
    "NATIONALUM": "Metal", "APLAPOLLO": "Metal", "RATNAMANI": "Metal",
    "WELSPUNLIV": "Metal", "JINDALSTEL": "Metal", "JSWISPL": "Metal",
    "HINDZINC": "Metal", "MOIL": "Metal", "SHYAMMETL": "Metal",

    # ── Energy ───────────────────────────────────────────────────────────────────────
    "RELIANCE": "Energy", "ONGC": "Energy", "BPCL": "Energy",
    "IOC": "Energy", "HINDPETRO": "Energy", "GAIL": "Energy",
    "PETRONET": "Energy", "NTPC": "Energy", "POWERGRID": "Energy",
    "TATAPOWER": "Energy", "ADANIPOWER": "Energy",
    "ADANIGREEN": "Energy", "TORNTPOWER": "Energy",
    "CESC": "Energy", "JSWENERGY": "Energy",
    "NHPC": "Energy", "SJVN": "Energy",
    "IREDA": "Energy", "SUZLON": "Energy",
    "KPIGREEN": "Energy", "INDOXWIND": "Energy",
    "WAAREEENER": "Energy", "INOXGREEN": "Energy",

    # ── Realty ───────────────────────────────────────────────────────────────────────
    "DLF": "Realty", "GODREJPROP": "Realty",
    "OBEROIRLTY": "Realty", "PRESTIGE": "Realty",
    "BRIGADE": "Realty", "SOBHA": "Realty",
    "PHOENIXLTD": "Realty", "MAHLIFE": "Realty",
    "LODHA": "Realty", "SUNTECK": "Realty",
    "KOLTEPATIL": "Realty", "ARVIND": "Realty",

    # ── Infrastructure ────────────────────────────────────────────────────────────────
    "LT": "Infrastructure", "LTTS": "Infrastructure",
    "IRCON": "Infrastructure", "RVNL": "Infrastructure",
    "IRFC": "Infrastructure", "RECLTD": "Infrastructure",
    "PFC": "Infrastructure", "ADANIPORTS": "Infrastructure",
    "GMRINFRA": "Infrastructure", "AIAENGLTD": "Infrastructure",
    "CUMMINSIND": "Infrastructure", "ABB": "Infrastructure",
    "SIEMENS": "Infrastructure", "HAVELLS": "Infrastructure",
    "KEI": "Infrastructure", "POLYCAB": "Infrastructure",
    "KALPATPOWR": "Infrastructure", "KEC": "Infrastructure",
    "ENGINERSIN": "Infrastructure", "NBCC": "Infrastructure",
    "PSPPROJECT": "Infrastructure",

    # ── Capital Goods ────────────────────────────────────────────────────────────────
    "SKFINDIA": "Capital Goods",
    "THERMAX": "Capital Goods",
    "KAYNES": "Capital Goods",
    "DIXON": "Capital Goods",
    "SYRMA": "Capital Goods",
    "CGPOWER": "Capital Goods",
    "VOLTAS": "Capital Goods",
    "BLUESTARCO": "Capital Goods",

    # ── Railways ─────────────────────────────────────────────────────────────────────
    "RAILTEL": "Railways",
    "TITAGARH": "Railways",
    "TEXRAIL": "Railways",
    "JWL": "Railways",
    "CONCOR": "Railways",

    # ── Defence ──────────────────────────────────────────────────────────────────────
    "HAL": "Defence", "BEL": "Defence",
    "COCHINSHIP": "Defence", "MAZDOCK": "Defence",
    "GRSE": "Defence", "MIDHANI": "Defence",
    "PARAS": "Defence", "DATAPATTNS": "Defence",
    "BHEL": "Defence", "BEML": "Defence",
    "ASTRA": "Defence", "MTAR": "Defence",
    "BDL": "Defence", "ZENTECH": "Defence",
    "IDEAFORGE": "Defence",
    "DCXINDIA": "Defence",
    "SOLARINDS": "Defence",
    "CYIENTDLM": "Defence",

    # ── MNC ───────────────────────────────────────────────────────────────────────────
    "ASIANPAINT": "MNC", "PIDILITIND": "MNC",
    "3MINDIA": "MNC", "HONAUT": "MNC",
    "SCHNEIDER": "MNC", "GILLETTE": "MNC",

    # ── Consumption ──────────────────────────────────────────────────────────────────
    "DMART": "Consumption", "TRENT": "Consumption",
    "ZOMATO": "Consumption", "NYKAA": "Consumption",
    "INDIAMART": "Consumption", "IRCTC": "Consumption",
    "JUBLFOOD": "Consumption", "DEVYANI": "Consumption",
    "SAPPHIRE": "Consumption", "WESTLIFE": "Consumption",
    "BARBEQUE": "Consumption", "EASEMYTRIP": "Consumption",
    "ABFRL": "Consumption", "VMART": "Consumption",
    "SHOPERSTOP": "Consumption", "ETHOSLTD": "Consumption",
    "MANYAVAR": "Consumption", "REDTAPE": "Consumption",
    "MEDPLUS": "Consumption",

    # ── Chemicals ────────────────────────────────────────────────────────────────────
    "DEEPAKNTR": "Chemicals",
    "SRF": "Chemicals",
    "NAVINFLUOR": "Chemicals",
    "FLUOROCHEM": "Chemicals",
    "TATACHEM": "Chemicals",
    "AARTIIND": "Chemicals",
    "ALKYLAMINE": "Chemicals",

    # ── Telecom ──────────────────────────────────────────────────────────────────────
    "BHARTIARTL": "Telecom",
    "INDUSTOWER": "Telecom",
    "TEJASNET": "Telecom",
    "HFCL": "Telecom",

    # ── Electronics ──────────────────────────────────────────────────────────────────
    "PGEL": "Electronics",
    "AVALON": "Electronics",
}

# =====================================================================================
# TV_SECTOR_TO_ROTATION
#
# TradingView Screener returns sector names that do NOT match SECTOR_ETF_MAP keys.
# Example: TV returns "Technology" but our ETF map key is "IT".
#
# This dict translates TV sector strings → SECTOR_ETF_MAP keys so that stocks
# whose sector comes directly from the watchlist parquet (built by daily_builder.py)
# can be matched correctly without relying on NSE_SECTOR_MAP symbol lookups.
#
# Unmapped TV sectors produce no bonus (return 0) — safe and explicit.
# =====================================================================================

TV_SECTOR_TO_ROTATION: dict[str, str] = {
    # Technology
    "Technology":               "IT",
    "Software":                 "IT",

    # Healthcare / Pharma
    "Health Technology":        "Pharma",
    "Health Services":          "Pharma",
    "Pharmaceuticals":          "Pharma",
    "Healthcare":               "Pharma",

    # Banking
    "Banks":                    "Banking",
    "Commercial Banks":         "Banking",
    "Public Sector Banks":      "PSU Bank",
    "PSU Banks":                "PSU Bank",

    # Finance / NBFC / Insurance
    "Finance":                  "Financials",
    "Financial Services":       "Financials",
    "Insurance":                "Financials",
    "Diversified Financials":   "Financials",

    # FMCG / Consumer
    "Consumer Non-Durables":    "FMCG",
    "Food & Beverages":         "FMCG",
    "Beverages":                "FMCG",
    "Tobacco":                  "FMCG",
    "Household Products":       "FMCG",

    # Auto / Ancillaries
    "Producer Manufacturing":   "Auto",
    "Consumer Durables":        "Auto",
    "Automobiles":              "Auto",
    "Auto Components":          "Auto",

    # Metals / Mining
    "Non-Energy Minerals":      "Metal",
    "Metals & Mining":          "Metal",
    "Steel":                    "Metal",
    "Aluminum":                 "Metal",

    # Energy / Power / Oil
    "Energy Minerals":          "Energy",
    "Oil & Gas":                "Energy",
    "Utilities":                "Energy",
    "Power":                    "Energy",
    "Renewable Energy":         "Energy",

    # Infrastructure / Capital Goods / Engineering
    "Industrial Services":      "Infrastructure",
    "Transportation":           "Infrastructure",
    "Engineering":              "Infrastructure",
    "Construction":             "Infrastructure",

    # Realty
    "Real Estate":              "Realty",
    "Real Estate Investment Trusts": "Realty",

    # Chemicals
    "Process Industries":       "Chemicals",
    "Chemicals":                "Chemicals",
    "Specialty Chemicals":      "Chemicals",

    # Telecom
    "Communications":           "Telecom",
    "Telecommunication Services": "Telecom",
    "Telecom":                  "Telecom",

    # Retail / Consumption
    "Retail Trade":             "Consumption",
    "Consumer Services":        "Consumption",
    "Food Service":             "Consumption",

    # Electronics / EMS
    "Electronic Technology":    "Electronics",
    "Electronics":              "Electronics",
    "Semiconductors":           "Electronics",

    # Capital Goods (direct match)
    "Capital Goods":            "Capital Goods",
    "Electrical Equipment":     "Capital Goods",
    "Industrial Machinery":     "Capital Goods",

    # Defence
    "Defence":                  "Defence",
    "Aerospace & Defence":      "Defence",

    # MNC (no direct TV equivalent — left unmapped intentionally)
}

# =====================================================================================
# DATA CLASSES
# =====================================================================================

@dataclass
class SectorScore:
    name:               str
    etf_ticker:         str
    rs_value:           float    # sector_return / nifty_return  (1.0 = inline with Nifty)
    rs_momentum:        float    # RS change over MOMENTUM_LOOKBACK_DAYS (+ve = accelerating)
    outperformance_pct: float    # (rs_value - 1) * 100  — human-readable
    classification:     str      # LEADING / IMPROVING / WEAKENING / LAGGING
    sector_return_pct:  float
    nifty_return_pct:   float
    bars_available:     int


@dataclass
class SectorRotationResult:
    scores:           dict[str, SectorScore]   # sector_name → SectorScore
    strong_sectors:   set[str]                 # LEADING + IMPROVING
    weak_sectors:     set[str]                 # WEAKENING + LAGGING
    scan_date:        date
    nifty_return_pct: float
    errors:           list[str] = field(default_factory=list)

    def score_bonus_for(self, tv_sector: str) -> int:
        """
        Returns the sector rotation score bonus (or penalty) for a given TV sector string.

        Parameters
        ----------
        tv_sector : str
            TradingView sector string from the watchlist parquet (e.g. "Technology",
            "Finance"). Translated via TV_SECTOR_TO_ROTATION before matching.
            Pass str(sector) if sector else "Unknown" from scanner call sites.

        Scanners call:
            rotation_result.score_bonus_for(safe_sector)
        """
        return get_sector_score_bonus("", self, sector=tv_sector)


# =====================================================================================
# INTERNAL HELPERS
# =====================================================================================

def _parse_close_series(df: pd.DataFrame, label: str) -> Optional[pd.Series]:
    """Extract a Close price series from a yfinance DataFrame (flat or MultiIndex)."""
    if df is None or df.empty:
        return None
    df = df.copy()
    df.reset_index(inplace=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.strip() for c in df.columns]
    date_col = next((c for c in ["Date", "Datetime", "index"] if c in df.columns), None)
    if date_col is None or "Close" not in df.columns:
        return None
    df[date_col] = pd.to_datetime(df[date_col])
    series = df.sort_values(date_col).set_index(date_col)["Close"].dropna()
    if isinstance(series, pd.DataFrame):
        series = series.iloc[:, 0]
    return series.astype(float)


def _batch_download_closes(tickers: list[str]) -> dict[str, Optional[pd.Series]]:
    """
    Download all tickers in ONE yfinance batch call.

    Deduplicates tickers before the request so that sectors sharing a proxy ETF
    (e.g. Infrastructure / Capital Goods / Railways all → INFRAIETF.NS) don't
    cause the same symbol to appear multiple times in the batch string, which
    confuses yfinance's MultiIndex column parsing.

    Returns {ticker_symbol: close_series_or_None} for every requested ticker,
    including duplicates (they all point to the same Series object).
    """
    results: dict[str, Optional[pd.Series]] = {}

    if not tickers:
        return results

    # Deduplicate while preserving order; batch only unique tickers.
    seen: set[str] = set()
    unique_tickers: list[str] = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            unique_tickers.append(t)

    tickers_str = " ".join(unique_tickers)
    logger.info(
        f"🔄 Sector rotation: batch downloading {len(unique_tickers)} unique tickers "
        f"({len(tickers)} requested, {len(tickers) - len(unique_tickers)} deduped) "
        f"| period={DOWNLOAD_PERIOD}"
    )

    try:
        raw = yf.download(
            tickers_str,
            period=DOWNLOAD_PERIOD,
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=False,
            group_by="ticker",
        )

        if raw is None or raw.empty:
            logger.warning("⚠️  Sector batch download returned empty — all sectors will be skipped")
            try:
                mark_failure('yfinance', 'Sector batch returned empty')
            except Exception:
                logger.exception("Failed to report yfinance failure for sector rotation")
            for t in tickers:
                results[t] = None
            return results

        if not isinstance(raw.columns, pd.MultiIndex):
            # Only one ticker survived — yfinance returns a flat DF.
            if len(unique_tickers) == 1:
                series = _parse_close_series(raw, unique_tickers[0])
                for t in tickers:                  # covers duplicates too
                    results[t] = series
            else:
                logger.warning(
                    "⚠️  Sector batch returned flat DF for multi-ticker request "
                    "— skipping to avoid symbol→data mismatch"
                )
                for t in tickers:
                    results[t] = None
            return results

        # MultiIndex: columns are (Ticker, OHLCV).
        # Build unique results first, then fan out to duplicates.
        unique_results: dict[str, Optional[pd.Series]] = {}
        level0 = raw.columns.get_level_values(0)
        for t in unique_tickers:
            try:
                if t in level0:
                    unique_results[t] = _parse_close_series(raw[t], t)
                else:
                    logger.debug(f"⚠️  {t} absent from sector batch (likely delisted) — skipped")
                    unique_results[t] = None
            except Exception:
                logger.exception(f"⚠️  Slice error for {t} in sector batch")
                unique_results[t] = None

        # Fan out: every originally requested ticker (including duplicates) gets its result.
        for t in tickers:
            results[t] = unique_results.get(t)
        try:
            mark_success('yfinance')
        except Exception:
            logger.exception("Failed to report yfinance success for sector rotation")

    except Exception as e:
        logger.exception("⚠️  Sector batch download failed — all sectors will be skipped")
        try:
            mark_failure('yfinance', f"{e} (Sector Rotation Batch)")
        except Exception:
            logger.exception("Failed to report yfinance failure for sector rotation (exception path)")
        for t in tickers:
            results[t] = None

    return results




def _pct_return(series: pd.Series, lookback: int) -> Optional[float]:
    if len(series) < lookback + 1: return None
    start = float(series.iloc[-(lookback + 1)])
    end   = float(series.iloc[-1])
    return None if start <= 0 else (end / start - 1.0) * 100.0


def _classify(rs_value: float, rs_momentum: float) -> str:
    strong   = rs_value   >= 1.0
    positive = rs_momentum >= 0.0
    if strong and positive:     return "LEADING"
    if not strong and positive: return "IMPROVING"
    if strong and not positive: return "WEAKENING"
    return                             "LAGGING"


def _build_report(scores, strong_sectors, nifty_return_pct, scan_date, errors) -> str:
    buckets = {k: [] for k in ("LEADING", "IMPROVING", "WEAKENING", "LAGGING")}
    for s in scores.values(): buckets[s.classification].append(s)
    for v in buckets.values(): v.sort(key=lambda x: x.outperformance_pct, reverse=True)

    def fmt(s):
        sign = "+" if s.outperformance_pct >= 0 else ""
        flag = " 🔥" if s.outperformance_pct >= HIGHLIGHT_THRESHOLD_PCT else ""
        return f"   • {s.name} {sign}{s.outperformance_pct:.1f}% vs Nifty{flag}"

    lines = [
        "= = = = = = = = = = = = = = = = =",
        f"🔄 SECTOR ROTATION | {scan_date.strftime('%d %b %Y')}",
        f"📊 Nifty 50: {'+' if nifty_return_pct >= 0 else ''}{nifty_return_pct:.1f}% ({RS_LOOKBACK_DAYS}d)",
        "= = = = = = = = = = = = = = = = =",
    ]

    icons  = {"LEADING": "✅", "IMPROVING": "📈", "WEAKENING": "📉", "LAGGING": "❌"}
    labels = {
        "LEADING":   "LEADING — Strong & Strengthening",
        "IMPROVING": "IMPROVING — Weak but Recovering",
        "WEAKENING": "WEAKENING — Strong but Fading",
        "LAGGING":   "LAGGING — Weak & Weakening",
    }
    for k in ("LEADING", "IMPROVING", "WEAKENING", "LAGGING"):
        if buckets[k]:
            lines.append(f"{icons[k]} {labels[k]}:")
            lines.extend(fmt(s) for s in buckets[k])

    focus = sorted(strong_sectors, key=lambda n: scores[n].outperformance_pct, reverse=True)
    lines.append("")
    if focus:
        lines.append(f"🎯 Scan focus: {', '.join(focus)}")
        lines.append("ℹ️  Score +4 (Leading) / +2 (Improving) / -2 (Weakening) / -4 (Lagging)")
    else:
        lines.append("⚠️  No leading sectors — full universe scan, no sector bonus applied")

    if errors:
        lines.append(f"⚠️  ETF data missing: {', '.join(errors)}")

    return "\n".join(lines)


# =====================================================================================
# PUBLIC API
# =====================================================================================
_cache:      Optional[SectorRotationResult] = None
_cache_time: Optional[datetime]             = None
CACHE_TTL_MINUTES = 30


def get_sector_scores(
    rs_lookback: int = RS_LOOKBACK_DAYS,
    momentum_lookback: int = MOMENTUM_LOOKBACK_DAYS,
    force_refresh: bool = False,
) -> SectorRotationResult:
    global _cache, _cache_time

    if not force_refresh and _cache is not None and _cache_time is not None:
        elapsed = (datetime.now(IST) - _cache_time).total_seconds()
        if elapsed < CACHE_TTL_MINUTES * 60:
            logger.info(
                f"🔄 Sector rotation: using cached result "
                f"({elapsed/60:.1f}m old, TTL={CACHE_TTL_MINUTES}m)"
            )
            return _cache

    today         = date.today()
    errors        = []
    sector_scores = {}

    # Build deduplicated ticker list: benchmark + all unique ETF tickers.
    # SECTOR_ETF_MAP may map multiple sectors to the same proxy ETF (e.g. INFRAIETF.NS
    # is used for Infrastructure, Capital Goods, and Railways). _batch_download_closes
    # handles deduplication internally and fans the result back to all callers.
    all_tickers = [BENCHMARK_TICKER] + list(SECTOR_ETF_MAP.values())
    close_map   = _batch_download_closes(all_tickers)

    nifty_close = close_map.get(BENCHMARK_TICKER)
    if nifty_close is None or len(nifty_close) < MIN_BARS_REQUIRED:
        logger.error("❌ Sector Rotation: Nifty 50 download failed")
        result = SectorRotationResult({}, set(), set(), "⚠️ Unavailable", today, 0.0, ["Nifty 50"])
        _cache, _cache_time = result, datetime.now(IST)
        return result

    nifty_return = _pct_return(nifty_close, rs_lookback)
    if nifty_return is None:
        logger.error("❌ Sector Rotation: insufficient Nifty bars")
        result = SectorRotationResult({}, set(), set(), "⚠️ Insufficient data", today, 0.0, ["Nifty 50 bars"])
        _cache, _cache_time = result, datetime.now(IST)
        return result

    nifty_base = nifty_return if abs(nifty_return) > 0.001 else 0.001

    for sector_name, etf_ticker in SECTOR_ETF_MAP.items():
        close = close_map.get(etf_ticker)
        if close is None or len(close) < MIN_BARS_REQUIRED:
            errors.append(sector_name)
            continue

        sec_return = _pct_return(close, rs_lookback)
        if sec_return is None:
            errors.append(sector_name)
            continue

        rs_value = (1 + sec_return / 100) / (1 + nifty_base / 100)

        sec_lagged   = _pct_return(close.iloc[:-momentum_lookback], rs_lookback)
        nifty_lagged = _pct_return(nifty_close.iloc[:-momentum_lookback], rs_lookback)
        if sec_lagged is not None and nifty_lagged is not None:
            nb_lagged   = nifty_lagged if abs(nifty_lagged) > 0.001 else 0.001
            rs_lagged   = (1 + sec_lagged / 100) / (1 + nb_lagged / 100)
            rs_momentum = rs_value - rs_lagged
        else:
            rs_momentum = 0.0

        classification = _classify(rs_value, rs_momentum)
        sector_scores[sector_name] = SectorScore(
            sector_name, etf_ticker,
            round(rs_value, 4), round(rs_momentum, 4),
            round((rs_value - 1.0) * 100, 2), classification,
            round(sec_return, 2), round(nifty_return, 2), len(close),
        )

    # ── Classification summary logs ───────────────────────────────────────────────────
    buckets_log = {"LEADING": "✅", "IMPROVING": "📈", "WEAKENING": "📉", "LAGGING": "❌"}
    for label, icon in buckets_log.items():
        bucket = [(n, s) for n, s in sector_scores.items() if s.classification == label]
        if bucket:
            sorted_b = sorted(bucket, key=lambda x: x[1].outperformance_pct, reverse=True)
            names = ", ".join(
                f"{n} ({'+' if s.outperformance_pct >= 0 else ''}{s.outperformance_pct:.1f}%)"
                for n, s in sorted_b
            )
            logger.info(f"{icon} {label}: {names}")
        else:
            logger.info(f"  — {label}: none")

    strong_sectors = {n for n, s in sector_scores.items() if s.classification in ("LEADING", "IMPROVING")}
    weak_sectors   = {n for n, s in sector_scores.items() if s.classification in ("WEAKENING", "LAGGING")}
    report = _build_report(sector_scores, strong_sectors, nifty_return, today, errors)

    result = SectorRotationResult(
        sector_scores, strong_sectors, weak_sectors, report,
        today, round(nifty_return, 2), errors,
    )
    _cache, _cache_time = result, datetime.now(IST)
    return result


_ROTATION_BONUS = {"LEADING": +4, "IMPROVING": +2, "WEAKENING": -2, "LAGGING": -4}


def get_sector_score_bonus(
    symbol: str,
    result: SectorRotationResult,
    sector: str = None,
) -> int:
    """
    Returns the sector rotation score bonus (or penalty) for a symbol.

    Parameters
    ----------
    symbol  : NSE ticker (e.g. "INFY")
    result  : SectorRotationResult from get_sector_scores()
    sector  : TV sector string from the watchlist parquet row["Sector"].
              When provided, this takes priority over NSE_SECTOR_MAP lookup.
              Translated via TV_SECTOR_TO_ROTATION before matching.

    Lookup order (first match wins):
      1. sector param → TV_SECTOR_TO_ROTATION → SECTOR_ETF_MAP key
      2. NSE_SECTOR_MAP[symbol] → SECTOR_ETF_MAP key  (legacy fallback)

    Returns 0 gracefully if sector unavailable, ETF data missing, or any error.
    """
    if not result.scores:
        return 0

    rotation_sector = None

    # Priority 1: watchlist sector column (TV string → rotation key)
    if sector and sector not in ("Unknown", "", "nan", "None"):
        rotation_sector = TV_SECTOR_TO_ROTATION.get(sector.strip())
        if rotation_sector is None:
            # Try direct match in case TV string already matches SECTOR_ETF_MAP key
            if sector.strip() in result.scores:
                rotation_sector = sector.strip()

    # Priority 2: hardcoded NSE_SECTOR_MAP fallback
    if rotation_sector is None:
        rotation_sector = NSE_SECTOR_MAP.get(symbol.strip().upper())

    if rotation_sector is None:
        logger.debug(
            f"  ○ [{symbol}] sector not mapped "
            f"(tv_sector={sector!r}) — rotation bonus skipped"
        )
        return 0

    score_obj = result.scores.get(rotation_sector)
    if score_obj is None:
        logger.debug(
            f"  ○ [{symbol}] rotation_sector={rotation_sector!r} "
            f"not in scores (ETF data missing?) — bonus skipped"
        )
        return 0

    bonus = _ROTATION_BONUS.get(score_obj.classification, 0)
    logger.info(
        f"  {'+'if bonus>=0 else ''}{bonus} [{symbol}] "
        f"sector={rotation_sector} | {score_obj.classification} "
        f"| RS={score_obj.rs_value:.3f} | source={'watchlist' if sector else 'fallback_map'}"
    )
    return bonus
