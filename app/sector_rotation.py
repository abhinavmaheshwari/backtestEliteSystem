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
#   • NEVER hard-filters stocks.  Rotation status is a score modifier only.
#     A stock in a lagging sector can still fire if its individual signals are
#     strong enough to clear the MIN_SCORE threshold.  This preserves alert
#     recall — you won't miss a genuine breakout because an ETF was stale.
#   • Fully graceful degradation.  If any ETF download fails, that sector is
#     simply absent from the map.  If the benchmark fails, the entire engine
#     returns neutral (0 bonus/penalty for all stocks).  Scanners never pause.
#   • Sector lookup is symbol-driven, not category-driven.  Your watchlist's
#     "Category" column (Elite Compounder / High Growth / Mature Quality) is a
#     *quality tier*, not a sector.  We map symbol → sector independently via
#     NSE_SECTOR_MAP below, which is auto-loaded at import time and requires
#     zero runtime API calls.
#
# HOW IT WORKS:
#   1. Downloads 60 days of daily OHLCV for each sector index ETF + ^NSEI.
#   2. Relative Strength (RS) = sector_pct_return / nifty_pct_return over
#      RS_LOOKBACK_DAYS (default 20 trading days ≈ 1 month).
#   3. RS Momentum = RS_now − RS_{N days ago} over MOMENTUM_LOOKBACK_DAYS
#      (default 5 days ≈ 1 week). Positive = money flowing in.
#   4. RS Quadrant classification (standard Relative Rotation Graph logic):
#        LEADING   — RS ≥ 1.0  AND momentum ≥ 0   (strong & strengthening)
#        IMPROVING — RS <  1.0  AND momentum ≥ 0   (weak but recovering)
#        WEAKENING — RS ≥ 1.0  AND momentum <  0   (strong but fading)
#        LAGGING   — RS <  1.0  AND momentum <  0   (weak & weakening)
#   5. Score modifier applied per stock via get_sector_score_bonus():
#        LEADING   → +4 pts   (sector tailwind, confirms breakout)
#        IMPROVING → +2 pts   (early rotation, slight edge)
#        WEAKENING → -2 pts   (sector fading, slight headwind)
#        LAGGING   → -4 pts   (sector selling, meaningful headwind)
#        Unknown   →  0 pts   (symbol not in map — neutral, no penalty)
#
# SECTOR ETF MAP:
#   NSE-listed ETFs that track official Nifty sector indices.
#   Chosen over raw index symbols (e.g. ^CNXPHARMA) because yfinance returns
#   those inconsistently — ETF tickers are equity instruments with reliable data.
#
# INTEGRATION (copy-paste ready):
#
#   ── eod_scanner.py ───────────────────────────────────────────────────────────
#   from sector_rotation import get_sector_scores, get_sector_score_bonus
#
#   # Once per scan attempt, BEFORE the stock loop:
#   rotation = get_sector_scores()
#   if rotation.rotation_report:
#       send_telegram_message(rotation.rotation_report, scan_type="EOD")
#
#   # Inside stock loop, AFTER calculate_score():
#   score += get_sector_score_bonus(symbol, rotation)
#   score  = max(0, min(score, 100))
#
#   ── intraday.py / live_scanner.py ────────────────────────────────────────────
#   from sector_rotation import get_sector_scores, get_sector_score_bonus
#
#   # Once per scan cycle, BEFORE the stock loop:
#   rotation = get_sector_scores()
#
#   # Inside stock loop, AFTER calculate_score():
#   score += get_sector_score_bonus(symbol, rotation)
#   score  = max(0, min(score, 100))
#
# =====================================================================================

import logging
import yfinance as yf
import pandas as pd

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST    = ZoneInfo("Asia/Kolkata")

# =====================================================================================
# SECTOR ETF MAP
# sector_name (str) → yfinance ticker (str)
# Sector names are the authoritative keys used throughout the codebase.
# NSE_SECTOR_MAP below maps stock symbols to these same sector_name strings.
# =====================================================================================

