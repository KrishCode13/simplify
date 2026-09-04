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
    """Return candidates ordered by weekly hours (primary -- minimizes
    overtime risk), then distance from the shift (secondary -- prefers a
    shorter commute among similarly-loaded candidates), then name and ID
    for reproducibility.

    ``distance_km`` is optional per-worker input (attached by the caller
    once it knows which shift/outlet is being filled); candidates without
    it sort as if distance were 0, so this stays backward compatible with
    callers that don't compute it. The input sequence and its worker
    dictionaries are not mutated.
    """

    def ranking_key(worker: Worker) -> tuple[Decimal, Decimal, str, str]:
        hours = _non_negative_decimal(
            _required(worker, "hours_worked_this_week", "worker"),
            "hours_worked_this_week",
        )
        distance = _non_negative_decimal(worker.get("distance_km", 0), "distance_km")
        name = str(_required(worker, "name", "worker")).casefold()
        worker_id = str(_required(worker, "id", "worker"))
        return hours, distance, name, worker_id

    return sorted(eligible_workers, key=ranking_key)


# ---------------------------------------------------------------------------
# Distance and pay-premium math.
#
# Both are plain deterministic arithmetic -- the LLM never computes a
# distance or invents a rate out of thin air. draft_message_node in
# agent.py asks the LLM to pick a specific number and write a
# justification, but the *band* it must pick within, and the distance
# figure it's given to reason about, both come from here.
# ---------------------------------------------------------------------------

EARTH_RADIUS_KM = Decimal("6371.0")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometres."""
    from math import asin, cos, radians, sin, sqrt

    lat1_r, lon1_r, lat2_r, lon2_r = map(radians, (lat1, lon1, lat2, lon2))
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = sin(dlat / 2) ** 2 + cos(lat1_r) * cos(lat2_r) * sin(dlon / 2) ** 2
    return float(EARTH_RADIUS_KM) * 2 * asin(sqrt(a))


def compute_pay_band(
    base_rate: float,
    notice_hours: float,
    distance_km: float,
    max_multiplier: float = 1.5,
) -> dict[str, float]:
    """Deterministic ad-hoc pay band for a last-minute cover request.

    * Floor is always the worker's normal rate -- never offer less than
      base pay for the inconvenience of a last-minute ask.
    * Ceiling scales with urgency (less notice -> higher ceiling, up to
      +30% inside a 12-hour window) and with commute distance (further
      -> higher ceiling, up to +20% at 20km+), capped at the manager-set
      ``max_multiplier`` policy guardrail.

    The LLM picks a specific number inside [min_rate, max_rate] and
    justifies it; it never sets either boundary.
    """
    notice_hours = max(0.0, notice_hours)
    distance_km = max(0.0, distance_km)

    urgency_boost = max(0.0, min(1.0, (12.0 - notice_hours) / 12.0)) * 0.30
    distance_boost = max(0.0, min(1.0, distance_km / 20.0)) * 0.20
    ceiling_multiplier = min(1.0 + urgency_boost + distance_boost, max(1.0, max_multiplier))

    return {
        "min_rate": round(base_rate, 2),
        "max_rate": round(base_rate * ceiling_multiplier, 2),
        "ceiling_multiplier": round(ceiling_multiplier, 3),
    }


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
