"""Apply the full Phase 2 schema to Supabase via direct PostgreSQL connection.

Runs schema.sql, views.sql, triggers.sql, and seed_plants.sql in order.
Requires DATABASE_URL in .env (Project Settings > Database > Connection string).
"""

import os
from pathlib import Path
from urllib.parse import urlparse, unquote

import psycopg2
from dotenv import load_dotenv

load_dotenv()

SQL_DIR = Path(__file__).parent / "sql"
SQL_FILES = ["schema.sql", "views.sql", "triggers.sql", "seed_plants.sql", "functions.sql"]


def apply_schema() -> None:
    """Execute all SQL setup files against the Supabase PostgreSQL database."""
    db_url = os.environ.get("SUPABASE_CONN_STRING") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise EnvironmentError("SUPABASE_CONN_STRING (or DATABASE_URL) not set in .env")

    # Parse manually so special chars in the password don't break URL parsing.
    parsed = urlparse(db_url)
    conn = psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
        user=parsed.username,
        password=unquote(parsed.password or ""),
    )
    conn.autocommit = True
    cur = conn.cursor()

    for filename in SQL_FILES:
        path = SQL_DIR / filename
        sql = path.read_text()
        print(f"Applying {filename}...")
        cur.execute(sql)
        print(f"  OK")

    cur.close()
    conn.close()
    print("\nSchema setup complete.")


if __name__ == "__main__":
    apply_schema()
