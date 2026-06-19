import os
import argparse
import psycopg2
from pathlib import Path

# Ensure we are loading the .env from the project root if it exists
ROOT_DIR = Path(__file__).resolve().parent.parent
env_path = ROOT_DIR / ".env"
if env_path.exists():
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip().strip("'\"")

DUMP_DIR = ROOT_DIR / "db_dump"


def get_connection():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ ERROR: DATABASE_URL not found in environment or .env file.")
        exit(1)
    try:
        return psycopg2.connect(db_url)
    except Exception as e:
        print(f"❌ ERROR connecting to database: {e}")
        exit(1)

def get_all_tables(cur):
    cur.execute("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public' 
          AND table_type = 'BASE TABLE'
    """)
    return [row[0] for row in cur.fetchall()]

def export_data():
    DUMP_DIR.mkdir(exist_ok=True)
    conn = get_connection()
    cur = conn.cursor()
    
    tables = get_all_tables(cur)
    if not tables:
        print("⚠️ No tables found in the database.")
        return

    print(f"📦 Exporting {len(tables)} tables to {DUMP_DIR} ...")
    for table in tables:
        file_path = DUMP_DIR / f"{table}.csv"
        print(f"  → Exporting {table} ...", end=" ", flush=True)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                cur.copy_expert(f"COPY {table} TO STDOUT WITH CSV HEADER", f)
            print("✅")
        except Exception as e:
            print(f"❌ Failed: {e}")

    conn.close()
    print("\n🎉 EXPORT COMPLETE! Commit the `db_dump` folder to your git repo.")

def import_data():
    if not DUMP_DIR.exists():
        print(f"❌ ERROR: Dump directory {DUMP_DIR} not found. Run --export first on the source server.")
        exit(1)

    csv_files = list(DUMP_DIR.glob("*.csv"))
    if not csv_files:
        print(f"❌ ERROR: No CSV files found in {DUMP_DIR}.")
        exit(1)

    # We must ensure the tables exist first. We can import the main init_db from the app.
    print("🛠️  Initializing database schema...")
    try:
        import sys
        sys.path.append(str(ROOT_DIR / "app"))
        from database import init_db
        init_db()
    except Exception as e:
        print(f"⚠️  Could not automatically initialize schema (ensure you are running from the project root): {e}")

    conn = get_connection()
    cur = conn.cursor()
    
    print(f"📥 Importing {len(csv_files)} tables from {DUMP_DIR} ...")
    
    for file_path in csv_files:
        table = file_path.stem
        print(f"  → Importing {table} ...", end=" ", flush=True)
        try:
            # Clear existing data first
            cur.execute(f"TRUNCATE TABLE {table} CASCADE")
            with open(file_path, "r", encoding="utf-8") as f:
                cur.copy_expert(f"COPY {table} FROM STDIN WITH CSV HEADER", f)
            print("✅")
        except Exception as e:
            print(f"❌ Failed: {e}")
            conn.rollback()
            continue
            
    conn.commit()
    conn.close()
    print("\n🎉 IMPORT COMPLETE! Your database is now populated on the new server.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Transfer Elite Breakout System database via CSV.")
    parser.add_argument("--export", action="store_true", help="Export all tables to db_dump/ folder")
    parser.add_argument("--import", dest="do_import", action="store_true", help="Import all tables from db_dump/ folder")
    
    args = parser.parse_args()
    
    if args.export:
        export_data()
    elif args.do_import:
        import_data()
    else:
        print("Please specify either --export or --import")
        parser.print_help()
