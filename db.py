"""Local SQLite storage for ShiftPilot.

Run this module directly to reset ``shiftpilot.db`` and load the demo
dataset. Import it to use the helper query and update functions.

Schema notes:
    * ``workers.hours_worked_this_week`` and ``workers.last_shift_end_time``
      are denormalized running totals rather than derived from ``shifts``.
      That keeps the deterministic rule checks in ``rules.py`` a single
      cheap read per worker, and keeps the seed data trivially reproducible.
    * ``locations`` models a 5-outlet chain across Singapore. Workers have
      a home outlet (``location_id``) AND a home address
      (``home_lat``/``home_lon``) -- the latter is what the deterministic
      distance/pay-premium calculation in ``rules.py`` uses, independent
      of which outlet someone normally works at.
    * ``ad_hoc_offers`` is the audit trail of every past last-minute cover
      request and its outcome. It is the ONLY source of truth for a
      worker's reliability track record -- nothing in the app is allowed
      to claim "has never declined before" unless a real row backs it up.
    * ``settings`` is a tiny key/value store for manager-editable policy,
      currently just the pay-premium ceiling.
    * Every row returned by the ``get_*`` functions is a ``sqlite3.Row``,
      which supports both ``row["col"]`` and ``dict(row)``.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path


DB_PATH = Path(__file__).with_name("shiftpilot.db")

DEFAULT_MAX_PREMIUM_MULTIPLIER = 1.5

# Bump this whenever create_schema()'s table shapes change. Stored in the
# database itself via SQLite's built-in PRAGMA user_version (no extra
# table needed). A stale .db file left over from an older version of this
# app -- e.g. from before the 5-outlet schema existed -- won't have this
# set to the current value, so needs_reset() catches it instead of the
# app crashing with "no such table" the first time a new column/table is
# queried.
SCHEMA_VERSION = 2


def get_connection() -> sqlite3.Connection:
    """Return a configured connection to the ShiftPilot database."""
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def needs_reset() -> bool:
    """True if there's no database file yet, or the one on disk predates
    the current schema (e.g. a leftover .db from an older version of the
    app). Callers should reset_database() when this is True rather than
    querying tables that may not exist."""
    if not DB_PATH.exists():
        return True
    try:
        with get_connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        return version != SCHEMA_VERSION
    except sqlite3.Error:
        return True


def create_schema() -> None:
    """Drop existing ShiftPilot tables and create a clean schema."""
    with get_connection() as connection:
        connection.executescript(
            """
            DROP TABLE IF EXISTS ad_hoc_offers;
            DROP TABLE IF EXISTS shifts;
            DROP TABLE IF EXISTS workers;
            DROP TABLE IF EXISTS locations;
            DROP TABLE IF EXISTS settings;

            CREATE TABLE locations (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                address TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL
            );

            CREATE TABLE workers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL CHECK (role IN ('Barista', 'Cashier')),
                location_id INTEGER NOT NULL,
                home_lat REAL NOT NULL,
                home_lon REAL NOT NULL,
                hours_worked_this_week REAL NOT NULL CHECK (hours_worked_this_week >= 0),
                last_shift_end_time TEXT NOT NULL,
                phone TEXT NOT NULL,
                hourly_rate REAL NOT NULL CHECK (hourly_rate >= 0),
                FOREIGN KEY (location_id) REFERENCES locations(id)
            );

            CREATE TABLE shifts (
                id INTEGER PRIMARY KEY,
                worker_id INTEGER NOT NULL,
                original_worker_id INTEGER NOT NULL,
                location_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('Barista', 'Cashier')),
                status TEXT NOT NULL
                    CHECK (status IN ('scheduled', 'sick', 'completed', 'covered')),
                FOREIGN KEY (worker_id) REFERENCES workers(id),
                FOREIGN KEY (original_worker_id) REFERENCES workers(id),
                FOREIGN KEY (location_id) REFERENCES locations(id)
            );

            CREATE TABLE ad_hoc_offers (
                id INTEGER PRIMARY KEY,
                worker_id INTEGER NOT NULL,
                shift_role TEXT NOT NULL,
                shift_hours REAL NOT NULL,
                base_rate REAL NOT NULL,
                offered_rate REAL NOT NULL,
                distance_km REAL NOT NULL,
                notice_hours REAL NOT NULL,
                outcome TEXT NOT NULL CHECK (outcome IN ('accepted', 'declined')),
                reasoning TEXT,
                occurred_at TEXT NOT NULL,
                FOREIGN KEY (worker_id) REFERENCES workers(id)
            );

            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX idx_shifts_date ON shifts(date);
            CREATE INDEX idx_shifts_worker_id ON shifts(worker_id);
            CREATE INDEX idx_workers_location ON workers(location_id);
            CREATE INDEX idx_offers_worker ON ad_hoc_offers(worker_id);
            """
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def seed_data() -> None:
    """Insert 5 outlets, ~21 workers, today's board, and offer history.

    Two disruption scenarios are deliberately built in:
        * Sarah Lee (Tanjong Pagar) -- the original flagship scenario.
          Local pool resolves it: Marcus fails rest, Ravi fails the 44-hr
          cap, Chloe fails role, Daniel is compliant.
        * Zhi Hao Lee (Woodlands) -- Woodlands' only other Barista-capable
          local option doesn't exist (the rest of the outlet is Cashiers),
          so the search has to expand across outlets. Jurong East is the
          nearest outlet with an eligible Barista.
    """
    today = date.today()
    yesterday = today - timedelta(days=1)

    def iso(day: date, time: str) -> str:
        return f"{day.isoformat()}T{time}:00"

    # The two flagship disruption shifts start a few hours from whenever the
    # demo is actually reset, so "notice_hours" in agent.py's pay-band math
    # is a real, non-trivial number instead of usually flooring to ~0
    # because the clock already passed a hardcoded "14:00". An 8-hr shift
    # starting a few hours from "now" can legitimately run past midnight
    # (normal for a late cafe shift) -- that's fine, HH:MM is just a
    # display string; date stays the day the shift was scheduled for, and
    # agent.py's shift_datetimes() is what correctly handles the wraparound
    # when it needs real datetimes.
    def flagship_shift_window(hours_from_now: float = 4.0, duration_hours: float = 8.0) -> tuple[datetime, str, str]:
        start_dt = datetime.now() + timedelta(hours=hours_from_now)
        end_dt = start_dt + timedelta(hours=duration_hours)
        return start_dt, start_dt.strftime("%H:%M"), end_dt.strftime("%H:%M")

    sarah_start_dt, sarah_start, sarah_end = flagship_shift_window()
    zhihao_start_dt, zhihao_start, zhihao_end = flagship_shift_window(hours_from_now=3.5)

    # Marcus Lim and Nur Aisyah are meant to fail the 11-hr rest check
    # against these two shifts specifically -- their prior shift must end
    # a fixed 9.5 hrs before the relevant flagship start, not at a fixed
    # clock time, or a late-in-the-day demo run would drift them into
    # (incorrectly) looking rested.
    marcus_last_shift_end = (sarah_start_dt - timedelta(hours=9.5)).isoformat(timespec="seconds")
    nur_aisyah_last_shift_end = (zhihao_start_dt - timedelta(hours=9.5)).isoformat(timespec="seconds")

    locations = [
        (1, "Orchard", "123 Orchard Road, #01-01, Singapore 238858", 1.3048, 103.8318),
        (2, "Tampines", "10 Tampines Central 1, #01-05, Singapore 529536", 1.3496, 103.9568),
        (3, "Jurong East", "50 Jurong Gateway Road, #01-12, Singapore 608549", 1.3329, 103.7436),
        (4, "Woodlands", "30 Woodlands Ave 2, #01-08, Singapore 738343", 1.4382, 103.7891),
        (5, "Tanjong Pagar", "1 Wallich Street, #01-20, Singapore 078881", 1.2764, 103.8455),
    ]

    # id, name, role, location_id, home_lat, home_lon, hours_this_week,
    # last_shift_end_time, phone, hourly_rate
    workers = [
        # Tanjong Pagar -- flagship scenario, unchanged outcomes
        (1, "Sarah Lee", "Barista", 5, 1.2800, 103.8300, 32.0, iso(yesterday, "20:00"), "+65 9123 4567", 18.50),
        (2, "Daniel Tan", "Barista", 5, 1.3000, 103.8600, 28.0, iso(yesterday, "21:00"), "+65 9234 5678", 17.50),
        (3, "Marcus Lim", "Barista", 5, 1.2900, 103.8500, 30.0, marcus_last_shift_end, "+65 9345 6789", 19.00),
        (4, "Chloe Ng", "Cashier", 5, 1.2850, 103.8400, 20.0, iso(yesterday, "17:00"), "+65 9456 7890", 16.50),
        (5, "Ravi Kumar", "Barista", 5, 1.2950, 103.8550, 43.5, iso(yesterday, "20:00"), "+65 9567 8901", 18.00),
        # Orchard
        (6, "Aisha Rahman", "Barista", 1, 1.3100, 103.8250, 22.0, iso(yesterday, "19:00"), "+65 8123 1111", 18.00),
        (7, "Wei Jie Tan", "Barista", 1, 1.3000, 103.8400, 35.0, iso(yesterday, "21:00"), "+65 8123 2222", 17.00),
        (8, "Farah Ismail", "Cashier", 1, 1.3080, 103.8350, 18.0, iso(yesterday, "16:00"), "+65 8123 3333", 16.00),
        (9, "Kai Xuan Ong", "Barista", 1, 1.3020, 103.8280, 26.0, iso(yesterday, "20:00"), "+65 8123 4444", 17.50),
        # Tampines
        (10, "Priya Nair", "Barista", 2, 1.3450, 103.9500, 24.0, iso(yesterday, "19:00"), "+65 8234 1111", 17.50),
        (11, "Hafiz Rahman", "Cashier", 2, 1.3520, 103.9600, 20.0, iso(yesterday, "17:00"), "+65 8234 2222", 16.00),
        (12, "Jia Yi Lim", "Barista", 2, 1.3480, 103.9550, 38.0, iso(yesterday, "21:00"), "+65 8234 3333", 17.00),
        (13, "Suresh Kumar", "Barista", 2, 1.3510, 103.9620, 15.0, iso(today - timedelta(days=3), "20:00"), "+65 8234 4444", 18.00),
        # Jurong East -- nearest outlet to Woodlands with Barista coverage
        (14, "Mei Lin Chua", "Barista", 3, 1.3350, 103.7400, 18.0, iso(yesterday, "20:00"), "+65 8345 1111", 17.50),
        (15, "Arjun Singh", "Cashier", 3, 1.3300, 103.7500, 22.0, iso(yesterday, "18:00"), "+65 8345 2222", 16.00),
        (16, "Nur Aisyah", "Barista", 3, 1.3360, 103.7460, 25.0, nur_aisyah_last_shift_end, "+65 8345 3333", 17.00),
        (17, "Benjamin Koh", "Barista", 3, 1.3310, 103.7420, 41.0, iso(yesterday, "20:00"), "+65 8345 4444", 18.50),
        # Woodlands -- deliberately Barista-thin, forces cross-location search
        (18, "Zhi Hao Lee", "Barista", 4, 1.4400, 103.7850, 30.0, iso(yesterday, "21:00"), "+65 8456 1111", 17.50),
        (19, "Grace Tan", "Cashier", 4, 1.4350, 103.7900, 20.0, iso(yesterday, "17:00"), "+65 8456 2222", 16.00),
        (20, "Ethan Ng", "Cashier", 4, 1.4420, 103.7950, 18.0, iso(yesterday, "16:00"), "+65 8456 3333", 16.00),
        (21, "Amirah Yusof", "Cashier", 4, 1.4360, 103.7830, 24.0, iso(yesterday, "18:00"), "+65 8456 4444", 16.50),
    ]

    # id, worker_id, location_id, date, start, end, role, status
    # (original_worker_id is filled in below -- always == worker_id at seed
    # time, before any reassignment happens)
    shifts_raw = [
        (1, 1, 5, today.isoformat(), sarah_start, sarah_end, "Barista", "scheduled"),  # Sarah Lee
        (2, 4, 5, today.isoformat(), "09:00", "17:00", "Cashier", "scheduled"),
        (3, 3, 5, today.isoformat(), "00:00", "04:00", "Barista", "completed"),
        (4, 6, 1, today.isoformat(), "09:00", "17:00", "Barista", "scheduled"),
        (5, 8, 1, today.isoformat(), "08:00", "16:00", "Cashier", "scheduled"),
        (6, 10, 2, today.isoformat(), "07:00", "15:00", "Barista", "scheduled"),
        (7, 11, 2, today.isoformat(), "09:00", "17:00", "Cashier", "scheduled"),
        (8, 14, 3, today.isoformat(), "09:00", "17:00", "Barista", "scheduled"),
        (9, 15, 3, today.isoformat(), "08:00", "16:00", "Cashier", "scheduled"),
        (10, 18, 4, today.isoformat(), zhihao_start, zhihao_end, "Barista", "scheduled"),  # Zhi Hao Lee
        (11, 19, 4, today.isoformat(), "09:00", "17:00", "Cashier", "scheduled"),
    ]
    shifts = [
        (row[0], row[1], row[1], *row[2:]) for row in shifts_raw
    ]

    # Past ad-hoc cover history -- this is what makes reliability claims
    # ("has accepted 3/3 past requests") true rather than invented.
    def past(days_ago: int, hhmm: str) -> str:
        return iso(today - timedelta(days=days_ago), hhmm)

    offers = [
        # id, worker_id, role, hours, base_rate, offered_rate, distance_km, notice_hours, outcome, reasoning, occurred_at
        (1, 2, "Barista", 8.0, 17.50, 21.00, 1.2, 3.5, "accepted", "Short notice, nearby -- accepted.", past(9, "13:00")),
        (2, 2, "Barista", 6.0, 17.50, 20.50, 1.2, 5.0, "accepted", "Accepted for a same-outlet cover.", past(16, "10:00")),
        (3, 2, "Barista", 8.0, 17.50, 22.00, 1.2, 2.0, "accepted", "Very last-minute, accepted anyway.", past(23, "13:30")),
        (4, 14, "Barista", 8.0, 17.50, 20.00, 2.1, 4.0, "accepted", "Accepted a same-outlet cover.", past(6, "09:00")),
        (5, 14, "Barista", 5.0, 17.50, 19.50, 2.1, 6.0, "accepted", "Accepted a short cover shift.", past(20, "14:00")),
        (6, 6, "Barista", 8.0, 18.00, 21.50, 3.0, 3.0, "declined", "Declined -- personal conflict.", past(11, "13:00")),
        (7, 6, "Barista", 6.0, 18.00, 20.50, 3.0, 5.5, "accepted", "Accepted a weekend cover.", past(18, "09:30")),
        (8, 7, "Barista", 8.0, 17.00, 21.00, 1.8, 3.0, "declined", "Declined -- too short notice.", past(14, "12:00")),
        (9, 10, "Barista", 8.0, 17.50, 20.50, 2.5, 4.5, "accepted", "Accepted a same-outlet cover.", past(8, "10:00")),
    ]

    settings = [
        ("max_premium_multiplier", str(DEFAULT_MAX_PREMIUM_MULTIPLIER)),
    ]

    with get_connection() as connection:
        connection.executemany(
            "INSERT INTO locations (id, name, address, lat, lon) VALUES (?, ?, ?, ?, ?)",
            locations,
        )
        connection.executemany(
            """
            INSERT INTO workers
                (id, name, role, location_id, home_lat, home_lon,
                 hours_worked_this_week, last_shift_end_time, phone, hourly_rate)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            workers,
        )
        connection.executemany(
            """
            INSERT INTO shifts
                (id, worker_id, original_worker_id, location_id, date, start_time, end_time, role, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            shifts,
        )
        connection.executemany(
            """
            INSERT INTO ad_hoc_offers
                (id, worker_id, shift_role, shift_hours, base_rate, offered_rate,
                 distance_km, notice_hours, outcome, reasoning, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            offers,
        )
        connection.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            settings,
        )


