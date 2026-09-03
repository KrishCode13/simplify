"""Local SQLite storage for ShiftPilot.

Run this module directly to reset ``shiftpilot.db`` and load demonstration data.
Import it to use the helper query and update functions.
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
                    CHECK (status IN ('scheduled', 'sick', 'completed')),
                FOREIGN KEY (worker_id) REFERENCES workers(id)
            );

            CREATE INDEX idx_shifts_date ON shifts(date);
            CREATE INDEX idx_shifts_worker_id ON shifts(worker_id);
            """
        )


def seed_data() -> None:
    """Insert a realistic worker roster and shifts relative to today's date."""
    today = date.today()
    yesterday = today - timedelta(days=1)

    workers = [
        (1, "Sarah Lee", "Barista", 32.0, "+65 9123 4567", 18.50),
        (2, "Daniel Tan", "Barista", 28.0, "+65 9234 5678", 17.50),
        (3, "Marcus Lim", "Barista", 41.0, "+65 9345 6789", 19.00),
        (4, "Chloe Ng", "Cashier", 20.0, "+65 9456 7890", 16.50),
        (5, "Ravi Kumar", "Barista", 43.5, "+65 9567 8901", 18.00),
    ]

    shifts = [
        # Sarah's scheduled shift is the one that may need reassignment.
        (1, 1, today.isoformat(), "14:00", "22:00", "Barista", "scheduled"),
        # Daniel completed a shift yesterday at 21:00.
        (2, 2, yesterday.isoformat(), "13:00", "21:00", "Barista", "completed"),
        # Marcus finished an overnight shift at 04:00 today (insufficient rest).
        (3, 3, today.isoformat(), "00:00", "04:00", "Barista", "completed"),
        (4, 4, today.isoformat(), "09:00", "17:00", "Cashier", "scheduled"),
        (5, 2, today.isoformat(), "08:00", "12:00", "Barista", "scheduled"),
    ]

    with get_connection() as connection:
        connection.executemany(
            """
            INSERT INTO workers
                (id, name, role, hours_worked_this_week, phone, hourly_rate)
            VALUES (?, ?, ?, ?, ?, ?)
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


def get_worker_by_name(name: str) -> sqlite3.Row | None:
    """Return the worker whose name matches exactly, or ``None``."""
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM workers WHERE name = ?",
            (name,),
        ).fetchone()


def get_candidates_for_role(role: str) -> list[sqlite3.Row]:
    """Return workers qualified for a role, least-worked first."""
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT *
            FROM workers
            WHERE role = ?
            ORDER BY hours_worked_this_week ASC, name ASC
            """,
            (role,),
        ).fetchall()


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


def update_shift_worker(shift_id: int, new_worker_id: int, new_status: str) -> bool:
    """Reassign a shift and update its status; return whether it existed."""
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE shifts
            SET worker_id = ?, status = ?
            WHERE id = ?
            """,
            (new_worker_id, new_status, shift_id),
        )
        return cursor.rowcount == 1


def reset_database() -> None:
    """Recreate the database and load the seed dataset."""
    create_schema()
    seed_data()


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
