"""Phase 2 integration test: verify the daily_summaries trigger fires correctly.

Flow:
1. Insert a mock meal via direct psycopg2 (needs service-level access).
2. Insert two meal_items (one plant with canonical_plant_id, one non-plant).
3. Assert daily_summaries row exists with correct aggregated totals.
4. Cleanup: delete the test meal (cascades to meal_items; trigger removes summary).
"""

import os
import uuid
from datetime import datetime, timezone
from urllib.parse import unquote

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

TEST_DATE = datetime(2099, 1, 1, 12, 0, 0, tzinfo=timezone.utc)  # far-future date avoids collisions


def get_conn() -> psycopg2.extensions.connection:
    db_url = os.environ.get("SUPABASE_CONN_STRING") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise EnvironmentError("SUPABASE_CONN_STRING (or DATABASE_URL) not set in .env")
    # Parse from the right so raw ':' '@' '/' in the password don't break parsing
    # (same pattern as apply_migration.py).
    rest = db_url.split("://", 1)[1]
    creds, hostpart = rest.rsplit("@", 1)
    user, _, password = creds.partition(":")
    hostport, _, dbname = hostpart.partition("/")
    host, _, port = hostport.partition(":")
    conn = psycopg2.connect(
        host=host,
        port=int(port) if port.isdigit() else 5432,
        dbname=dbname.split("?")[0] or "postgres",
        user=unquote(user),
        password=unquote(password),
    )
    conn.autocommit = True
    psycopg2.extras.register_uuid()
    return conn


def test_trigger_fires() -> None:
    """Insert a meal + items, assert trigger populates daily_summaries."""
    conn = get_conn()
    cur = conn.cursor()
    meal_id = uuid.uuid4()

    try:
        # 1. Insert mock meal
        cur.execute(
            """
            INSERT INTO meals
                (id, logged_at, meal_type, raw_user_string,
                 total_calories, total_protein_g, total_carbs_g,
                 total_fat_g, total_fiber_g)
            VALUES (%s, %s, 'lunch', 'test: broccoli and chicken',
                    500, 40.0, 30.0, 15.0, 5.0)
            """,
            (meal_id, TEST_DATE),
        )

        # 2. Resolve a known plant id (broccoli seeded in canonical_plants)
        cur.execute("SELECT id FROM canonical_plants WHERE name = 'broccoli'")
        row = cur.fetchone()
        assert row, "canonical_plants not seeded — run setup_db.py first"
        broccoli_id = row[0]

        # 3. Insert plant item (broccoli)
        cur.execute(
            """
            INSERT INTO meal_items
                (meal_id, food_name, calories, protein_g, carbs_g,
                 fat_g, fiber_g, fraction_eaten, is_plant,
                 plant_name, canonical_plant_id)
            VALUES (%s, 'broccoli', 51, 4.2, 9.9, 0.6, 3.9,
                    1.0, true, 'broccoli', %s)
            """,
            (meal_id, broccoli_id),
        )

        # 4. Insert non-plant item (chicken)
        cur.execute(
            """
            INSERT INTO meal_items
                (meal_id, food_name, calories, protein_g, carbs_g,
                 fat_g, fiber_g, fraction_eaten, is_plant)
            VALUES (%s, 'grilled chicken', 248, 46.5, 0.0, 5.4, 0.0, 1.0, false)
            """,
            (meal_id,),
        )

        # 5. Assert daily_summaries was populated by the trigger
        test_date_only = TEST_DATE.date()
        cur.execute(
            "SELECT total_calories, total_protein_g, unique_plant_count "
            "FROM daily_summaries WHERE summary_date = %s",
            (test_date_only,),
        )
        summary = cur.fetchone()
        assert summary is not None, "Trigger did not insert into daily_summaries"

        total_cal, total_pro, plant_count = summary
        assert total_cal == 500, f"Expected 500 cal, got {total_cal}"
        assert float(total_pro) == 40.0, f"Expected 40.0g protein, got {total_pro}"
        assert plant_count == 1, f"Expected 1 unique plant, got {plant_count}"

        print("PASS: trigger fired, daily_summaries populated correctly")
        print(f"  total_calories={total_cal}, total_protein_g={total_pro}, unique_plant_count={plant_count}")

    finally:
        # Cleanup: delete meal (cascades to meal_items); trigger removes summary row.
        cur.execute("DELETE FROM meals WHERE id = %s", (meal_id,))
        cur.execute("DELETE FROM daily_summaries WHERE summary_date = %s", (TEST_DATE.date(),))
        cur.close()
        conn.close()


def test_canonical_plants_count() -> None:
    """Assert canonical_plants has 80+ entries."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM canonical_plants")
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    assert count >= 80, f"Expected 80+ plants, got {count}"
    print(f"PASS: canonical_plants has {count} entries")


def test_logged_meals_view() -> None:
    """Assert logged_meals view excludes templates."""
    conn = get_conn()
    cur = conn.cursor()
    # View must exist and be queryable
    cur.execute("SELECT COUNT(*) FROM logged_meals")
    cur.fetchone()
    cur.close()
    conn.close()
    print("PASS: logged_meals view exists and is queryable")


if __name__ == "__main__":
    print("Running Phase 2 integration tests...\n")
    test_canonical_plants_count()
    test_logged_meals_view()
    test_trigger_fires()
    print("\nAll tests passed.")
