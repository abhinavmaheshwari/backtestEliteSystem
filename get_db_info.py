import os
import psycopg2

db_url = os.getenv("DATABASE_URL")
if not db_url:
    print("No DATABASE_URL found.")
    exit(1)

conn = psycopg2.connect(db_url)
cur = conn.cursor()

# Get table sizes
cur.execute("""
    SELECT relname as "Table",
           pg_size_pretty(pg_total_relation_size(relid)) As "Size",
           pg_size_pretty(pg_table_size(relid)) as "Table Size",
           pg_size_pretty(pg_indexes_size(relid)) as "Index Size"
    FROM pg_catalog.pg_statio_user_tables
    ORDER BY pg_total_relation_size(relid) DESC;
""")
print("=== Postgres Table Sizes ===")
for row in cur.fetchall():
    print(f"{row[0]:<30} | {row[1]:<10} | {row[2]:<10} | {row[3]:<10}")

# Get table row counts
cur.execute("""
    SELECT relname, n_live_tup
    FROM pg_stat_user_tables
    ORDER BY n_live_tup DESC;
""")
print("\n=== Postgres Row Counts ===")
for row in cur.fetchall():
    print(f"{row[0]:<30} | {row[1]}")

cur.execute("SELECT * FROM alerts ORDER BY created_at DESC LIMIT 10;")
print("\n=== Recent Alerts ===")
from pprint import pprint
for r in cur.fetchall():
    print(r)

cur.close()
conn.close()
