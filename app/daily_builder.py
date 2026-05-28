# =====================================================================================
# app/daily_builder.py
#
# WHAT THIS FILE DOES:
#   Constructs the master watchlist dataframe (Stock, Category) from the sector map.
#   Saves the result to a Parquet file atomically to prevent race conditions.
# =====================================================================================

import os
import tempfile
import logging
import pandas as pd

from config import WATCHLIST_PATH

logger = logging.getLogger(__name__)

# =====================================================================================
# MASTER SECTOR MAP
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
# CORE LOGIC
# =====================================================================================

def main():
    """
    Main execution function. Called directly or imported by main.py / eod_scanner.py.
    """
    logger.info("🛠️ Building daily watchlist...")

    # 1. Ensure the target directory exists
    target_dir = os.path.dirname(WATCHLIST_PATH)
    os.makedirs(target_dir, exist_ok=True)

    # 2. Extract symbols directly from the map
    symbols = list(NSE_SECTOR_MAP.keys())

    # 3. Construct DataFrame
    watchlist_data = [
        {"Stock": symbol, "Category": NSE_SECTOR_MAP[symbol]}
        for symbol in symbols
    ]
    df = pd.DataFrame(watchlist_data)

    # 4. Atomic Save (Prevents FileNotFoundError or corrupted reads in threads)
    try:
        # Create a temporary file in the same directory
        fd, temp_path = tempfile.mkstemp(dir=target_dir, suffix=".parquet")
        os.close(fd) # Close the file descriptor so pandas can open it

        # Save to the temporary file
        df.to_parquet(temp_path, index=False)

        # Atomically replace the old watchlist with the new one
        os.replace(temp_path, WATCHLIST_PATH)
        logger.info(f"💾 Watchlist saved atomically to {WATCHLIST_PATH}. Total active stocks: {len(df)}")

    except Exception as e:
        logger.exception(f"❌ Failed to save watchlist: {e}")
        # Clean up temp file if something went wrong
        if os.path.exists(temp_path):
            os.remove(temp_path)

if __name__ == "__main__":
    # Ensure logs print to console if run directly
    logging.basicConfig(level=logging.INFO)
    main()