SECTOR_ETF_MAP: dict[str, str] = {
    "IT":             "ITETF.NS",
    "Pharma":         "PHARMABEES.NS",
    "Banking":        "BANKBEES.NS",
    "FMCG":           "FMCGIETF.NS",
    "Auto":           "AUTOIETF.NS",
    "Metal":          "METALIETF.NS",
    "Realty":         "REAIETF.NS",
    "Energy":         "ENERGIETF.NS",
    "Infrastructure": "INFRAIETF.NS",
    "PSU Bank":       "PSUBNKIETF.NS",
    "Defence":        "DEFEFIETF.NS",
    "MNC":            "MNCIETF.NS",
    "Consumption":    "CONSIETF.NS",
}

BENCHMARK_TICKER      = "^NSEI"
RS_LOOKBACK_DAYS      = 20    # 1 calendar month of trading days
MOMENTUM_LOOKBACK_DAYS = 5    # 1 trading week
DOWNLOAD_PERIOD       = "60d"
MIN_BARS_REQUIRED     = RS_LOOKBACK_DAYS + MOMENTUM_LOOKBACK_DAYS + 5
HIGHLIGHT_THRESHOLD_PCT = 5.0  # sectors outperforming Nifty by this % get 🔥

# =====================================================================================
# NSE SYMBOL → SECTOR MAP
#
# This is the ONLY place you need to maintain the symbol→sector relationship.
# Keys   = NSE ticker without suffix, uppercase  (e.g. "TCS", "RELIANCE")
# Values = sector name matching SECTOR_ETF_MAP keys exactly
#
# HOW TO EXTEND:
#   When you add new stocks to your watchlist (daily_builder.py), add one line here.
#   Wrong sector = neutral (0 pts), not a crash.  Missing = also neutral (0 pts).
#   There is no penalty for an unmapped symbol.
# =====================================================================================

