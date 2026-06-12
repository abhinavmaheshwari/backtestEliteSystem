# =====================================================================================
# app/message_formatter.py
# TELEGRAM MESSAGE FORMATTER WITH FORENSIC VALUATION
# =====================================================================================

# =====================================================================================
# SCORE COSMETICS
# =====================================================================================

def score_tier(score):
    if score >= 95: return "ELITE ★★★"
    if score >= 85: return "STRONG ★★"
    if score >= 75: return "SOLID ★"
    return             "WATCH"

def score_bar(score):
    filled = round(score / 10)
    return "🟢" * filled + "⚫" * (10 - filled)

# =====================================================================================
# CATEGORY — compact
# =====================================================================================

_CAT_ICON = {
    "Dividend Aristocrat":       "💸",
    "Debt-Free Cash Generator":  "💰",
    "Long Term Compounder":      "💎",
    "Wealth Compounder":         "🏆",
    "Top Bank/NBFC":             "🏦",
    "Capital Efficient":         "⚙️",
    "Efficient Lender":          "🏦",
    "Undervalued Growth":        "⚖️",
    "High Momentum":             "🚀",
    "Fast Growing Financial":    "📈",
    "Consistent Performer":      "📊",
    "Blue Chip Stable":          "🏛",
    "Blue Chip Financial":       "🏛",
    "Recovery Play":             "🔄",
    "Financial Recovery":        "🔄",
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
# BREAKOUT SIGNALS
# =====================================================================================

_BK_ICON = {
    "52W Breakout":     "🚀",
    "Monthly Breakout": "🌕",
    "Weekly Breakout":  "📊",
    "Daily Breakout":   "📉",
    "Hourly Breakout":  "⏱",   
    "Session Breakout": "⚡",   
    "BB Breakout":      "🎯",   
    "Volume Surge":     "📈",   
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
# =====================================================================================

_TOP = "= = = = = = = = = = = = = = = = ="
_DIV = "- " * 16

_SCANNER_LABEL = {
    "EOD":      "📊 EOD BREAKOUT ALERT",
    "1H":       "🚀 TREND CONFIRMED ALERT — 1H",
    "INTRADAY": "⚡ EARLY MOMENTUM ALERT — 15M",
    "REVERSAL": "🔄 DEEP VALUE REVERSAL ALERT",
}

_BAR_LABEL = {
    "EOD":      "Daily (EOD)",
    "1H":       "1H (completed)",
    "INTRADAY": "15M (completed)",
    "REVERSAL": "Daily (Mean Reversion)",
}

def format_alert(a, scanner="1H"):
    tier     = score_tier(a["score"])
    bar      = score_bar(a["score"])
    cat      = category_label(a["category"])
    bk       = breakout_lines(a["breakout_signals"])
    trend    = trend_structure_lines(a)
    bar_type = _BAR_LABEL.get(scanner, scanner)

    # ── VALUATION BADGE (PEG) ──
    peg = a.get("peg")
    peg_badge = ""
    if peg is not None:
        if peg < 1.0:
            peg_badge = " 🔥 <b>[DEEP VALUE]</b>"
        elif peg <= 1.5:
            peg_badge = " ✅ <b>[FAIR VALUE]</b>"
        elif peg >= 2.0:
            peg_badge = " ⚠️ <b>[PREMIUM]</b>"

    # ── FUNDAMENTAL MOAT BLOCK ──
    yoy_rev = a.get("yoy_rev")
    yoy_profit = a.get("yoy_profit")
    roe = a.get("roe")
    
    moat_block = ""
    if yoy_rev is not None and yoy_profit is not None:
        moat_lines = ["", "📊 <b>Fundamental Engine:</b>"]
        moat_lines.append(f"├─ YoY Revenue: +{yoy_rev}%")
        moat_lines.append(f"├─ YoY Profit:  +{yoy_profit}%")
        if roe: moat_lines.append(f"└─ ROE:         {roe}%")
        moat_block = "\n".join(moat_lines)

    # ── PRICE & DELIVERY ──
    open_price   = a.get("open")
    day_high     = a.get("day_high")
    day_low      = a.get("day_low")
    delivery_pct = a.get("delivery_pct")   

    price_lines = [f"Price:    ₹{a['price']}"]
    if open_price is not None: price_lines.append(f"Open:     ₹{open_price}")
    if day_high is not None:   price_lines.append(f"Day High: ₹{day_high}")
    if day_low is not None:    price_lines.append(f"Day Low:  ₹{day_low}")
    if "atr_stop" in a:
        price_lines.append(f"Stop Loss: ₹{a['atr_stop']}")
    if "target_price" in a:
        price_lines.append(f"Target 1:  ₹{a['target_price']}  (primary)")
        if a.get("target_2"):
            price_lines.append(f"Target 2:  ₹{a['target_2']}")
        if a.get("target_3"):
            price_lines.append(f"Target 3:  ₹{a['target_3']}  (extended)")
    if a.get("rr_ratio"):
        price_lines.append(f"R:R Ratio: {a['rr_ratio']}:1")

    price_block = "\n".join(price_lines)

    if delivery_pct is not None:
        if delivery_pct >= 60: deliv_label = "🏦 Institutional"
        elif delivery_pct >= 40: deliv_label = "📦 Positional"
        elif delivery_pct >= 25: deliv_label = "📬 Moderate"
        else: deliv_label = "🔄 Intraday churn"
        delivery_line = f"Delivery:         {delivery_pct:.1f}%  {deliv_label}"
    else:
        delivery_line = None

    # ── TRAILING SL / EXIT NOTE ─────────────────────────────────────────────
    trail_note  = a.get("trail_note")
    trail_block = f"\n💡 <b>Exit Plan:</b> {trail_note}" if trail_note else ""

    # ── ASSEMBLE FINAL MESSAGE ──
    lines = [
        _DIV,
        f"Stock: <b>{a['symbol']}</b>{peg_badge}",
        "",
        "Category:",
        cat,
        "",
        "Technical Triggers:",
        bk,
        "",
        price_block,
    ]
    if trail_block:
        lines.append(trail_block)
    lines += [
        "",
        f"RSI:              {a['rsi']}",
        f"Volume Expansion: {a['volume_ratio']}x",
        f"Candle:           🟢 Bullish | Body {a['body_ratio']}%",
    ]
    if delivery_line:
        lines.append(delivery_line)

    if moat_block:
        lines.append(moat_block)

    lines += [
        "",
        "Trend Structure:",
        trend,
        "",
        "Setup Score:",
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
