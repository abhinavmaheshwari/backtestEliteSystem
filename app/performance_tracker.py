# =====================================================================================
# app/performance_tracker.py
#
# WHAT THIS DOES:
#   Reads every alert from alerts.db, fetches the entry-day close price
#   and the current market price via yfinance, and calculates P&L, win-rate,
#   and per-scanner/per-category statistics.
#
#   Outputs:
#     1. JSON file  → data/performance_data.json  (consumed by the dashboard)
#     2. Console summary
#
#   Run manually:
#       python app/performance_tracker.py
#
#   Or import and call generate_performance_data() from another module.
# =====================================================================================

import sqlite3
import json
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

try:
    from config import DB_PATH, DATA_DIR
except ImportError:
    import os
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DB_PATH  = os.path.join(BASE_DIR, "data", "alerts.db")
    DATA_DIR = os.path.join(BASE_DIR, "data")

import os
OUTPUT_JSON = os.path.join(DATA_DIR, "performance_data.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# How many calendar days back to fetch price history for entry-day lookups
HISTORY_LOOKBACK = "2y"

# Minimum move (%) to be counted as a "win" for the win-rate calculation
WIN_THRESHOLD_PCT = 2.0

# ─────────────────────────────────────────────────────────────────────────────────────

def _fetch_price_history(symbol: str) -> pd.DataFrame | None:
    """Returns a daily OHLCV DataFrame for the NSE ticker, or None on failure."""
    ns_sym = f"{symbol}.NS"
    for attempt in range(3):
        try:
            df = yf.download(
                ns_sym,
                period=HISTORY_LOOKBACK,
                interval="1d",
                progress=False,
                auto_adjust=True,
                threads=False,
            )
            if df is not None and not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df.index = pd.to_datetime(df.index).tz_localize(None)
                return df
        except Exception as e:
            logger.debug(f"  Attempt {attempt+1} failed for {symbol}: {e}")
            time.sleep(1.5 ** attempt)
    return None


def _get_price_on_date(df: pd.DataFrame, target_date: datetime.date) -> float | None:
    """
    Returns the closing price on target_date.
    Falls back to the next available trading day if target_date is a weekend/holiday.
    """
    if df is None or df.empty:
        return None
    df_dates = df.index.normalize()
    target_ts = pd.Timestamp(target_date)
    # Look within 5 trading days forward (handles bank holidays, weekends)
    for offset in range(6):
        candidate = target_ts + pd.Timedelta(days=offset)
        matches = df[df_dates == candidate]
        if not matches.empty:
            return float(matches["Close"].iloc[0])
    return None


def _latest_price(df: pd.DataFrame) -> float | None:
    if df is None or df.empty:
        return None
    return float(df["Close"].iloc[-1])


def _parse_scanner(breakout_type: str) -> str:
    """Extracts scanner name from the dedup key: Category|Signals|Date|SCANNER"""
    parts = breakout_type.split("|")
    if len(parts) >= 4:
        return parts[-1].strip()
    return "UNKNOWN"


def _parse_category(breakout_type: str) -> str:
    parts = breakout_type.split("|")
    if len(parts) >= 1:
        return parts[0].strip()
    return "UNKNOWN"


def _parse_signals(breakout_type: str) -> str:
    parts = breakout_type.split("|")
    if len(parts) >= 2:
        return parts[1].strip()
    return ""


# ─────────────────────────────────────────────────────────────────────────────────────

def load_alerts() -> pd.DataFrame:
    """Read all alerts from the SQLite database."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    df = pd.read_sql_query(
        "SELECT symbol, breakout_type, alert_time, alert_date FROM alerts ORDER BY alert_date ASC",
        conn,
    )
    conn.close()
    df["alert_date"] = pd.to_datetime(df["alert_date"]).dt.date
    return df


# ─────────────────────────────────────────────────────────────────────────────────────

def generate_performance_data() -> dict:
    """
    Core function. Returns a dict that is both saved as JSON and returned to callers.

    Structure:
    {
      "generated_at": "ISO string",
      "summary": { total_alerts, winners, losers, open, win_rate, avg_return_pct, ... },
      "by_scanner": { "EOD": {...}, "INTRADAY": {...}, ... },
      "by_category": { "Elite Compounder": {...}, ... },
      "trades": [ { symbol, scanner, category, entry_date, entry_price, current_price,
                     pnl_pct, status }, ... ],
      "equity_curve": [ { date, cumulative_return }, ... ],
      "monthly": [ { month, alerts, wins, win_rate }, ... ]
    }
    """
    logger.info("=" * 70)
    logger.info("📊 PERFORMANCE TRACKER | Building performance data...")
    logger.info("=" * 70)

    if not os.path.exists(DB_PATH):
        logger.error(f"❌ Database not found at {DB_PATH}")
        return {}

    alerts = load_alerts()
    if alerts.empty:
        logger.warning("⚠️ No alerts in database yet.")
        return {"generated_at": datetime.now(IST).isoformat(), "trades": [], "summary": {}}

    logger.info(f"📋 Loaded {len(alerts)} alert records for {alerts['symbol'].nunique()} unique symbols")

    # ── FETCH PRICE HISTORY (one batch per unique symbol) ────────────────────────────
    unique_symbols = alerts["symbol"].unique().tolist()
    price_cache: dict[str, pd.DataFrame | None] = {}

    for i, sym in enumerate(unique_symbols, 1):
        logger.info(f"  [{i}/{len(unique_symbols)}] Fetching price history for {sym}")
        price_cache[sym] = _fetch_price_history(sym)
        time.sleep(0.3)  # gentle rate limiting

    # ── BUILD TRADE RECORDS ──────────────────────────────────────────────────────────
    today = datetime.now(IST).date()
    trades = []

    for _, row in alerts.iterrows():
        symbol        = row["symbol"]
        alert_date    = row["alert_date"]
        breakout_type = row["breakout_type"]
        scanner       = _parse_scanner(breakout_type)
        category      = _parse_category(breakout_type)
        signals       = _parse_signals(breakout_type)

        df = price_cache.get(symbol)
        entry_price   = _get_price_on_date(df, alert_date)
        current_price = _latest_price(df)

        if entry_price is None or entry_price <= 0:
            status    = "NO_DATA"
            pnl_pct   = None
        elif current_price is None:
            status    = "NO_DATA"
            pnl_pct   = None
        else:
            pnl_pct = ((current_price - entry_price) / entry_price) * 100
            days_held = (today - alert_date).days

            if days_held <= 1:
                status = "OPEN"          # Only held since yesterday — too early to judge
            elif pnl_pct >= WIN_THRESHOLD_PCT:
                status = "WIN"
            elif pnl_pct <= -WIN_THRESHOLD_PCT:
                status = "LOSS"
            else:
                status = "NEUTRAL"

        trades.append({
            "symbol":        symbol,
            "scanner":       scanner,
            "category":      category,
            "signals":       signals,
            "entry_date":    str(alert_date),
            "entry_price":   round(entry_price, 2) if entry_price else None,
            "current_price": round(current_price, 2) if current_price else None,
            "pnl_pct":       round(pnl_pct, 2) if pnl_pct is not None else None,
            "status":        status,
            "days_held":     (today - alert_date).days,
        })

    # ── SUMMARY STATS ────────────────────────────────────────────────────────────────
    judged   = [t for t in trades if t["status"] in ("WIN", "LOSS", "NEUTRAL")]
    winners  = [t for t in judged if t["status"] == "WIN"]
    losers   = [t for t in judged if t["status"] == "LOSS"]
    open_pos = [t for t in trades if t["status"] == "OPEN"]

    win_rate   = (len(winners) / len(judged) * 100) if judged else 0
    pnl_values = [t["pnl_pct"] for t in judged if t["pnl_pct"] is not None]
    avg_return = sum(pnl_values) / len(pnl_values) if pnl_values else 0
    best_trade = max(pnl_values) if pnl_values else 0
    worst_trade= min(pnl_values) if pnl_values else 0
    avg_win    = sum(t["pnl_pct"] for t in winners if t["pnl_pct"] is not None) / len(winners) if winners else 0
    avg_loss   = sum(t["pnl_pct"] for t in losers  if t["pnl_pct"] is not None) / len(losers)  if losers  else 0

    summary = {
        "total_alerts":   len(trades),
        "judged":         len(judged),
        "winners":        len(winners),
        "losers":         len(losers),
        "open_positions": len(open_pos),
        "win_rate":       round(win_rate, 1),
        "avg_return_pct": round(avg_return, 2),
        "avg_win_pct":    round(avg_win, 2),
        "avg_loss_pct":   round(avg_loss, 2),
        "best_trade_pct": round(best_trade, 2),
        "worst_trade_pct":round(worst_trade, 2),
        "expectancy":     round((win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss), 2),
    }

    # ── BY SCANNER ───────────────────────────────────────────────────────────────────
    by_scanner: dict[str, dict] = {}
    for scanner_name in set(t["scanner"] for t in trades):
        st = [t for t in judged if t["scanner"] == scanner_name]
        sw = [t for t in st if t["status"] == "WIN"]
        pnls = [t["pnl_pct"] for t in st if t["pnl_pct"] is not None]
        by_scanner[scanner_name] = {
            "total":    len([t for t in trades if t["scanner"] == scanner_name]),
            "judged":   len(st),
            "win_rate": round(len(sw) / len(st) * 100, 1) if st else 0,
            "avg_return": round(sum(pnls) / len(pnls), 2) if pnls else 0,
        }

    # ── BY CATEGORY ──────────────────────────────────────────────────────────────────
    by_category: dict[str, dict] = {}
    for cat_name in set(t["category"] for t in trades):
        ct = [t for t in judged if t["category"] == cat_name]
        cw = [t for t in ct if t["status"] == "WIN"]
        pnls = [t["pnl_pct"] for t in ct if t["pnl_pct"] is not None]
        by_category[cat_name] = {
            "total":    len([t for t in trades if t["category"] == cat_name]),
            "judged":   len(ct),
            "win_rate": round(len(cw) / len(ct) * 100, 1) if ct else 0,
            "avg_return": round(sum(pnls) / len(pnls), 2) if pnls else 0,
        }

    # ── MONTHLY BREAKDOWN ────────────────────────────────────────────────────────────
    monthly_map: dict[str, dict] = {}
    for t in judged:
        month_key = t["entry_date"][:7]  # "YYYY-MM"
        if month_key not in monthly_map:
            monthly_map[month_key] = {"alerts": 0, "wins": 0, "pnls": []}
        monthly_map[month_key]["alerts"] += 1
        if t["status"] == "WIN":
            monthly_map[month_key]["wins"] += 1
        if t["pnl_pct"] is not None:
            monthly_map[month_key]["pnls"].append(t["pnl_pct"])

    monthly = [
        {
            "month":       k,
            "alerts":      v["alerts"],
            "wins":        v["wins"],
            "win_rate":    round(v["wins"] / v["alerts"] * 100, 1) if v["alerts"] else 0,
            "avg_return":  round(sum(v["pnls"]) / len(v["pnls"]), 2) if v["pnls"] else 0,
        }
        for k, v in sorted(monthly_map.items())
    ]

    # ── EQUITY CURVE (cumulative avg return over time) ───────────────────────────────
    sorted_judged = sorted(judged, key=lambda t: t["entry_date"])
    equity_curve  = []
    running_total = 0.0
    for i, t in enumerate(sorted_judged, 1):
        if t["pnl_pct"] is not None:
            running_total += t["pnl_pct"]
            equity_curve.append({
                "date":              t["entry_date"],
                "symbol":            t["symbol"],
                "cumulative_return": round(running_total / i, 2),
                "trade_return":      t["pnl_pct"],
            })

    # ── ASSEMBLE OUTPUT ──────────────────────────────────────────────────────────────
    output = {
        "generated_at": datetime.now(IST).isoformat(),
        "summary":      summary,
        "by_scanner":   by_scanner,
        "by_category":  by_category,
        "trades":       sorted(trades, key=lambda t: t["entry_date"], reverse=True),
        "equity_curve": equity_curve,
        "monthly":      monthly,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_JSON, "w") as f:
        json.dump(output, f, indent=2, default=str)

    logger.info(f"✅ Performance data saved → {OUTPUT_JSON}")
    logger.info(
        f"   Summary | Alerts: {summary['total_alerts']} | "
        f"Win Rate: {summary['win_rate']}% | "
        f"Avg Return: {summary['avg_return_pct']}%"
    )

    return output


# ─────────────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    generate_performance_data()
