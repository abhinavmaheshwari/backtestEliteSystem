# =====================================================================================
# app/performance_tracker.py
# Builds performance_data.json from the Postgres alerts table + live yfinance prices.
# Called every 5 minutes from main.py (same thread as before).
# =====================================================================================

import os
import json
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

import yfinance as yf

from database import get_all_alerts

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

try:
    from config import DATA_DIR
except ImportError:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = os.path.join(BASE_DIR, "data")

PERF_JSON_PATH = os.path.join(DATA_DIR, "performance_data.json")

# How many calendar days after alert_date before we "close" a trade for judging
HOLD_DAYS = 5


# =====================================================================================
# HELPERS
# =====================================================================================

def _parse_dedup_key(breakout_type: str) -> tuple[str, str, str]:
    """
    breakout_type format:  "{category}|{signals}|{date}|{scanner}"
    Returns (category, signals, scanner).  Safe for old rows without the format.
    """
    parts = breakout_type.split("|")
    if len(parts) >= 4:
        return parts[0].strip(), parts[1].strip(), parts[3].strip()
    if len(parts) == 3:
        return parts[0].strip(), parts[1].strip(), "UNKNOWN"
    return "Unknown", breakout_type, "UNKNOWN"


def _fetch_current_prices(symbols: list[str]) -> dict[str, float]:
    """Batch-fetch latest close prices for a list of NSE symbols."""
    if not symbols:
        return {}
    tickers = [s if s.endswith(".NS") else f"{s}.NS" for s in symbols]
    try:
        raw = yf.download(
            tickers,
            period="2d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        prices = {}
        if len(tickers) == 1:
            sym = symbols[0]
            try:
                prices[sym] = float(raw["Close"].dropna().iloc[-1])
            except Exception:
                pass
        else:
            for sym, ticker in zip(symbols, tickers):
                try:
                    prices[sym] = float(raw[ticker]["Close"].dropna().iloc[-1])
                except Exception:
                    pass
        return prices
    except Exception:
        logger.warning("⚠️ yfinance price fetch failed in performance_tracker")
        return {}


def _days_held(alert_date_str: str) -> int:
    try:
        alert_dt = date.fromisoformat(alert_date_str)
        return (date.today() - alert_dt).days
    except Exception:
        return 0


def _trade_status(pnl_pct: float | None, days: int) -> str:
    if days < HOLD_DAYS:
        return "OPEN"
    if pnl_pct is None:
        return "OPEN"
    if pnl_pct >= 2.0:
        return "WIN"
    if pnl_pct <= -2.0:
        return "LOSS"
    return "NEUTRAL"


# =====================================================================================
# MAIN BUILD FUNCTION
# =====================================================================================

def build_performance_data():
    logger.info("=" * 70)
    logger.info("📊 PERFORMANCE TRACKER | Building performance data...")
    logger.info("=" * 70)

    # ── 1. Load all alerts from Postgres ────────────────────────────────────────────
    try:
        raw_alerts = get_all_alerts()
    except Exception:
        logger.exception("❌ Could not load alerts from database")
        _write_empty()
        return

    if not raw_alerts:
        logger.warning("⚠️ No alerts in database yet.")
        _write_empty()
        return

    logger.info(f"📋 {len(raw_alerts)} total alerts in database")

    # ── 2. Build trade objects ───────────────────────────────────────────────────────
    trades = []
    for row in raw_alerts:
        symbol       = row["symbol"]
        alert_date   = row.get("alert_date") or (row["alert_time"][:10] if row.get("alert_time") else "")
        entry_price  = row.get("entry_price")

        # Parse category / signals / scanner from breakout_type if not stored separately
        cat_stored     = row.get("category")
        scanner_stored = row.get("scanner")
        sig_stored     = row.get("signals")

        category, signals, scanner = _parse_dedup_key(row["breakout_type"])
        if cat_stored:
            category = cat_stored
        if scanner_stored:
            scanner = scanner_stored
        if sig_stored:
            signals = sig_stored

        days = _days_held(alert_date)

        trades.append({
            "symbol":        symbol,
            "scanner":       scanner,
            "category":      category,
            "signals":       signals,
            "entry_date":    alert_date,
            "entry_price":   entry_price,       # None until we enrich below
            "stop_loss":     row.get("stop_loss"),
            "current_price": None,              # filled after price fetch
            "pnl_pct":       None,
            "stopped_out":   False,             # True if current price breached stop
            "days_held":     days,
            "status":        "OPEN",
            "score":         row.get("score"),
            "rsi":           row.get("rsi"),
            "volume_ratio":  row.get("volume_ratio"),
        })

    # ── 3. Fetch current prices for all symbols ──────────────────────────────────────
    unique_symbols = list({t["symbol"] for t in trades})
    logger.info(f"📈 Fetching prices for {len(unique_symbols)} symbols...")
    current_prices = _fetch_current_prices(unique_symbols)

    # ── 4. Enrich trades with prices + P&L ──────────────────────────────────────────
    for t in trades:
        sym   = t["symbol"]
        cur_p = current_prices.get(sym)
        ep    = t["entry_price"]
        sl    = t["stop_loss"]

        t["current_price"] = round(cur_p, 2) if cur_p else None

        # ── Stop-loss breach detection ───────────────────────────────────────────────
        # If we have both an entry price and a stop loss, check whether the current
        # price has breached the stop. If yes, the trade is force-closed at the stop
        # level — the P&L is calculated against stop_loss, NOT current_price.
        # This is the correct accounting: once stopped out, any recovery is irrelevant.
        if ep and sl and cur_p:
            if cur_p <= sl:
                # Price is AT or BELOW the stop — trade is stopped out
                t["stopped_out"] = True
                t["pnl_pct"]     = round((sl - ep) / ep * 100, 2)  # loss locked at stop
                logger.debug(f"🛑 {sym} stopped out | entry={ep} stop={sl} cur={cur_p} pnl={t['pnl_pct']}%")
            else:
                # Still open above stop — mark-to-market P&L
                t["stopped_out"] = False
                t["pnl_pct"]     = round((cur_p - ep) / ep * 100, 2)
        elif ep and cur_p:
            # No stop stored (legacy alert) — plain mark-to-market
            t["stopped_out"] = False
            t["pnl_pct"]     = round((cur_p - ep) / ep * 100, 2)
        else:
            t["stopped_out"] = False
            t["pnl_pct"]     = None

        t["status"] = _trade_status(t["pnl_pct"], t["days_held"], t["stopped_out"])

    # ── 5. Compute summary stats ─────────────────────────────────────────────────────
    judged  = [t for t in trades if t["status"] in ("WIN", "LOSS", "NEUTRAL")]
    winners = [t for t in judged if t["status"] == "WIN"]
    losers  = [t for t in judged if t["status"] == "LOSS"]
    open_p  = [t for t in trades if t["status"] == "OPEN"]

    pnls    = [t["pnl_pct"] for t in judged if t["pnl_pct"] is not None]
    win_pnl = [t["pnl_pct"] for t in winners if t["pnl_pct"] is not None]
    los_pnl = [t["pnl_pct"] for t in losers  if t["pnl_pct"] is not None]

    n_judged  = len(judged)
    wr        = round(len(winners) / n_judged * 100, 1) if n_judged else 0
    avg_ret   = round(sum(pnls) / len(pnls), 2)          if pnls     else 0
    avg_win   = round(sum(win_pnl) / len(win_pnl), 2)    if win_pnl  else 0
    avg_loss  = round(sum(los_pnl) / len(los_pnl), 2)    if los_pnl  else 0
    best      = round(max(pnls), 2)                       if pnls     else 0
    worst     = round(min(pnls), 2)                       if pnls     else 0
    expectancy = round((wr / 100) * avg_win + (1 - wr / 100) * avg_loss, 2)

    summary = {
        "total_alerts":    len(trades),
        "judged":          n_judged,
        "winners":         len(winners),
        "losers":          len(losers),
        "open_positions":  len(open_p),
        "win_rate":        wr,
        "avg_return_pct":  avg_ret,
        "avg_win_pct":     avg_win,
        "avg_loss_pct":    avg_loss,
        "best_trade_pct":  best,
        "worst_trade_pct": worst,
        "expectancy":      expectancy,
    }

    # ── 6. Equity curve ─────────────────────────────────────────────────────────────
    sorted_judged = sorted(judged, key=lambda t: t["entry_date"])
    cum = 0.0
    equity_curve = []
    for i, t in enumerate(sorted_judged):
        if t["pnl_pct"] is not None:
            cum += t["pnl_pct"]
            equity_curve.append({
                "date":              t["entry_date"],
                "symbol":            t["symbol"],
                "trade_return":      t["pnl_pct"],
                "cumulative_return": round(cum / (i + 1), 2),
            })

    # ── 7. Monthly breakdown ─────────────────────────────────────────────────────────
    mmap: dict[str, dict] = {}
    for t in judged:
        m = t["entry_date"][:7]
        if m not in mmap:
            mmap[m] = {"alerts": 0, "wins": 0, "pnls": []}
        mmap[m]["alerts"] += 1
        if t["status"] == "WIN":
            mmap[m]["wins"] += 1
        if t["pnl_pct"] is not None:
            mmap[m]["pnls"].append(t["pnl_pct"])

    monthly = []
    for m in sorted(mmap):
        v = mmap[m]
        avg_m = round(sum(v["pnls"]) / len(v["pnls"]), 2) if v["pnls"] else 0
        monthly.append({
            "month":      m,
            "alerts":     v["alerts"],
            "wins":       v["wins"],
            "win_rate":   round(v["wins"] / v["alerts"] * 100, 1) if v["alerts"] else 0,
            "avg_return": avg_m,
        })

    # ── 8. By scanner ────────────────────────────────────────────────────────────────
    all_scanners = {t["scanner"] for t in trades}
    by_scanner: dict[str, dict] = {}
    for sc in all_scanners:
        sc_judged = [t for t in judged  if t["scanner"] == sc]
        sc_wins   = [t for t in sc_judged if t["status"] == "WIN"]
        sc_pnls   = [t["pnl_pct"] for t in sc_judged if t["pnl_pct"] is not None]
        by_scanner[sc] = {
            "total":      len([t for t in trades if t["scanner"] == sc]),
            "judged":     len(sc_judged),
            "win_rate":   round(len(sc_wins) / len(sc_judged) * 100, 1) if sc_judged else 0,
            "avg_return": round(sum(sc_pnls) / len(sc_pnls), 2) if sc_pnls else 0,
        }

    # ── 9. By category ───────────────────────────────────────────────────────────────
    all_cats = {t["category"] for t in trades}
    by_category: dict[str, dict] = {}
    for cat in all_cats:
        cat_judged = [t for t in judged  if t["category"] == cat]
        cat_wins   = [t for t in cat_judged if t["status"] == "WIN"]
        cat_pnls   = [t["pnl_pct"] for t in cat_judged if t["pnl_pct"] is not None]
        by_category[cat] = {
            "total":      len([t for t in trades if t["category"] == cat]),
            "judged":     len(cat_judged),
            "win_rate":   round(len(cat_wins) / len(cat_judged) * 100, 1) if cat_judged else 0,
            "avg_return": round(sum(cat_pnls) / len(cat_pnls), 2) if cat_pnls else 0,
        }

    # ── 10. Write JSON ───────────────────────────────────────────────────────────────
    payload = {
        "generated_at": datetime.now(IST).isoformat(),
        "summary":      summary,
        "trades":       sorted(trades, key=lambda t: t["entry_date"], reverse=True),
        "equity_curve": equity_curve,
        "monthly":      monthly,
        "by_scanner":   by_scanner,
        "by_category":  by_category,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PERF_JSON_PATH, "w") as f:
        json.dump(payload, f, default=str)

    logger.info(
        f"✅ PERFORMANCE TRACKER | Dashboard refreshed | "
        f"{len(trades)} alerts | {len(winners)}W / {len(losers)}L / {len(open_p)} open"
    )


# =====================================================================================
# EMPTY FALLBACK
# =====================================================================================

def _write_empty():
    payload = {
        "generated_at": datetime.now(IST).isoformat(),
        "trades":       [],
        "summary": {
            "total_alerts": 0, "judged": 0, "winners": 0, "losers": 0,
            "open_positions": 0, "win_rate": 0, "avg_return_pct": 0,
            "avg_win_pct": 0, "avg_loss_pct": 0, "best_trade_pct": 0,
            "worst_trade_pct": 0, "expectancy": 0,
        },
        "equity_curve": [],
        "monthly":      [],
        "by_scanner":   {},
        "by_category":  {},
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PERF_JSON_PATH, "w") as f:
        json.dump(payload, f)
    logger.info("✅ PERFORMANCE TRACKER | Dashboard refreshed (empty — no alerts yet)")


# =====================================================================================
# STANDALONE RUN
# =====================================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    build_performance_data()
