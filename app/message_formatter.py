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
    cat      = category_label(a["category"])
    bk       = breakout_lines(a["breakout_signals"])

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



    if delivery_pct is not None:
        if delivery_pct >= 60: deliv_label = "🏦 Institutional"
        elif delivery_pct >= 40: deliv_label = "📦 Positional"
        elif delivery_pct >= 25: deliv_label = "📬 Moderate"
        else: deliv_label = "🔄 Intraday churn"
        delivery_line = f"{delivery_pct:.1f}% {deliv_label}"
    else:
        delivery_line = None

    # ── TRAILING SL / EXIT NOTE ─────────────────────────────────────────────
    trail_note  = a.get("trail_note")

    # ── ASSEMBLE FINAL MESSAGE ──
    lines = [
        _DIV,
        f"🚀 <b>{a['symbol']}</b> {peg_badge}",
        _DIV,
        f"<b>Category:</b> {cat}",
        f"<b>Score:</b> {score_tier(score)}  {score_bar(score)}  ({score}/100)",
        "",
        "<b>📊 Price Action:</b>",
        f"├─ CMP:       ₹{a['price']}",
    ]
    if open_price is not None and day_high is not None and day_low is not None:
        lines.append(f"├─ Day Range: ₹{day_low} - ₹{day_high}")
    if delivery_line:
        lines.append(f"└─ Delivery:  {delivery_line}")
    else:
        lines[-1] = lines[-1].replace("├─", "└─")

    lines.append("")
    lines.append("<b>⚡ Triggers:</b>")
    lines.append(bk)
    
    if moat_block:
        lines.append("")
        lines.append("<b>💎 Fundamentals:</b>")
        lines.append(f"├─ YoY Growth: Rev +{yoy_rev}% | Profit +{yoy_profit}%")
        lines.append(f"└─ Quality:    ROE {roe}%")

    lines.append("")
    lines.append(f"<b>🎯 Execution (R:R {a.get('rr_ratio', 'N/A')}:1):</b>")
    if "atr_stop" in a:
        lines.append(f"├─ SL: ₹{a['atr_stop']}")
    if "target_price" in a:
        lines.append(f"├─ T1: ₹{a['target_price']}")
        if a.get("target_2"):
            lines.append(f"├─ T2: ₹{a['target_2']}")
        if a.get("target_3"):
            lines.append(f"├─ T3: ₹{a['target_3']}")
    lines[-1] = lines[-1].replace("├─", "└─")
    
    cap_alloc = a.get("capital_allocated", 0)
    shares = a.get("shares_bought", 0)
    if cap_alloc and shares:
        lines.append("")
        lines.append("<b>🏦 Portfolio Allocation:</b>")
        lines.append(f"├─ Buy:    {shares} shares")
        lines.append(f"└─ Capital: ₹{cap_alloc:,.2f}")
    
    if trail_note:
        lines.append(f"💡 <i>{trail_note}</i>")
    

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
