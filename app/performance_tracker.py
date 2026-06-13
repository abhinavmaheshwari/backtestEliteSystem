# =====================================================================================
# app/performance_tracker.py
# Builds performance_data.json from the Postgres alerts table + live yfinance prices.
# Called every 5 minutes from main.py.
#
# SL / TARGET DETECTION LOGIC
# ────────────────────────────
# Both SL and Target are detected using intraday (1h) bars filtered to >= alert_time.
# This means:
#   • Any low printed BEFORE the alert on the same day is IGNORED for SL.
#   • Any high printed BEFORE the alert on the same day is IGNORED for Target.
#
# Priority:
#   1. SL hit first  → status = LOSS  (locked at stop_loss price)
#   2. Target hit first → status = WIN  (locked at target_price)
#   3. Neither hit   → mark-to-market vs current close
#
# To determine which hit first, we compare the timestamps of the first SL-breach
# candle and the first Target-breach candle.
# =====================================================================================

import os
import json
import logging
from typing import Optional, Tuple
import pandas as pd
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

from database import get_all_alerts, update_alert_outcome, upsert_scanner_health, save_system_state

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

try:
    from config import DATA_DIR
except ImportError:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = os.path.join(BASE_DIR, "data")

PERF_JSON_PATH = os.path.join(DATA_DIR, "performance_data.json")

# Time-based auto-exit is disabled; holdings are kept open until SL or Target is hit.
# HOLD_DAYS = 5


# =====================================================================================
# HELPERS
# =====================================================================================

def _parse_dedup_key(breakout_type: str) -> tuple[str, str, str]:
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


def _fetch_post_alert_bars(symbol: str, alert_time_str: str) -> Optional[pd.DataFrame]:
    """
    Fetch 1h bars for *symbol* from the alert date to today.
    Returns a DataFrame with timezone-aware IST index, or None on failure.

    All candles whose open-time < alert_time are DROPPED — this is the core rule
    that prevents pre-alert price action from triggering SL or Target.
    """
    try:
        alert_dt_naive = datetime.fromisoformat(alert_time_str)
        alert_dt_ist   = alert_dt_naive.replace(tzinfo=IST)
        alert_date     = alert_dt_ist.date()

        # Guard: if alert is from today and market hasn't opened yet (before 09:15 IST),
        # no 1h bars exist — return None immediately to avoid yfinance "delisted" noise.
        now_ist = datetime.now(IST)
        market_open_ist = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
        if alert_date == now_ist.date() and now_ist < market_open_ist:
            logger.debug(f"⏳ {symbol} | Alert is from today but market not open yet — skipping bar fetch")
            return None

        ticker_sym = symbol if symbol.endswith(".NS") else f"{symbol}.NS"
        start_str  = alert_date.isoformat()
        end_str    = (date.today() + timedelta(days=1)).isoformat()

        hist = yf.download(
            ticker_sym,
            start=start_str,
            end=end_str,
            interval="1h",
            auto_adjust=True,
            progress=False,
        )

        if hist.empty:
            return None

        if isinstance(hist.columns, pd.MultiIndex):
            hist.columns = hist.columns.get_level_values(0)

        if not {"High", "Low", "Close"}.issubset(hist.columns):
            return None

        # Localise index to IST
        idx = hist.index
        if idx.tzinfo is None:
            idx = idx.tz_localize("Asia/Kolkata")
        else:
            idx = idx.tz_convert("Asia/Kolkata")
        hist.index = idx

        # Drop all candles that opened before the alert timestamp
        hist = hist[hist.index >= alert_dt_ist].copy()

        return hist if not hist.empty else None

    except Exception:
        logger.debug(f"⚠️ Could not fetch bars for {symbol} (alert={alert_time_str})")
        return None


