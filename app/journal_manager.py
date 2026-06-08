import sqlite3
import pandas as pd
from config import DB_PATH

def generate_performance_report():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT * FROM alerts", conn)
    conn.close()
    
    # Calculate performance metrics
    total_trades = len(df)
    # Logic to compare price vs current market price would go here
    return f"📊 <b>Weekly Performance Report</b>\nTotal Trades Tracked: {total_trades}"
