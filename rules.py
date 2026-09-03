"""Deterministic compliance checks and candidate ranking for ShiftPilot.

This module intentionally contains no LLM or user-interface logic. Datetimes may
be supplied as ``datetime`` objects or ISO-8601 strings. When timezone-aware
datetimes are used, both shift timestamps must be timezone-aware.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


MAX_REGULAR_WEEKLY_HOURS = Decimal("44.0")
MINIMUM_REST_HOURS = Decimal("11.0")

Worker = Mapping[str, Any]
Shift = Mapping[str, Any]


def _required(record: Mapping[str, Any], key: str, record_name: str) -> Any:
    """Return a required value and fail clearly when it is absent."""
    if key not in record:
        raise ValueError(f"{record_name} is missing required field: {key}")
    return record[key]


def _non_negative_decimal(value: Any, field_name: str) -> Decimal:
    """Convert a numeric input without inheriting binary floating-point error."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a non-negative number") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{field_name} must be a non-negative number")
    return number


def _datetime(value: Any, field_name: str) -> datetime:
    """Parse a datetime object or an ISO-8601 datetime string."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            return datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a valid ISO-8601 datetime") from exc
    raise ValueError(f"{field_name} must be a datetime or ISO-8601 string")


def _validate_timezone_compatibility(first: datetime, second: datetime) -> None:
    first_is_aware = first.utcoffset() is not None
    second_is_aware = second.utcoffset() is not None
    if first_is_aware != second_is_aware:
        raise ValueError("shift timestamps must use compatible timezone information")


def check_worker_compliance(worker: Worker, target_shift: Shift) -> tuple[bool, str]:
    """Check one worker against role, weekly-hours, and rest constraints.

    Checks are evaluated in the order required by the product specification.
    Invalid or missing data raises ``ValueError`` instead of producing a possibly
    unsafe eligibility decision.
    """
    worker_role = _required(worker, "role", "worker")
    shift_role = _required(target_shift, "role", "target_shift")
    if worker_role != shift_role:
        return False, "Role mismatch"

    hours_worked = _non_negative_decimal(
        _required(worker, "hours_worked_this_week", "worker"),
        "hours_worked_this_week",
    )
    duration = _non_negative_decimal(
        _required(target_shift, "duration_hours", "target_shift"),
        "duration_hours",
    )
    if hours_worked + duration > MAX_REGULAR_WEEKLY_HOURS:
        return False, "Exceeds 44-hr weekly cap"

    last_shift_end = _datetime(
        _required(worker, "last_shift_end_time", "worker"),
        "last_shift_end_time",
    )
    target_start = _datetime(
        _required(target_shift, "start_time", "target_shift"),
        "start_time",
    )
    _validate_timezone_compatibility(last_shift_end, target_start)

    rest_seconds = Decimal(str((target_start - last_shift_end).total_seconds()))
    rest_hours = rest_seconds / Decimal("3600")
    if rest_hours < MINIMUM_REST_HOURS:
        return False, "Violates 11-hr mandatory rest period"

    return True, "Compliant"


def rank_candidates(eligible_workers: Sequence[Worker]) -> list[Worker]:
    """Return candidates ordered by weekly hours, then name and ID.

    The secondary keys make equal-hours results reproducible. The input sequence
    and its worker dictionaries are not mutated.
    """

    def ranking_key(worker: Worker) -> tuple[Decimal, str, str]:
        hours = _non_negative_decimal(
            _required(worker, "hours_worked_this_week", "worker"),
            "hours_worked_this_week",
        )
        name = str(_required(worker, "name", "worker")).casefold()
        worker_id = str(_required(worker, "id", "worker"))
        return hours, name, worker_id

    return sorted(eligible_workers, key=ranking_key)


if __name__ == "__main__":
    target_shift = {
        "role": "Barista",
        "start_time": "2026-09-03T14:00:00+08:00",
        "end_time": "2026-09-03T22:00:00+08:00",
        "duration_hours": 8.0,
    }

    workers = [
        {
            "id": "W001",
            "name": "Marcus",
            "role": "Barista",
            "hours_worked_this_week": 24.0,
            "last_shift_end_time": "2026-09-03T06:00:00+08:00",
        },
        {
            "id": "W002",
            "name": "Ravi",
            "role": "Barista",
            "hours_worked_this_week": 40.0,
            "last_shift_end_time": "2026-09-02T22:00:00+08:00",
        },
        {
            "id": "W003",
            "name": "Daniel",
            "role": "Barista",
            "hours_worked_this_week": 16.0,
            "last_shift_end_time": "2026-09-02T22:00:00+08:00",
        },
        {
            "id": "W004",
            "name": "Aisha",
            "role": "Barista",
            "hours_worked_this_week": 20.0,
            "last_shift_end_time": "2026-09-02T20:00:00+08:00",
        },
    ]

    results = {
        worker["name"]: check_worker_compliance(worker, target_shift)
        for worker in workers
    }

    assert results["Marcus"] == (
        False,
        "Violates 11-hr mandatory rest period",
    )
    assert results["Ravi"] == (False, "Exceeds 44-hr weekly cap")
    assert results["Daniel"] == (True, "Compliant")

    eligible = [worker for worker in workers if results[worker["name"]][0]]
    ranked = rank_candidates(eligible)
    assert ranked[0]["name"] == "Daniel"

    for worker in workers:
        compliant, reason = results[worker["name"]]
        print(f"{worker['name']}: {'APPROVED' if compliant else 'REJECTED'} - {reason}")
    print("Candidate ranking:", " > ".join(worker["name"] for worker in ranked))
