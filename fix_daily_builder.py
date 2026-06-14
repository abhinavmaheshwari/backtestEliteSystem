import re

filename = "app/daily_builder.py"
with open(filename, 'r') as f:
    content = f.read()

# 1. Add description to CAT_DESCRIPTIONS
old_desc = '"Dividend Aristocrat":      "High dividend yield (≥3.0%) with strong ROE and stability.",'
new_desc = '"Dividend Aristocrat":      "High dividend yield (≥3.0%) with strong ROE and stability.",\n    "Inst Accumulation":        "Massive institutional bulk/block buying + high delivery absorption.",'
content = content.replace(old_desc, new_desc)

# 2. Add globals for delivery and inst buys at the top
old_import = "from config import WATCHLIST_PATH"
new_import = """from config import WATCHLIST_PATH

# Globals for accumulation data
_DELIVERY_DATA = {}
_INST_BUYS = {}
"""
content = content.replace(old_import, new_import)

# 3. Add fetching logic inside _main_impl()
old_main = """    logger.info("🚀 ELITE FUNDAMENTAL SCAN STARTED")

    os.makedirs(os.path.dirname(OUTPUT_PARQUET), exist_ok=True)

    universe_df = fetch_universe()"""
new_main = """    logger.info("🚀 ELITE FUNDAMENTAL SCAN STARTED")

    # Fetch institutional and delivery data
    global _DELIVERY_DATA, _INST_BUYS
    try:
        from delivery_data import fetch_previous_day_delivery
        _DELIVERY_DATA = fetch_previous_day_delivery()
        
        from institutional_data import get_institutional_buys
        _INST_BUYS = get_institutional_buys()
    except Exception as e:
        logger.warning(f"⚠️ Could not fetch accumulation data: {e}")

    os.makedirs(os.path.dirname(OUTPUT_PARQUET), exist_ok=True)

    universe_df = fetch_universe()"""
content = content.replace(old_main, new_main)

# 4. Add logic to _classify_nonfin
old_classify_nonfin = """    # NEW: Dividend Aristocrat
    div_val = div_yield if div_yield is not None else 0.0
    dividend_aristocrat = (div_val >= 3.0 and roe >= 15.0 and low_debt and market_cap >= 50_000_000_000)

    # ── DIAMOND HOLD (LONG TERM) LOGIC ──"""

new_classify_nonfin = """    # NEW: Dividend Aristocrat
    div_val = div_yield if div_yield is not None else 0.0
    dividend_aristocrat = (div_val >= 3.0 and roe >= 15.0 and low_debt and market_cap >= 50_000_000_000)

    # NEW: Institutional Accumulation
    deliv_per = _DELIVERY_DATA.get(symbol, 0.0)
    inst_buyers = _INST_BUYS.get(symbol, [])
    inst_accumulation = (deliv_per >= 60.0 and len(inst_buyers) > 0 and opm >= 10.0 and yoy_profit > 0.0)

    # ── DIAMOND HOLD (LONG TERM) LOGIC ──"""
content = content.replace(old_classify_nonfin, new_classify_nonfin)

# Add to the if not any list
old_any_nonfin = "dividend_aristocrat]):"
new_any_nonfin = "dividend_aristocrat, inst_accumulation]):"
content = content.replace(old_any_nonfin, new_any_nonfin)

old_cats_nonfin = """    if dividend_aristocrat:cats.append("Dividend Aristocrat")"""
new_cats_nonfin = """    if dividend_aristocrat:cats.append("Dividend Aristocrat")
    if inst_accumulation:  cats.append("Inst Accumulation")"""
content = content.replace(old_cats_nonfin, new_cats_nonfin)

# 5. Add logic to _classify_fin
old_classify_fin = """    # NEW: Dividend Aristocrat
    div_val = div_yield if div_yield is not None else 0.0
    dividend_aristocrat = (div_val >= 3.0 and roe >= 15.0 and market_cap >= 50_000_000_000)

    diamond_hold = False"""

new_classify_fin = """    # NEW: Dividend Aristocrat
    div_val = div_yield if div_yield is not None else 0.0
    dividend_aristocrat = (div_val >= 3.0 and roe >= 15.0 and market_cap >= 50_000_000_000)

    # NEW: Institutional Accumulation
    deliv_per = _DELIVERY_DATA.get(symbol, 0.0)
    inst_buyers = _INST_BUYS.get(symbol, [])
    inst_accumulation = (deliv_per >= 60.0 and len(inst_buyers) > 0 and roa >= 1.0)

    diamond_hold = False"""
content = content.replace(old_classify_fin, new_classify_fin)

old_any_fin = "dividend_aristocrat]):"
new_any_fin = "dividend_aristocrat, inst_accumulation]):"
content = content.replace(old_any_fin, new_any_fin)

old_cats_fin = """    if dividend_aristocrat:cats.append("Dividend Aristocrat")"""
new_cats_fin = """    if dividend_aristocrat:cats.append("Dividend Aristocrat")
    if inst_accumulation:  cats.append("Inst Accumulation")"""
content = content.replace(old_cats_fin, new_cats_fin)

# 6. Scoring: Let's give a minor score bump to Inst Accumulation
old_score_nonfin = "if turnaround: score += 3"
new_score_nonfin = """if turnaround: score += 3
    if inst_accumulation: score += 10"""
# Need to pass inst_accumulation into the scoring function!
# Wait, let's just add it locally without passing it into the helper func to avoid changing the function signature.
# Actually, the score helper func is cleaner. Let's change the function signature.
# Nevermind, let's just do `score += 10 if inst_accumulation else 0` AFTER calling `_score_nonfin`.

old_score_call_nonfin = "score = _score_nonfin(yoy_sales, yoy_profit, qoq_sales, qoq_profit, roe, opm, debt_equity, yoy_margin_expanding, qoq_margin_expanding, mature_quality, elite_compounder, turnaround)"
new_score_call_nonfin = """score = _score_nonfin(yoy_sales, yoy_profit, qoq_sales, qoq_profit, roe, opm, debt_equity, yoy_margin_expanding, qoq_margin_expanding, mature_quality, elite_compounder, turnaround)
    if inst_accumulation: score += 15"""
content = content.replace(old_score_call_nonfin, new_score_call_nonfin)

old_score_call_fin = "score = _score_fin(yoy_rev, yoy_profit, qoq_rev, qoq_profit, roe, roa, yoy_margin_expanding, fin_mature_quality, fin_compounder)"
new_score_call_fin = """score = _score_fin(yoy_rev, yoy_profit, qoq_rev, qoq_profit, roe, roa, yoy_margin_expanding, fin_mature_quality, fin_compounder)
    if inst_accumulation: score += 15"""
content = content.replace(old_score_call_fin, new_score_call_fin)


with open(filename, 'w') as f:
    f.write(content)
print("Updated daily_builder.py successfully.")

