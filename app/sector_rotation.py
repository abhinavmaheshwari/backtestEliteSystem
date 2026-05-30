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
    "Financials":     "FINIETF.NS",       
    "Capital Goods":  "CGIETF.NS",        
    "Chemicals":      "CHEMIETF.NS",      
    "Telecom":        "TELE.NS",          
    "Railways":       "INFRAIETF.NS",     # Proxy
    "Electronics":    "MANUIETF.NS",      # Proxy
}

BENCHMARK_TICKER      = "^NSEI"
RS_LOOKBACK_DAYS      = 20    
MOMENTUM_LOOKBACK_DAYS = 5    
DOWNLOAD_PERIOD       = "60d"
MIN_BARS_REQUIRED     = RS_LOOKBACK_DAYS + MOMENTUM_LOOKBACK_DAYS + 5
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

    def sector_for(self, symbol: str) -> Optional[str]:
        return NSE_SECTOR_MAP.get(symbol.strip().upper())

    def score_bonus_for(self, symbol: str) -> int:
        return get_sector_score_bonus(symbol, self)


# =====================================================================================
# INTERNAL HELPERS
# =====================================================================================

def _download_close(ticker: str) -> Optional[pd.Series]:
    try:
        df = yf.download(
            ticker,
            period=DOWNLOAD_PERIOD,
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=False,
        )
        if df.empty: return None

        df.reset_index(inplace=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.strip() for c in df.columns]

        date_col = next((c for c in ["Date", "Datetime", "index"] if c in df.columns), None)
        if date_col is None or "Close" not in df.columns: return None

        df[date_col] = pd.to_datetime(df[date_col])
        series = df.sort_values(date_col).set_index(date_col)["Close"].dropna()
        if isinstance(series, pd.DataFrame): series = series.iloc[:, 0]
        return series.astype(float)
    except Exception:
        logger.exception(f"⚠️  _download_close({ticker}) failed")
        return None

def _pct_return(series: pd.Series, lookback: int) -> Optional[float]:
    if len(series) < lookback + 1: return None
    start = float(series.iloc[-(lookback + 1)])
    end   = float(series.iloc[-1])
    return None if start <= 0 else (end / start - 1.0) * 100.0

def _classify(rs_value: float, rs_momentum: float) -> str:
    strong   = rs_value  >= 1.0
    positive = rs_momentum >= 0.0
    if strong and positive:     return "LEADING"
    if not strong and positive: return "IMPROVING"
    if strong and not positive: return "WEAKENING"
    return                      "LAGGING"

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

    if errors: lines.append(f"⚠️  ETF data missing: {', '.join(errors)}")

    return "\n".join(lines)


# =====================================================================================
# PUBLIC API
# =====================================================================================
_cache:      Optional[SectorRotationResult] = None
_cache_time: Optional[datetime]             = None
CACHE_TTL_MINUTES = 30

def get_sector_scores(rs_lookback=RS_LOOKBACK_DAYS, momentum_lookback=MOMENTUM_LOOKBACK_DAYS, force_refresh=False):
    global _cache, _cache_time

    if not force_refresh and _cache is not None and _cache_time is not None:
        elapsed = (datetime.now(IST) - _cache_time).total_seconds()
        if elapsed < CACHE_TTL_MINUTES * 60:
            logger.info(f"🔄 Sector rotation: using cached result ({elapsed/60:.1f}m old, TTL={CACHE_TTL_MINUTES}m)")
            return _cache

    today  = date.today()
    errors = []
    sector_scores = {}

    logger.info("🔄 Sector Rotation Engine: downloading Nifty 50 benchmark...")
    nifty_close = _download_close(BENCHMARK_TICKER)

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
        close = _download_close(etf_ticker)
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
            sector_name, etf_ticker, round(rs_value, 4), round(rs_momentum, 4),
            round((rs_value - 1.0) * 100, 2), classification, round(sec_return, 2),
            round(nifty_return, 2), len(close)
        )

    strong_sectors = {n for n, s in sector_scores.items() if s.classification in ("LEADING", "IMPROVING")}
    weak_sectors   = {n for n, s in sector_scores.items() if s.classification in ("WEAKENING", "LAGGING")}
    report = _build_report(sector_scores, strong_sectors, nifty_return, today, errors)

    result = SectorRotationResult(sector_scores, strong_sectors, weak_sectors, report, today, round(nifty_return, 2), errors)
    _cache, _cache_time = result, datetime.now(IST)
    return result

_ROTATION_BONUS = {"LEADING": +4, "IMPROVING": +2, "WEAKENING": -2, "LAGGING": -4}

def get_sector_score_bonus(symbol: str, result: SectorRotationResult) -> int:
    if not result.scores: return 0
    sector = NSE_SECTOR_MAP.get(symbol.strip().upper())
    if sector is None: return 0
    score_obj = result.scores.get(sector)
    if score_obj is None: return 0
    
    bonus = _ROTATION_BONUS.get(score_obj.classification, 0)
    logger.info(f"  {'+'if bonus>=0 else ''}{bonus} [{symbol}] sector={sector} | {score_obj.classification} | RS={score_obj.rs_value:.3f}")
    return bonus
