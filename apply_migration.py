"""One-off: apply sql/migrate_2026_06_11.sql to the live Supabase DB.

Same connection pattern as setup_db.py. Run once, then delete or keep for
the next migration (edit MIGRATION_FILE).
"""

import os
from pathlib import Path
from urllib.parse import unquote

import psycopg2
from dotenv import load_dotenv

load_dotenv()

MIGRATION_FILE = Path(__file__).parent / "sql" / "migrate_2026_07_05_patterns.sql"


def _connect(db_url: str):
    """Parse postgres URI manually from the right so raw special chars in the
    password (':', '@', '/') don't break parsing."""
    rest = db_url.split("://", 1)[1]
    creds, hostpart = rest.rsplit("@", 1)
    user, _, password = creds.partition(":")
    hostport, _, dbname = hostpart.partition("/")
    host, _, port = hostport.partition(":")
    return psycopg2.connect(
        host=host,
        port=int(port) if port.isdigit() else 5432,
        dbname=dbname.split("?")[0] or "postgres",
        user=unquote(user),
        password=unquote(password),
    )


def main() -> None:
    db_url = os.environ.get("SUPABASE_CONN_STRING") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise EnvironmentError("SUPABASE_CONN_STRING (or DATABASE_URL) not set in .env")

    conn = _connect(db_url)
    conn.autocommit = False
    cur = conn.cursor()

    print(f"Applying {MIGRATION_FILE.name}...")
    cur.execute(MIGRATION_FILE.read_text())
    conn.commit()
    print("  OK (committed)")

    # Post-migration verification
    cur.execute("SELECT count(*) FROM canonical_plants")
    total = cur.fetchone()[0]
    cur.execute("SELECT category, count(*) FROM canonical_plants GROUP BY category ORDER BY category")
    cats = cur.fetchall()
    cur.execute("SELECT count(*) FROM canonical_plants WHERE auto_added")
    auto = cur.fetchone()[0]
    cur.execute(
        "SELECT proname FROM pg_proc WHERE proname IN "
        "('undo_last_meal','get_week_plants','record_weekly_weight','get_gap_nudge','log_meal') "
        "ORDER BY proname"
    )
    funcs = [r[0] for r in cur.fetchall()]

    print(f"\ncanonical_plants total: {total} (auto_added: {auto})")
    for c, n in cats:
        print(f"  {c:12s} {n}")
    print(f"functions present: {', '.join(funcs)}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
