# =====================================================================================
# app/database.py
# PERMANENT TRADE JOURNAL & ALERT STORAGE
# =====================================================================================

import sqlite3
from datetime import datetime
from config import DB_PATH

def init_db():
    """Initializes the database with an expanded schema for Journaling/Backtesting."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Added 'status' and 'pnl' fields for future backtesting/journaling integration
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            alert_date TEXT,
            alert_time TEXT,
            breakout_type TEXT,
            price REAL,
            atr_stop REAL,
            status TEXT DEFAULT 'OPEN',
            pnl REAL DEFAULT 0.0
        )
    ''')
    conn.commit()
    conn.close()

def save_alert_if_new(symbol: str, breakout_type: str, alert_time: str, price: float, atr_stop: float) -> bool:
    """Saves alert with technical context for journaling."""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check for duplicates within the same day for the same setup
    cursor.execute("SELECT id FROM alerts WHERE symbol=? AND alert_date=? AND breakout_type=?", 
                   (symbol, today, breakout_type))
    if cursor.fetchone():
        conn.close()
        return False
        
    cursor.execute('''
        INSERT INTO alerts (symbol, alert_date, alert_time, breakout_type, price, atr_stop)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (symbol, today, alert_time, breakout_type, price, atr_stop))
    
    conn.commit()
    conn.close()
    return True

def get_last_alert_data(symbol: str):
    """Retrieves the last recorded technical context for a symbol."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT price, atr_stop FROM alerts WHERE symbol = ? ORDER BY id DESC LIMIT 1", (symbol,))
    row = cursor.fetchone()
    conn.close()
    return {"price": row[0], "atr_stop": row[1]} if row else None
