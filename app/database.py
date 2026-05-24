# =====================================================================================
# app/database.py
# =====================================================================================

import sqlite3
import os

from config import DB_PATH

# =====================================================================================
# INITIALIZE DATABASE
# =====================================================================================

def init_db():

    # ============================================================================
    # CREATE data/ DIRECTORY IF MISSING
    # ============================================================================

    os.makedirs(
        os.path.dirname(DB_PATH),
        exist_ok=True
    )

    # ============================================================================
    # CONNECT DATABASE
    # ============================================================================

    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()

    # ============================================================================
    # ALERTS TABLE
    # ============================================================================

    c.execute('''

    CREATE TABLE IF NOT EXISTS alerts (

        symbol TEXT,

        breakout_type TEXT,

        alert_time TEXT
    )

    ''')

    conn.commit()

    conn.close()

# =====================================================================================
# CHECK DUPLICATE ALERT
# =====================================================================================

def alert_exists(symbol, breakout_type):

    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()

    c.execute(

        """

        SELECT *

        FROM alerts

        WHERE symbol=?

        AND breakout_type=?

        """,

        (symbol, breakout_type)
    )

    result = c.fetchone()

    conn.close()

    return result is not None

# =====================================================================================
# SAVE ALERT
# =====================================================================================

def save_alert(

    symbol,

    breakout_type,

    alert_time
):

    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()

    c.execute(

        """

        INSERT INTO alerts

        VALUES (?, ?, ?)

        """,

        (
            symbol,
            breakout_type,
            alert_time
        )
    )

    conn.commit()

    conn.close()
