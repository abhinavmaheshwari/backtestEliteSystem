import psycopg2, os
conn = psycopg2.connect(os.getenv("DATABASE_URL", "postgresql://abhinavmaheshwari@localhost/backtest_elite"))
conn.autocommit = True
cur = conn.cursor()
cur.execute("SELECT pid, state, query FROM pg_stat_activity WHERE state != 'idle';")
for row in cur.fetchall():
    print(row)