def reset_database() -> None:
    """Recreate the database and load the seed dataset. Idempotent --
    safe to call from the UI's "Reset demo" button as many times as needed.
    """
    create_schema()
    seed_data()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_locations() -> list[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute("SELECT * FROM locations ORDER BY name").fetchall()


def get_location(location_id: int) -> sqlite3.Row | None:
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM locations WHERE id = ?", (location_id,)
        ).fetchone()


def get_worker_by_name(name: str) -> sqlite3.Row | None:
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM workers WHERE name = ?", (name,)
        ).fetchone()


def get_candidates_by_location(location_id: int, exclude_name: str | None = None) -> list[sqlite3.Row]:
    """Workers based at one outlet, least-worked first. Any role -- role
    eligibility is rules.py's job, not a pre-filter here (see module note
    in agent.py about wanting role-mismatch rejections to actually show up)."""
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT * FROM workers
            WHERE location_id = ? AND name != ?
            ORDER BY hours_worked_this_week ASC, name ASC
            """,
            (location_id, exclude_name or ""),
        ).fetchall()


def get_candidate_pool(exclude_name: str | None = None) -> list[sqlite3.Row]:
    """Every worker, any outlet, least-worked first."""
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT * FROM workers
            WHERE name != ?
            ORDER BY hours_worked_this_week ASC, name ASC
            """,
            (exclude_name or "",),
        ).fetchall()