NSE_SECTOR_MAP: dict[str, str] = {
    # ── IT ───────────────────────────────────────────────────────────────────────────
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT", "TECHM": "IT",
    "LTIM": "IT", "MPHASIS": "IT", "PERSISTENT": "IT", "COFORGE": "IT",
    "OFSS": "IT", "KPITTECH": "IT", "TATAELXSI": "IT", "MASTEK": "IT",
    "HEXAWARE": "IT", "NIITTECH": "IT", "RATEGAIN": "IT", "NEWGEN": "IT",

    # ── Pharma ───────────────────────────────────────────────────────────────────────
    "SUNPHARMA": "Pharma", "DRREDDY": "Pharma", "CIPLA": "Pharma",
    "DIVISLAB": "Pharma", "AUROPHARMA": "Pharma", "TORNTPHARM": "Pharma",
    "ALKEM": "Pharma", "LUPIN": "Pharma", "ABBOTINDIA": "Pharma",
    "GLAXO": "Pharma", "PFIZER": "Pharma", "SANOFI": "Pharma",
    "LALPATHLAB": "Pharma", "METROPOLIS": "Pharma", "POLYMED": "Pharma",
    "GRANULES": "Pharma", "AJANTPHARM": "Pharma", "NATCOPHARM": "Pharma",
    "IPCALAB": "Pharma", "GLAND": "Pharma", "ERIS": "Pharma",

    # ── Banking ───────────────────────────────────────────────────────────────────────
    "HDFCBANK": "Banking", "ICICIBANK": "Banking", "KOTAKBANK": "Banking",
    "AXISBANK": "Banking", "INDUSINDBK": "Banking", "FEDERALBNK": "Banking",
    "BANDHANBNK": "Banking", "IDFCFIRSTB": "Banking", "RBLBANK": "Banking",
    "AUBANK": "Banking", "YESBANK": "Banking", "DCBBANK": "Banking",
    "KARNATAKA": "Banking", "CSBBANK": "Banking",

    # ── PSU Bank ──────────────────────────────────────────────────────────────────────
    "SBIN": "PSU Bank", "BANKBARODA": "PSU Bank", "PNB": "PSU Bank",
    "CANBK": "PSU Bank", "UNIONBANK": "PSU Bank", "INDIANB": "PSU Bank",
    "BANKINDIA": "PSU Bank", "MAHABANK": "PSU Bank", "IOB": "PSU Bank",
    "UCOBANK": "PSU Bank", "CENTRALBK": "PSU Bank",

    # ── FMCG ──────────────────────────────────────────────────────────────────────────
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG", "DABUR": "FMCG", "MARICO": "FMCG",
    "COLPAL": "FMCG", "GODREJCP": "FMCG", "EMAMILTD": "FMCG",
    "VBL": "FMCG", "UBL": "FMCG", "MCDOWELL-N": "FMCG",
    "RADICO": "FMCG", "TATACONSUM": "FMCG", "JYOTHYLAB": "FMCG",

    # ── Auto ──────────────────────────────────────────────────────────────────────────
    "MARUTI": "Auto", "TATAMOTORS": "Auto", "M&M": "Auto",
    "BAJAJ-AUTO": "Auto", "HEROMOTOCO": "Auto", "EICHERMOT": "Auto",
    "TVSMOTORS": "Auto", "ASHOKLEY": "Auto", "TIINDIA": "Auto",
    "MOTHERSON": "Auto", "BOSCHLTD": "Auto", "BHARATFORG": "Auto",
    "BALKRISIND": "Auto", "APOLLOTYRE": "Auto", "MRF": "Auto",
    "CEATLTD": "Auto", "EXIDEIND": "Auto", "AMARARAJA": "Auto",
    "SUNDRMFAST": "Auto", "SUBROS": "Auto",

    # ── Metal ─────────────────────────────────────────────────────────────────────────
    "TATASTEEL": "Metal", "JSWSTEEL": "Metal", "SAIL": "Metal",
    "HINDALCO": "Metal", "VEDL": "Metal", "NMDC": "Metal",
    "NATIONALUM": "Metal", "APLAPOLLO": "Metal", "RATNAMANI": "Metal",
    "WELSPUNLIV": "Metal", "JINDALSTEL": "Metal", "JSWISPL": "Metal",
    "HINDZINC": "Metal", "MOIL": "Metal",

    # ── Energy ───────────────────────────────────────────────────────────────────────
    "RELIANCE": "Energy", "ONGC": "Energy", "BPCL": "Energy",
    "IOC": "Energy", "HINDPETRO": "Energy", "GAIL": "Energy",
    "PETRONET": "Energy", "NTPC": "Energy", "POWERGRID": "Energy",
    "TATAPOWER": "Energy", "ADANIPOWER": "Energy", "ADANIGREEN": "Energy",
    "TORNTPOWER": "Energy", "CESC": "Energy", "JSWENERGY": "Energy",
    "NHPC": "Energy", "SJVN": "Energy", "IREDA": "Energy",

    # ── Realty ───────────────────────────────────────────────────────────────────────
    "DLF": "Realty", "GODREJPROP": "Realty", "OBEROIRLTY": "Realty",
    "PRESTIGE": "Realty", "BRIGADE": "Realty", "SOBHA": "Realty",
    "PHOENIXLTD": "Realty", "MAHLIFE": "Realty", "LODHA": "Realty",
    "SUNTECK": "Realty", "KOLTEPATIL": "Realty", "ARVIND": "Realty",

    # ── Infrastructure ────────────────────────────────────────────────────────────────
    "LT": "Infrastructure", "LTTS": "Infrastructure", "IRCON": "Infrastructure",
    "RVNL": "Infrastructure", "IRFC": "Infrastructure", "RECLTD": "Infrastructure",
    "PFC": "Infrastructure", "ADANIPORTS": "Infrastructure", "GMRINFRA": "Infrastructure",
    "AIAENGLTD": "Infrastructure", "CUMMINSIND": "Infrastructure", "ABB": "Infrastructure",
    "SIEMENS": "Infrastructure", "HAVELLS": "Infrastructure", "KEI": "Infrastructure",
    "POLYCAB": "Infrastructure", "KALPATPOWR": "Infrastructure", "KEC": "Infrastructure",
    "ENGINERSIN": "Infrastructure", "NBCC": "Infrastructure",

    # ── Defence ──────────────────────────────────────────────────────────────────────
    "HAL": "Defence", "BEL": "Defence", "COCHINSHIP": "Defence",
    "MAZDOCK": "Defence", "GRSE": "Defence", "MIDHANI": "Defence",
    "PARAS": "Defence", "DATAPATTNS": "Defence", "BHEL": "Defence",
    "BEML": "Defence", "ASTRA": "Defence", "MTAR": "Defence",

    # ── MNC ───────────────────────────────────────────────────────────────────────────
    "ASIANPAINT": "MNC", "PIDILITIND": "MNC", "3MINDIA": "MNC",
    "HONAUT": "MNC", "SCHNEIDER": "MNC", "GILLETTE": "MNC",

    # ── Consumption ──────────────────────────────────────────────────────────────────
    "DMART": "Consumption", "TRENT": "Consumption", "ZOMATO": "Consumption",
    "NYKAA": "Consumption", "INDIAMART": "Consumption", "IRCTC": "Consumption",
    "JUBLFOOD": "Consumption", "DEVYANI": "Consumption", "SAPPHIRE": "Consumption",
    "WESTLIFE": "Consumption", "BARBEQUE": "Consumption", "EASEMYTRIP": "Consumption",
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
    rotation_report:  str                      # Telegram-ready summary
    scan_date:        date
    nifty_return_pct: float
    errors:           list[str] = field(default_factory=list)

    # Convenience: look up which sector a symbol belongs to
    # Returns None if symbol is not in NSE_SECTOR_MAP
    def sector_for(self, symbol: str) -> Optional[str]:
        return NSE_SECTOR_MAP.get(symbol.strip().upper())

    # Score modifier for a symbol: +4 / +2 / -2 / -4 / 0
    # Encapsulates the entire bonus logic — callers need only one call
    def score_bonus_for(self, symbol: str) -> int:
        return get_sector_score_bonus(symbol, self)


# =====================================================================================
# INTERNAL HELPERS
# =====================================================================================

def _download_close(ticker: str) -> Optional[pd.Series]:
    """Download daily close prices. Returns pd.Series indexed by date, or None."""
    try:
        df = yf.download(
            ticker,
            period=DOWNLOAD_PERIOD,
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=False,
        )
        if df.empty:
            return None

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

    except Exception as e:
        logger.warning(f"⚠️  _download_close({ticker}): {e}")
        return None


def _pct_return(series: pd.Series, lookback: int) -> Optional[float]:
    """Percentage return over the last `lookback` bars. None if insufficient data."""
    if len(series) < lookback + 1:
        return None
    start = float(series.iloc[-(lookback + 1)])
    end   = float(series.iloc[-1])
    return None if start <= 0 else (end / start - 1.0) * 100.0


def _classify(rs_value: float, rs_momentum: float) -> str:
    strong   = rs_value  >= 1.0
    positive = rs_momentum >= 0.0
    if strong and positive:     return "LEADING"
    if not strong and positive: return "IMPROVING"
    if strong and not positive: return "WEAKENING"
    return                              "LAGGING"


def _build_report(
    scores: dict[str, SectorScore],
    strong_sectors: set[str],
    nifty_return_pct: float,
    scan_date: date,
    errors: list[str],
) -> str:
    buckets = {k: [] for k in ("LEADING", "IMPROVING", "WEAKENING", "LAGGING")}
    for s in scores.values():
        buckets[s.classification].append(s)
    for v in buckets.values():
        v.sort(key=lambda x: x.outperformance_pct, reverse=True)

    def fmt(s: SectorScore) -> str:
        sign = "+" if s.outperformance_pct >= 0 else ""
        flag = " 🔥" if s.outperformance_pct >= HIGHLIGHT_THRESHOLD_PCT else ""
        return f"   • {s.name} {sign}{s.outperformance_pct:.1f}% vs Nifty{flag}"

    lines = [
        "= = = = = = = = = = = = = = = = =",
        f"🔄 SECTOR ROTATION | {scan_date.strftime('%d %b %Y')}",
        f"📊 Nifty 50: {'+' if nifty_return_pct >= 0 else ''}{nifty_return_pct:.1f}% ({RS_LOOKBACK_DAYS}d)",
        "= = = = = = = = = = = = = = = = =",
    ]

    icons = {"LEADING": "✅", "IMPROVING": "📈", "WEAKENING": "📉", "LAGGING": "❌"}
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
# SIMPLE TIME-BASED CACHE
# All three scanners run in parallel threads (main.py).  Without a cache, each
# scanner downloads all 13 ETFs independently — 3× the yfinance load.
# Cache TTL = 30 minutes.  Rotation data older than 30 min is re-fetched.
# =====================================================================================

_cache:      Optional[SectorRotationResult] = None
_cache_time: Optional[datetime]             = None
CACHE_TTL_MINUTES = 30


# =====================================================================================
# PUBLIC API
# =====================================================================================

def get_sector_scores(
    rs_lookback: int        = RS_LOOKBACK_DAYS,
    momentum_lookback: int  = MOMENTUM_LOOKBACK_DAYS,
    force_refresh: bool     = False,
) -> SectorRotationResult:
    """
    Compute (or return cached) sector relative-strength scores vs Nifty 50.

    Parameters
    ----------
    rs_lookback        : trading days for RS calculation  (default 20)
    momentum_lookback  : trading days for RS momentum     (default 5)
    force_refresh      : bypass cache and re-download     (default False)

    Returns
    -------
    SectorRotationResult — always returns something usable.
    On total failure returns a neutral result (empty scores, no bonus/penalty).
    """
    global _cache, _cache_time

    # ── CACHE CHECK ───────────────────────────────────────────────────────────────────
    if not force_refresh and _cache is not None and _cache_time is not None:
        elapsed = (datetime.now(IST) - _cache_time).total_seconds()
        if elapsed < CACHE_TTL_MINUTES * 60:
            logger.info(
                f"🔄 Sector rotation: using cached result "
                f"({elapsed/60:.1f}m old, TTL={CACHE_TTL_MINUTES}m)"
            )
            return _cache

    today  = date.today()
    errors = []
    sector_scores: dict[str, SectorScore] = {}

    # ── BENCHMARK ─────────────────────────────────────────────────────────────────────
    logger.info("🔄 Sector Rotation Engine: downloading Nifty 50 benchmark...")
    nifty_close = _download_close(BENCHMARK_TICKER)

    if nifty_close is None or len(nifty_close) < MIN_BARS_REQUIRED:
        logger.error("❌ Sector Rotation: Nifty 50 download failed — returning neutral result")
        result = SectorRotationResult(
            scores={}, strong_sectors=set(), weak_sectors=set(),
            rotation_report="⚠️ Sector rotation unavailable — score bonuses not applied",
            scan_date=today, nifty_return_pct=0.0, errors=["Nifty 50"],
        )
        _cache      = result
        _cache_time = datetime.now(IST)
        return result

    nifty_return = _pct_return(nifty_close, rs_lookback)
    if nifty_return is None:
        logger.error("❌ Sector Rotation: insufficient Nifty bars")
        result = SectorRotationResult(
            scores={}, strong_sectors=set(), weak_sectors=set(),
            rotation_report="⚠️ Sector rotation: insufficient benchmark data",
            scan_date=today, nifty_return_pct=0.0, errors=["Nifty 50 bars"],
        )
        _cache      = result
        _cache_time = datetime.now(IST)
        return result

    # Avoid divide-by-zero if Nifty is completely flat
    nifty_base = nifty_return if abs(nifty_return) > 0.001 else 0.001
    logger.info(f"✅ Nifty 50: {nifty_return:+.2f}% over {rs_lookback}d")

    # ── SECTOR ETFs ───────────────────────────────────────────────────────────────────
    for sector_name, etf_ticker in SECTOR_ETF_MAP.items():
        logger.info(f"  📦 {sector_name} ({etf_ticker})...")

        close = _download_close(etf_ticker)
        if close is None or len(close) < MIN_BARS_REQUIRED:
            logger.warning(
                f"  ⚠️  {sector_name}: "
                f"{'no data' if close is None else f'{len(close)} bars < {MIN_BARS_REQUIRED}'} — skipped"
            )
            errors.append(sector_name)
            continue

        sec_return = _pct_return(close, rs_lookback)
        if sec_return is None:
            errors.append(sector_name)
            continue

        rs_value = (1 + sec_return / 100) / (1 + nifty_base / 100)

        # RS momentum: compare RS now vs RS computed from N days ago
        sec_lagged   = _pct_return(close.iloc[:-momentum_lookback],   rs_lookback)
        nifty_lagged = _pct_return(nifty_close.iloc[:-momentum_lookback], rs_lookback)
        if sec_lagged is not None and nifty_lagged is not None:
            nb_lagged   = nifty_lagged if abs(nifty_lagged) > 0.001 else 0.001
            rs_lagged   = (1 + sec_lagged / 100) / (1 + nb_lagged / 100)
            rs_momentum = rs_value - rs_lagged
        else:
            rs_momentum = 0.0

        classification = _classify(rs_value, rs_momentum)
        sector_scores[sector_name] = SectorScore(
            name               = sector_name,
            etf_ticker         = etf_ticker,
            rs_value           = round(rs_value, 4),
            rs_momentum        = round(rs_momentum, 4),
            outperformance_pct = round((rs_value - 1.0) * 100, 2),
            classification     = classification,
            sector_return_pct  = round(sec_return, 2),
            nifty_return_pct   = round(nifty_return, 2),
            bars_available     = len(close),
        )
        logger.info(
            f"  ✅ {sector_name}: RS={rs_value:.3f} | "
            f"{(rs_value-1)*100:+.2f}% vs Nifty | mom={rs_momentum:+.4f} | {classification}"
        )

    strong_sectors = {n for n, s in sector_scores.items() if s.classification in ("LEADING", "IMPROVING")}
    weak_sectors   = {n for n, s in sector_scores.items() if s.classification in ("WEAKENING", "LAGGING")}

    report = _build_report(sector_scores, strong_sectors, nifty_return, today, errors)

    logger.info(
        f"🔄 Rotation complete | Strong={len(strong_sectors)} | "
        f"Weak={len(weak_sectors)} | Errors={len(errors)}"
    )

    result = SectorRotationResult(
        scores           = sector_scores,
        strong_sectors   = strong_sectors,
        weak_sectors     = weak_sectors,
        rotation_report  = report,
        scan_date        = today,
        nifty_return_pct = round(nifty_return, 2),
        errors           = errors,
    )
    _cache      = result
    _cache_time = datetime.now(IST)
    return result


# ── SCORE BONUS HELPER ────────────────────────────────────────────────────────────────

_ROTATION_BONUS = {
    "LEADING":   +4,
    "IMPROVING": +2,
    "WEAKENING": -2,
    "LAGGING":   -4,
}


def get_sector_score_bonus(symbol: str, result: SectorRotationResult) -> int:
    """
    Returns the score adjustment for a stock based on its sector's RS classification.

    Parameters
    ----------
    symbol : str                  — NSE ticker WITHOUT .NS suffix (e.g. "TCS")
    result : SectorRotationResult — output of get_sector_scores()

    Returns
    -------
    int — bonus to add to composite score.
          +4 LEADING | +2 IMPROVING | -2 WEAKENING | -4 LAGGING | 0 unknown

    The caller is responsible for clamping: score = max(0, min(score + bonus, 100))

    NEVER returns a value that would block an alert on its own.
    The existing MIN_SCORE threshold in each scanner handles that gate.
    """
    if not result.scores:
        # Rotation data completely unavailable — neutral
        return 0

    sector = NSE_SECTOR_MAP.get(symbol.strip().upper())
    if sector is None:
        # Symbol not in map — neutral, not a penalty
        logger.debug(f"  ○ [{symbol}] sector unknown — rotation bonus: 0")
        return 0

    score_obj = result.scores.get(sector)
    if score_obj is None:
        # Sector ETF failed to download — neutral, not a penalty
        logger.debug(f"  ○ [{symbol}] sector={sector} ETF unavailable — rotation bonus: 0")
        return 0

    bonus = _ROTATION_BONUS.get(score_obj.classification, 0)
    logger.info(
        f"  {'+'if bonus>=0 else ''}{bonus} [{symbol}] sector={sector} | "
        f"{score_obj.classification} | RS={score_obj.rs_value:.3f} | "
        f"{score_obj.outperformance_pct:+.2f}% vs Nifty"
    )
    return bonus
