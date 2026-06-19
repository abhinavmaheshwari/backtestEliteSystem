import psycopg2
import os

db_url = os.getenv("DATABASE_URL", "postgresql://abhinavmaheshwari@localhost/backtest_elite")
conn = psycopg2.connect(db_url)
conn.autocommit = True
cur = conn.cursor()
cur.execute("DROP VIEW IF EXISTS v_trade_analytics CASCADE;")
print("Dropped view.")