def _check_sl_and_target(
    hist: pd.DataFrame,
    stop_loss: float,
    target_price: float,
) -> Tuple[str, Optional[float], Optional[str]]:
    """
    Walk through post-alert 1h bars in chronological order.
    Returns (outcome, exit_price, hit_time) where outcome is one of:
        "SL_HIT"     — stop_loss breached first
        "TARGET_HIT" — target_price breached first
        "OPEN"       — neither hit yet
    exit_price is the SL or target level (not the candle close) when hit.
    hit_time is the formatted string of the candle timestamp when hit.
    """
    for ts, row in hist.iterrows():
        open_price = float(row["Open"])
        low  = float(row["Low"])
        high = float(row["High"])

        # Check SL first (conservative — protect capital before counting gains)
        if low <= stop_loss:
            # If the candle opened below the SL (e.g., gap down), we take the loss 
            # at the open price, not the theoretical SL level.
            exit_price = open_price if open_price < stop_loss else stop_loss
            return "SL_HIT", exit_price, ts.strftime("%Y-%m-%d %H:%M:%S")

        # Then check target
        if high >= target_price:
            # Similarly, if it gapped up above target, book at open
            exit_price = open_price if open_price > target_price else target_price
            return "TARGET_HIT", exit_price, ts.strftime("%Y-%m-%d %H:%M:%S")

    return "OPEN", None, None


def _days_held(alert_date_str: str) -> int:
    try:
        return (date.today() - date.fromisoformat(alert_date_str)).days
    except Exception:
        return 0


def _trade_status(
    pnl_pct: Optional[float],
    days: int,
    stopped_out: bool,
    target_hit: bool,
) -> str:
    if stopped_out:
        return "LOSS"
    if target_hit:
        return "WIN"
    # Keep positions open until SL or Target is hit (no time-based auto-exit)
    return "OPEN"


# =====================================================================================
# MAIN BUILD FUNCTION
# =====================================================================================