def get_today_schedule() -> list[sqlite3.Row]:
    """Today's shifts, every outlet, with worker + location names joined in."""
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT
                s.id, s.worker_id, w.name AS worker_name,
                s.original_worker_id, ow.name AS original_worker_name,
                s.location_id, l.name AS location_name,
                s.date, s.start_time, s.end_time, s.role, s.status
            FROM shifts AS s
            JOIN workers AS w ON w.id = s.worker_id
            JOIN workers AS ow ON ow.id = s.original_worker_id
            JOIN locations AS l ON l.id = s.location_id
            WHERE s.date = ?
            ORDER BY l.name ASC, s.start_time ASC, s.id ASC
            """,
            (date.today().isoformat(),),
        ).fetchall()


def get_worker_reliability(worker_id: int) -> tuple[int, int]:
    """Return (accepted_count, total_resolved_count) from real history.
    (0, 0) means no track record -- callers must treat that as "unknown",
    never as 0%.
    """
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT
                SUM(CASE WHEN outcome = 'accepted' THEN 1 ELSE 0 END) AS accepted,
                COUNT(*) AS total
            FROM ad_hoc_offers WHERE worker_id = ?
            """,
            (worker_id,),
        ).fetchone()
    return (row["accepted"] or 0, row["total"] or 0)


