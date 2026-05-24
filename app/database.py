import sqlite3
from config import DB_PATH


def init_db():

    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()

    c.execute('''
    CREATE TABLE IF NOT EXISTS alerts (
        symbol TEXT,
        breakout_type TEXT,
        alert_time TEXT
    )
    ''')

    conn.commit()
    conn.close()


def alert_exists(symbol, breakout_type):

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute(
        "SELECT * FROM alerts WHERE symbol=? AND breakout_type=?",
        (symbol, breakout_type)
    )

    result = c.fetchone()

    conn.close()

    return result is not None


def save_alert(symbol, breakout_type, alert_time):

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute(
        "INSERT INTO alerts VALUES (?, ?, ?)",
        (symbol, breakout_type, alert_time)
    )

    conn.commit()
    conn.close()