def build_performance_data():
    logger.info("=" * 70)
    logger.info("📊 PERFORMANCE TRACKER | Building performance data...")
    logger.info("=" * 70)

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

    # ── 1. Build trade objects ───────────────────────────────────────────────────────
    trades = []
    for row in raw_alerts:
        symbol      = row["symbol"]
        alert_time  = row.get("alert_time") or ""
        alert_date  = row.get("alert_date") or (alert_time[:10] if alert_time else "")
        # Cast to float immediately — psycopg2 returns REAL/NUMERIC as decimal.Decimal
        # and mixing Decimal with float in arithmetic raises TypeError.
        def _f(v):
            return float(v) if v is not None else None

        entry_price = _f(row.get("entry_price"))

        cat_stored     = row.get("category")
        scanner_stored = row.get("scanner")
        sig_stored     = row.get("signals")

        category, signals, scanner = _parse_dedup_key(row["breakout_type"])
        if cat_stored:     category = cat_stored
        if scanner_stored: scanner  = scanner_stored
        if sig_stored:     signals  = sig_stored

        trades.append({
            "id":            row["id"],          # needed for write-back
            "symbol":        symbol,
            "scanner":       scanner,
            "category":      category,
            "signals":       signals,
            "entry_date":    alert_date,
            "alert_time":    alert_time,
            "entry_price":   entry_price,
            "stop_loss":     _f(row.get("stop_loss")),
            "target_price":  _f(row.get("target_price")),
            "current_price": None,
            "exit_price":    _f(row.get("exit_price")),   # pre-filled if already closed
            "pnl_pct":       _f(row.get("pnl_pct")),      # pre-filled if already closed
            "stopped_out":   row.get("status") == "LOSS",
            "target_hit":    row.get("status") == "WIN",
            "days_held":     _days_held(alert_date),
            "status":        row.get("status") or "OPEN",
            "shares_bought": row.get("shares_bought", 0),
            "capital_allocated": _f(row.get("capital_allocated")),
            "pnl_rs":        _f(row.get("pnl_rs")),
            "score":         row.get("score"),
            "rsi":           _f(row.get("rsi")),
            "volume_ratio":  _f(row.get("volume_ratio")),
            "closed_at":     row.get("closed_at"),        # ISO timestamp when SL/Target locked
            "_db_closed":    row.get("status") in ("WIN", "LOSS"),  # internal flag
        })

    # ── 2. Fetch current prices ──────────────────────────────────────────────────────
    unique_symbols = list({t["symbol"] for t in trades})
    logger.info(f"📈 Fetching current prices for {len(unique_symbols)} symbols...")
    current_prices = _fetch_current_prices(unique_symbols)

    # ── 3. Per-trade SL + Target detection via post-alert intraday bars ─────────────
    logger.info("📉 Checking SL / Target levels via post-alert intraday bars...")

    for t in trades:
        sym        = t["symbol"]
        ep         = t["entry_price"]
        sl         = t["stop_loss"]
        tp         = t["target_price"]
        alert_time = t["alert_time"]
        cur_p      = current_prices.get(sym)

        t["current_price"] = round(cur_p, 2) if cur_p else None

        # ── Already closed in DB — no bar download needed ────────────────────────
        if t["_db_closed"]:
            # pnl_pct and exit_price already populated from DB above
            # Just refresh current_price for display; status stays locked
            logger.debug(f"⏭️  {sym} already closed ({t['status']}) — skipping bar fetch")
            continue

        # FIX: use `is None` (not falsy check) so ep=0.0 doesn't misfire.
        # When ep is None we cannot compute any P&L — mark status and move on.
        if ep is None:
            t["pnl_pct"] = None
            t["status"]  = _trade_status(None, t["days_held"], False, False)
            continue

        if sl and tp and alert_time:
            # ── Full SL + Target detection ───────────────────────────────────────
            hist = _fetch_post_alert_bars(sym, alert_time)

            if hist is not None:
                outcome, exit_p, hit_time = _check_sl_and_target(hist, sl, tp)

                if outcome == "SL_HIT":
                    t["stopped_out"] = True
                    t["exit_price"]  = exit_p
                    t["pnl_pct"]     = round((exit_p - ep) / ep * 100, 2)
                    t["pnl_rs"]      = t["shares_bought"] * (exit_p - ep) if t["shares_bought"] else 0.0
                    t["closed_at"]   = hit_time
                    logger.debug(f"🛑 {sym} SL HIT | entry={ep} sl={sl} pnl={t['pnl_pct']}%")
                    update_alert_outcome(t["id"], "LOSS", exit_p, t["pnl_pct"], pnl_rs=t["pnl_rs"], closed_at=hit_time)

                elif outcome == "TARGET_HIT":
                    t["target_hit"] = True
                    t["exit_price"] = exit_p
                    t["pnl_pct"]    = round((exit_p - ep) / ep * 100, 2)
                    t["pnl_rs"]      = t["shares_bought"] * (exit_p - ep) if t["shares_bought"] else 0.0
                    t["closed_at"]   = hit_time
                    logger.debug(f"🎯 {sym} TARGET HIT | entry={ep} target={tp} pnl={t['pnl_pct']}%")
                    update_alert_outcome(t["id"], "WIN", exit_p, t["pnl_pct"], pnl_rs=t["pnl_rs"], closed_at=hit_time)

                else:
                    # Still open — mark-to-market
                    t["pnl_pct"] = round((cur_p - ep) / ep * 100, 2) if cur_p else None

            else:
                # No bar data — fall back to current price
                t["pnl_pct"] = round((cur_p - ep) / ep * 100, 2) if cur_p else None

        elif sl and alert_time:
            # SL only (no target stored — legacy or partial row)
            hist = _fetch_post_alert_bars(sym, alert_time)
            if hist is not None and not hist.empty:
                lowest_low = float(hist["Low"].min())
                if lowest_low <= sl:
                    t["stopped_out"] = True
                    t["exit_price"]  = sl
                    t["pnl_pct"]     = round((sl - ep) / ep * 100, 2)
                    # Find the first candle that breached the Stop Loss
                    hit_row = hist[hist["Low"] <= sl]
                    hit_time = hit_row.index[0].strftime("%Y-%m-%d %H:%M:%S") if not hit_row.empty else None
                    t["pnl_rs"]      = t["shares_bought"] * (sl - ep) if t["shares_bought"] else 0.0
                    t["closed_at"]   = hit_time
                    update_alert_outcome(t["id"], "LOSS", sl, t["pnl_pct"], pnl_rs=t["pnl_rs"], closed_at=hit_time)
                elif cur_p:
                    t["pnl_pct"] = round((cur_p - ep) / ep * 100, 2)
            elif cur_p:
                t["pnl_pct"] = round((cur_p - ep) / ep * 100, 2)

        elif cur_p:
            # Legacy alert — no SL/Target at all
            t["pnl_pct"] = round((cur_p - ep) / ep * 100, 2)

        t["status"] = _trade_status(
            t["pnl_pct"], t["days_held"], t["stopped_out"], t["target_hit"]
        )

    # ── 4. Summary stats ────────────────────────────────────────────────────────────
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

    # SL vs Target breakdown
    sl_closed     = [t for t in judged if t["stopped_out"]]
    target_closed = [t for t in judged if t["target_hit"]]

    summary = {
        "total_alerts":      len(trades),
        "judged":            n_judged,
        "winners":           len(winners),
        "losers":            len(losers),
        "open_positions":    len(open_p),
        "sl_triggered":      len(sl_closed),
        "target_hit":        len(target_closed),
        "win_rate":          wr,
        "avg_return_pct":    avg_ret,
        "avg_win_pct":       avg_win,
        "avg_loss_pct":      avg_loss,
        "best_trade_pct":    best,
        "worst_trade_pct":   worst,
        "expectancy":        expectancy,
    }

    # ── 5. Equity curve ─────────────────────────────────────────────────────────────
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
                "close_reason":      "SL" if t["stopped_out"] else ("TARGET" if t["target_hit"] else "TIME"),
            })

    # ── 6. Monthly breakdown ────────────────────────────────────────────────────────
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

    monthly = [
        {
            "month":    m,
            "alerts":   v["alerts"],
            "wins":     v["wins"],
            "win_rate": round(v["wins"] / v["alerts"] * 100, 1) if v["alerts"] else 0,
            "avg_return": round(sum(v["pnls"]) / len(v["pnls"]), 2) if v["pnls"] else 0,
        }
        for m in sorted(mmap)
        for v in [mmap[m]]
    ]

    # ── 7. By scanner ────────────────────────────────────────────────────────────────
    all_scanners = {t["scanner"] for t in trades}
    by_scanner = {}
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

    # ── 8. By category ───────────────────────────────────────────────────────────────
    all_cats = {t["category"] for t in trades}
    by_category = {}
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

    # Strip internal tracking flag before serialising
    for t in trades:
        t.pop("_db_closed", None)
        t.pop("id", None)

    # ── 9. Write scanner health to Postgres (source of truth) ──────────────────
    today_str = date.today().isoformat()
    for sc in all_scanners:
        sc_today = [t for t in trades if t["scanner"] == sc and t["entry_date"] == today_str]
        try:
            # We pass last_success=None so that the DB preserves the actual heartbeat
            # timestamps updated directly by the scanner loops.
            upsert_scanner_health(
                scanner_name  = sc,
                status        = "OK",
                last_success  = None,
                today_alerts  = len(sc_today),
                error_msg     = None,
            )
        except Exception:
            logger.warning(f"⚠️ Could not update scanner_health for {sc}")

    # ── 10. Write DB State ───────────────────────────────────────────────────────────
    payload = {
        "generated_at": datetime.now(IST).isoformat(),
        "summary":      summary,
        "trades":       sorted(trades, key=lambda t: t["entry_date"], reverse=True),
        "equity_curve": equity_curve,
        "monthly":      monthly,
        "by_scanner":   by_scanner,
        "by_category":  by_category,
    }

    try:
        payload_str = json.dumps(payload, default=str)
        save_system_state("performance_data", payload_str)
        logger.info("✅ PERFORMANCE TRACKER | Stored performance metrics in PostgreSQL")
    except Exception:
        logger.exception("❌ PERFORMANCE TRACKER | Failed to store performance metrics in DB")

    logger.info(
        f"✅ PERFORMANCE TRACKER | {len(trades)} alerts | "
        f"{len(winners)}W / {len(losers)}L / {len(open_p)} OPEN | "
        f"SL triggers={len(sl_closed)} | Target hits={len(target_closed)}"
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
            "open_positions": 0, "sl_triggered": 0, "target_hit": 0,
            "win_rate": 0, "avg_return_pct": 0, "avg_win_pct": 0,
            "avg_loss_pct": 0, "best_trade_pct": 0, "worst_trade_pct": 0,
            "expectancy": 0,
        },
        "equity_curve": [],
        "monthly":      [],
        "by_scanner":   {},
        "by_category":  {},
        "scanner_stats": {},
    }
    try:
        save_system_state("performance_data", json.dumps(payload, default=str))
        logger.info("✅ PERFORMANCE TRACKER | Stored empty performance metrics in PostgreSQL")
    except Exception:
        logger.exception("❌ PERFORMANCE TRACKER | Failed to store empty metrics in DB")


# =====================================================================================
# STANDALONE RUN
# =====================================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    build_performance_data()
