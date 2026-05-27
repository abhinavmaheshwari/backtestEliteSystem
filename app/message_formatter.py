# =====================================================================================
# app/message_formatter.py
# TELEGRAM MESSAGE FORMATTER — Pure Unicode, zero HTML tags
# Works in all parse modes (HTML / Markdown / none)
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
    # 10-block bar — more granular than 5
    filled = round(score / 10)
    return "▓" * filled + "░" * (10 - filled)

# =====================================================================================
# BREAKOUT COSMETICS
# =====================================================================================

def breakout_label(signals):
    """Shorten signal names and pick the strongest emoji."""
    SHORT = {
        "52W Breakout":     "52W 🚀",
        "Monthly Breakout": "Monthly 🌕",
        "Weekly Breakout":  "Weekly 📈",
        "Daily Breakout":   "Daily  📊",
    }
    return "  ".join(SHORT.get(s, s) for s in signals)

# =====================================================================================
# CATEGORY — compact single-line
# =====================================================================================

_CAT_SHORT = {
    "Elite Compounder": "💎 Elite",
    "High Growth":      "🚀 Growth",
    "Mature Quality":   "🏛 Quality",
}

def category_short(category):
    parts = [_CAT_SHORT.get(c.strip(), c.strip()) for c in category.split("+")]
    return "  ·  ".join(parts)

# =====================================================================================
# SINGLE ALERT BLOCK
# Rendered example (no HTML):
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 🏆  HINDZINC              100
#     ▓▓▓▓▓▓▓▓▓▓  ELITE ★★★
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ₹656.1   RSI 70.7   Vol 1.9x
#  Body 65%   💎 Elite
#
#  📈 Weekly  📊 Daily
# =====================================================================================

_THIN  = "┄" * 32
_THICK = "━" * 32

def format_alert(a):
    badge = score_badge(a["score"])
    bar   = score_bar(a["score"])
    tier  = score_tier(a["score"])
    cat   = category_short(a["category"])
    bk    = breakout_label(a["breakout_signals"])

    # right-align score in header line (symbol + padding + score)
    sym_field = a["symbol"].ljust(18)

    lines = [
        _THICK,
        f"{badge}  {sym_field}{a['score']:>3}/100",
        f"    {bar}  {tier}",
        _THIN,
        f"  ₹{a['price']}   RSI {a['rsi']}   Vol {a['volume_ratio']}x",
        f"  Body {a['body_ratio']}%   {cat}",
        "",
        f"  {bk}",
    ]
    return "\n".join(lines)

# =====================================================================================
# FULL MESSAGE
# =====================================================================================

_SCANNER_ICON = {
    "EOD":      "📊",
    "1H":       "🚀",
    "INTRADAY": "⚡",
}

_SCANNER_LABEL = {
    "EOD":      "EOD DAILY SCAN",
    "1H":       "TREND SCAN 1H",
    "INTRADAY": "INTRADAY 15M",
}

def build_message(scanner, cat, alerts, chunk_num, total_chunks, scan_time):
    """
    scanner : "EOD" | "1H" | "INTRADAY"
    alerts  : list of dicts with keys:
                symbol, price, rsi, volume_ratio, body_ratio,
                score, category, breakout_signals
    """
    icon  = _SCANNER_ICON.get(scanner, "📡")
    label = _SCANNER_LABEL.get(scanner, scanner)
    suffix = f"  [{chunk_num}/{total_chunks}]" if total_chunks > 1 else ""

    cat_line = category_short(cat)

    header = "\n".join([
        f"{icon}  {label}{suffix}",
        "═" * 32,
        f"{cat_line}   ·   {len(alerts)} alert{'s' if len(alerts) != 1 else ''}",
        "═" * 32,
    ])

    body_parts = [format_alert(a) for a in alerts]

    footer = f"\n⏰  {scan_time}"

    return header + "\n\n" + "\n\n".join(body_parts) + "\n" + footer
