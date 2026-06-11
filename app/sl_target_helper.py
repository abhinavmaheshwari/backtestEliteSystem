# =====================================================================================
# app/sl_target_helper.py
# Centralised Stop-Loss and Target calculation for all scanners.
#
# SL PHILOSOPHY (timeframe-aware):
#   - 15m (INTRADAY)  : 1.0 × ATR  — tight, fast-moving setups
#   - 1h  (1H / LIVE) : 1.5 × ATR  — standard swing buffer
#   - 1d  (EOD)       : 2.0 × ATR  — daily ATR is large; need breathing room
#   - REVERSAL        : 2.0 × ATR  — same as EOD (daily bars)
#
# TARGET PHILOSOPHY:
#   Fixed 2:1 reward-to-risk on every setup.
#   target = entry + 2 × (entry - stop_loss)
#   This gives the dashboard a concrete level to close the trade as a WIN.
# =====================================================================================

ATR_MULTIPLIERS = {
    "15m":      1.0,
    "INTRADAY": 1.0,
    "1h":       1.5,
    "1H":       1.5,
    "1d":       2.0,
    "EOD":      2.0,
    "REVERSAL": 2.0,
}

RR_RATIO = 2.0   # reward : risk


def compute_sl_and_target(
    entry_price: float,
    atr: float | None,
    candle_range: float,
    timeframe: str,
) -> tuple[float, float]:
    """
    Returns (stop_loss, target_price) rounded to 2 decimal places.

    Parameters
    ----------
    entry_price  : candle close at time of alert
    atr          : ATR value from indicator column (None → fallback to candle_range)
    candle_range : High - Low of the signal candle (fallback when ATR missing)
    timeframe    : one of "15m", "1h", "1d", "INTRADAY", "1H", "EOD", "REVERSAL"
    """
    multiplier = ATR_MULTIPLIERS.get(timeframe, 1.5)
    effective_atr = atr if (atr is not None and atr > 0) else (candle_range * 1.5)

    risk        = multiplier * effective_atr
    stop_loss   = round(entry_price - risk, 2)
    target      = round(entry_price + RR_RATIO * risk, 2)

    return stop_loss, target
