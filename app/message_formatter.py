# =====================================================================================
# app/message_formatter.py
# TELEGRAM MESSAGE FORMATTER
# parse_mode: HTML  — uses <b>, <i>, <code> tags (safer than Markdown)
# Rules: no box-drawing chars, no block chars — emoji + hyphens only
# =====================================================================================

# =====================================================================================
# SCORE COSMETICS
# =====================================================================================

def score_tier(score):
    if score >= 95: return "ELITE ★★★"
    if score >= 85: return "STRONG ★★"
    if score >= 75: return "SOLID ★"
    return             "WATCH"

def score_badge(score):
    if score >= 95: return "🏆"
    if score >= 85: return "🔥"
    if score >= 75: return "⚡"
    return             "📌"

def score_bar(score):
    filled = round(score / 10)
    return "🟢" * filled + "⚫" * (10 - filled)

# =====================================================================================
# CATEGORY — compact
# =====================================================================================

_CAT_ICON = {
    "Elite Compounder":         "💎",
    "Financial Compounder":     "💎",
    "High Growth":              "📈",
    "Financial High Growth":    "📈",
    "Steady Compounder":        "📊",
    "Mature Quality":           "🏛",
    "Financial Mature Quality": "🏛",
    "Turnaround":               "🔄",
    "Financial Turnaround":     "🔄",
}

def category_icons(category):
    return "  ".join(
        _CAT_ICON.get(c.strip(), c.strip())
        for c in category.split("+")
    )

def category_label(category):
    parts = []
    for c in category.split("+"):
        c = c.strip()
        icon = _CAT_ICON.get(c, "")
        parts.append(f"{icon} {c}")
    return "\n".join(parts)

# =====================================================================================
# BREAKOUT SIGNALS — one per line with emoji
# =====================================================================================

_BK_ICON = {
    "52W Breakout":     "🚀",
    "Monthly Breakout": "🌕",
    "Weekly Breakout":  "📊",
    "Daily Breakout":   "📉",
    "Hourly Breakout":  "⏱",   # 1H timeframe signal
    "Session Breakout": "⚡",   # 15m timeframe signal
    "BB Breakout":      "🎯",   # Bollinger Band squeeze breakout
    "Volume Surge":     "📈",   # Volume × 3 confirmation
}

def breakout_lines(signals):
    return "\n".join(
        f"{_BK_ICON.get(s, '•')} {s}"
        for s in signals
    )

# =====================================================================================
# TREND STRUCTURE BLOCK
# =====================================================================================

def trend_structure_lines(alert):
    lines = []

    above_ema20  = alert.get("above_ema20")
    above_sma50  = alert.get("above_sma50")
    golden_cross = alert.get("golden_cross")

    if above_ema20 is not None:
        icon = "✅" if above_ema20 else "❌"
        lines.append(f"{icon} Above EMA20")
    if above_sma50 is not None:
        icon = "✅" if above_sma50 else "❌"
        lines.append(f"{icon} Above SMA50")
    if golden_cross is not None:
        icon = "✅" if golden_cross else "❌"
        lines.append(f"{icon} Bullish 50/200 DMA (Golden Cross)")

    return "\n".join(lines) if lines else "—"

# =====================================================================================
# SINGLE ALERT BLOCK
# Symbol is wrapped in <b> for bold; rest is plain text (HTML-safe)
# =====================================================================================

_TOP = "= = = = = = = = = = = = = = = = ="
_DIV = "- " * 16

_SCANNER_LABEL = {
    "EOD":      "📊 EOD BREAKOUT ALERT",
    "1H":       "🚀 TREND CONFIRMED ALERT — 1H",
    "INTRADAY": "⚡ EARLY MOMENTUM ALERT — 15M",
}

_BAR_LABEL = {
    "EOD":      "Daily (EOD)",
    "1H":       "1H (completed)",
    "INTRADAY": "15M (completed)",
}

def format_alert(a, scanner="1H"):
    tier     = score_tier(a["score"])
    bar      = score_bar(a["score"])
    cat      = category_label(a["category"])
    bk       = breakout_lines(a["breakout_signals"])
    trend    = trend_structure_lines(a)
    bar_type = _BAR_LABEL.get(scanner, scanner)

    open_price   = a.get("open")
    day_high     = a.get("day_high")
    day_low      = a.get("day_low")
    delivery_pct = a.get("delivery_pct")   # present in EOD alerts; None for intraday/1H

    price_lines = [f"Price:    ₹{a['price']}"]
    if open_price is not None:
        price_lines.append(f"Open:     ₹{open_price}")
    if day_high is not None:
        price_lines.append(f"Day High: ₹{day_high}")
    if day_low is not None:
        price_lines.append(f"Day Low:  ₹{day_low}")
    price_block = "\n".join(price_lines)

    # Delivery conviction label — shown only for EOD alerts
    if delivery_pct is not None:
        if delivery_pct >= 60:
            deliv_label = "🏦 Institutional"
        elif delivery_pct >= 40:
            deliv_label = "📦 Positional"
        elif delivery_pct >= 25:
            deliv_label = "📬 Moderate"
        else:
            deliv_label = "🔄 Intraday churn"
        delivery_line = f"Delivery:         {delivery_pct:.1f}%  {deliv_label}"
    else:
        delivery_line = None

    # <b> tag for bold symbol — HTML parse_mode
    lines = [
        _DIV,
        f"Stock: <b>{a['symbol']}</b>",
        "",
        "Category:",
        cat,
        "",
        "Breakouts:",
        bk,
        "",
        price_block,
        "",
        f"RSI:              {a['rsi']}",
        f"Volume Expansion: {a['volume_ratio']}x",
        f"Candle:           🟢 Bullish | Body {a['body_ratio']}%",
    ]
    if delivery_line:
        lines.append(delivery_line)
    lines += [
        "",
        "Trend Structure:",
        trend,
        "",
        "Breakout Score:",
        f"{a['score']}/100  {tier}",
        bar,
        "",
        f"Bar: {bar_type}",
    ]
    return "\n".join(lines)

# =====================================================================================
# FULL MESSAGE
# =====================================================================================

def build_message(scanner, cat, alerts, chunk_num, total_chunks, scan_time):
    suffix    = f"  [{chunk_num}/{total_chunks}]" if total_chunks > 1 else ""
    cat_icons = category_icons(cat)
    label     = _SCANNER_LABEL.get(scanner, scanner)

    header = "\n".join([
        _TOP,
        f"{label}{suffix}",
        f"{cat_icons}  |  {len(alerts)} alert{'s' if len(alerts) != 1 else ''}",
        _TOP,
    ])

    body = "\n\n".join(format_alert(a, scanner) for a in alerts)

    return f"{header}\n\n{body}\n\n⏰ {scan_time}"