def get_worker_offer_history(worker_id: int) -> list[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM ad_hoc_offers WHERE worker_id = ? ORDER BY occurred_at DESC",
            (worker_id,),
        ).fetchall()


def get_all_offers() -> list[sqlite3.Row]:
    """Full ad-hoc offer history joined with worker + location, for the
    cost dashboard."""
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT o.*, w.name AS worker_name, w.location_id, l.name AS location_name
            FROM ad_hoc_offers AS o
            JOIN workers AS w ON w.id = o.worker_id
            JOIN locations AS l ON l.id = w.location_id
            ORDER BY o.occurred_at DESC
            """
        ).fetchall()


def get_setting(key: str, default: str) -> str:
    with get_connection() as connection:
        row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_max_premium_multiplier() -> float:
    return float(get_setting("max_premium_multiplier", str(DEFAULT_MAX_PREMIUM_MULTIPLIER)))


# ---------------------------------------------------------------------------
# Writes -- the only places a schedule/policy mutation is persisted
# ---------------------------------------------------------------------------


def set_shift_status(shift_id: int, status: str) -> bool:
    with get_connection() as connection:
        cursor = connection.execute(
            "UPDATE shifts SET status = ? WHERE id = ?", (status, shift_id)
        )
        return cursor.rowcount == 1


def mark_shift_sick(shift_id: int) -> bool:
    return set_shift_status(shift_id, "sick")


def commit_shift_coverage(shift_id: int, new_worker_id: int, shift_hours: float) -> bool:
    """Reassign a shift to the approved replacement and book their hours.
    Only ever called after human approval + a simulated worker "yes"."""
    with get_connection() as connection:
        cursor = connection.execute(
            "UPDATE shifts SET worker_id = ?, status = 'covered' WHERE id = ?",
            (new_worker_id, shift_id),
        )
        if cursor.rowcount != 1:
            return False
        connection.execute(
            "UPDATE workers SET hours_worked_this_week = hours_worked_this_week + ? WHERE id = ?",
            (shift_hours, new_worker_id),
        )
        return True


def cancel_coverage(shift_id: int) -> bool:
    """Undo a covered assignment: give the shift back to the originally
    absent worker's slot as 'sick' (still unstaffed) and refund the hours
    booked onto the covering worker, so the state is consistent for a
    fresh search. Only valid on a shift currently in 'covered' status."""
    with get_connection() as connection:
        row = connection.execute(
            "SELECT worker_id, original_worker_id, start_time, end_time, status "
            "FROM shifts WHERE id = ?",
            (shift_id,),
        ).fetchone()
        if row is None or row["status"] != "covered":
            return False
        covering_worker_id = row["worker_id"]
        start_h, start_m = (int(part) for part in row["start_time"].split(":"))
        end_h, end_m = (int(part) for part in row["end_time"].split(":"))
        end_minutes = end_h * 60 + end_m
        start_minutes = start_h * 60 + start_m
        if end_minutes <= start_minutes:
            end_minutes += 24 * 60  # overnight shift
        shift_hours = (end_minutes - start_minutes) / 60

        connection.execute(
            "UPDATE shifts SET worker_id = original_worker_id, status = 'sick' WHERE id = ?",
            (shift_id,),
        )
        connection.execute(
            "UPDATE workers SET hours_worked_this_week = MAX(0, hours_worked_this_week - ?) WHERE id = ?",
            (shift_hours, covering_worker_id),
        )
        return True


def record_adhoc_offer(
    worker_id: int,
    shift_role: str,
    shift_hours: float,
    base_rate: float,
    offered_rate: float,
    distance_km: float,
    notice_hours: float,
    outcome: str,
    reasoning: str,
) -> None:
    """Append one resolved (accepted/declined) offer to the audit trail.
    This IS the reliability track record -- every future "has accepted
    N/M past requests" claim traces back to a row written here."""
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO ad_hoc_offers
                (worker_id, shift_role, shift_hours, base_rate, offered_rate,
                 distance_km, notice_hours, outcome, reasoning, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                worker_id, shift_role, shift_hours, base_rate, offered_rate,
                distance_km, notice_hours, outcome, reasoning,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def set_max_premium_multiplier(value: float) -> None:
    with get_connection() as connection:
        connection.execute(
            "INSERT INTO settings (key, value) VALUES ('max_premium_multiplier', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(value),),
        )


def print_verification_counts() -> None:
    with get_connection() as connection:
        counts = {
            "Locations": connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0],
            "Workers": connection.execute("SELECT COUNT(*) FROM workers").fetchone()[0],
            "Shifts": connection.execute("SELECT COUNT(*) FROM shifts").fetchone()[0],
            "Ad-hoc offer history rows": connection.execute("SELECT COUNT(*) FROM ad_hoc_offers").fetchone()[0],
        }
    print(f"Database initialized: {DB_PATH}")
    for label, count in counts.items():
        print(f"{label}: {count}")


if __name__ == "__main__":
    reset_database()
    print_verification_counts()
