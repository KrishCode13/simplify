"""Local SQLite storage for ShiftPilot.

Run this module directly to reset ``shiftpilot.db`` and load the demo
dataset. Import it to use the helper query and update functions.

Schema notes:
    * ``workers.hours_worked_this_week`` and ``workers.last_shift_end_time``
      are denormalized running totals rather than derived from ``shifts``.
      That keeps the deterministic rule checks in ``rules.py`` a single
      cheap read per worker, and keeps the seed data trivially reproducible
      for the demo -- no need to reconstruct "hours worked" from a shift
      history that doesn't exist yet for a brand-new prototype.
    * Every row returned by the ``get_*`` functions is a ``sqlite3.Row``,
      which supports both ``row["col"]`` and ``dict(row)``. ``agent.py``
      converts these to plain dicts before handing them to ``rules.py``.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path


DB_PATH = Path(__file__).with_name("shiftpilot.db")


def get_connection() -> sqlite3.Connection:
    """Return a configured connection to the ShiftPilot database."""
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def create_schema() -> None:
    """Drop existing ShiftPilot tables and create a clean schema."""
    with get_connection() as connection:
        connection.executescript(
            """
            DROP TABLE IF EXISTS shifts;
            DROP TABLE IF EXISTS workers;

            CREATE TABLE workers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL CHECK (role IN ('Barista', 'Cashier')),
                hours_worked_this_week REAL NOT NULL CHECK (hours_worked_this_week >= 0),
                last_shift_end_time TEXT NOT NULL,
                phone TEXT NOT NULL,
                hourly_rate REAL NOT NULL CHECK (hourly_rate >= 0)
            );

            CREATE TABLE shifts (
                id INTEGER PRIMARY KEY,
                worker_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('Barista', 'Cashier')),
                status TEXT NOT NULL
                    CHECK (status IN ('scheduled', 'sick', 'completed', 'covered')),
                FOREIGN KEY (worker_id) REFERENCES workers(id)
            );

            CREATE INDEX idx_shifts_date ON shifts(date);
            CREATE INDEX idx_shifts_worker_id ON shifts(worker_id);
            """
        )


def seed_data() -> None:
    """Insert the worker roster + today's schedule for the judge demo.

    Numbers are chosen deliberately so the disruption scenario resolves
    the same way every time:
        * Marcus Lim  -> fails the 11-hr rest rule (off an early-morning
          shift that ended at 04:00 today).
        * Ravi Kumar  -> fails the 44-hr weekly cap (43.5 + 8 = 51.5).
        * Chloe Ng    -> fails on role (Cashier, shift needs a Barista).
        * Daniel Tan  -> compliant: 28 + 8 = 36 hrs, 17 hrs of rest.
    """
    today = date.today()
    yesterday = today - timedelta(days=1)

    def iso(day: date, time: str) -> str:
        return f"{day.isoformat()}T{time}:00"

    workers = [
        (1, "Sarah Lee", "Barista", 32.0, iso(yesterday, "20:00"), "+65 9123 4567", 18.50),
        (2, "Daniel Tan", "Barista", 28.0, iso(yesterday, "21:00"), "+65 9234 5678", 17.50),
        (3, "Marcus Lim", "Barista", 30.0, iso(today, "04:00"), "+65 9345 6789", 19.00),
        (4, "Chloe Ng", "Cashier", 20.0, iso(yesterday, "17:00"), "+65 9456 7890", 16.50),
        (5, "Ravi Kumar", "Barista", 43.5, iso(yesterday, "20:00"), "+65 9567 8901", 18.00),
    ]

    shifts = [
        # The shift that goes up for grabs when Sarah calls in sick.
        (1, 1, today.isoformat(), "14:00", "22:00", "Barista", "scheduled"),
        # Rest of today's board, for a realistic-looking roster on load.
        (2, 4, today.isoformat(), "09:00", "17:00", "Cashier", "scheduled"),
        (3, 3, today.isoformat(), "00:00", "04:00", "Barista", "completed"),
    ]

    with get_connection() as connection:
        connection.executemany(
            """
            INSERT INTO workers
                (id, name, role, hours_worked_this_week, last_shift_end_time, phone, hourly_rate)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            workers,
        )
        connection.executemany(
            """
            INSERT INTO shifts
                (id, worker_id, date, start_time, end_time, role, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            shifts,
        )


def reset_database() -> None:
    """Recreate the database and load the seed dataset. Idempotent --
    safe to call from the UI's "Reset Demo" button as many times as needed.
    """
    create_schema()
    seed_data()


def get_worker_by_name(name: str) -> sqlite3.Row | None:
    """Return the worker whose name matches exactly, or ``None``."""
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM workers WHERE name = ?",
            (name,),
        ).fetchone()


def get_candidate_pool(exclude_name: str | None = None) -> list[sqlite3.Row]:
    """Return every worker (any role), least-worked first.

    Deliberately NOT filtered by role -- ``rules.check_worker_compliance``
    is the single source of truth for role eligibility, and the audit
    trail is more convincing when it shows *why* a wrong-role candidate
    (e.g. a cashier) was rejected rather than silently omitting them.
    """
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM workers
            WHERE name != ?
            ORDER BY hours_worked_this_week ASC, name ASC
            """,
            (exclude_name or "",),
        ).fetchall()
    return rows


def get_today_schedule() -> list[sqlite3.Row]:
    """Return today's shifts with worker details in chronological order."""
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT
                s.id,
                s.worker_id,
                w.name AS worker_name,
                s.date,
                s.start_time,
                s.end_time,
                s.role,
                s.status
            FROM shifts AS s
            JOIN workers AS w ON w.id = s.worker_id
            WHERE s.date = ?
            ORDER BY s.start_time ASC, s.id ASC
            """,
            (date.today().isoformat(),),
        ).fetchall()


def set_shift_status(shift_id: int, status: str) -> bool:
    """Set a shift's status directly (e.g. reverting a cancelled disruption)."""
    with get_connection() as connection:
        cursor = connection.execute(
            "UPDATE shifts SET status = ? WHERE id = ?",
            (status, shift_id),
        )
        return cursor.rowcount == 1


def mark_shift_sick(shift_id: int) -> bool:
    """Flag a shift as an unstaffed disruption (worker called in sick)."""
    return set_shift_status(shift_id, "sick")


def commit_shift_coverage(shift_id: int, new_worker_id: int, shift_hours: float) -> bool:
    """Reassign a shift to the approved replacement and book their hours.

    This is the ONLY place a schedule mutation is persisted, and it is
    only ever called after the human approval gate + simulated worker
    acceptance -- never by the agent graph itself.
    """
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


def print_verification_counts() -> None:
    """Print concise counts to verify that initialization succeeded."""
    with get_connection() as connection:
        worker_count = connection.execute("SELECT COUNT(*) FROM workers").fetchone()[0]
        shift_count = connection.execute("SELECT COUNT(*) FROM shifts").fetchone()[0]
        today_count = connection.execute(
            "SELECT COUNT(*) FROM shifts WHERE date = ?",
            (date.today().isoformat(),),
        ).fetchone()[0]

    print(f"Database initialized: {DB_PATH}")
    print(f"Workers: {worker_count}")
    print(f"Shifts: {shift_count}")
    print(f"Today's shifts: {today_count}")


if __name__ == "__main__":
    reset_database()
    print_verification_counts()
