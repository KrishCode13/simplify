"""ShiftPilot agent state machine.

LangGraph orchestration for the "unexpected disruption -> replacement staffing"
workflow described in the ShiftPilot hackathon spec.

Boundary this file respects:
    * Deterministic legal/scheduling logic (MOM hour caps, rest periods,
      candidate ranking, distance, and the pay-premium band) lives in
      ``rules.py`` and is imported, never re-implemented here.
    * Data access (workers, shifts, outlets, offer history) lives in
      ``db.py`` and is imported, never re-implemented here.
    * The LLM is used ONLY for natural-language judgment calls that are
      legitimately subjective: which eligible candidate to lead with (and
      why, in plain English), and exactly where to land within the
      deterministic pay band. It never computes hours, rest gaps,
      distance, or the band's boundaries -- that's already done by the
      time draft_message_node runs, and its output is re-validated
      (rate clamped into the band) rather than trusted blindly.

Graph shape:
    START -> investigate_disruption -> evaluate_candidates -> draft_message
          -> human_approval_gate -> END

The graph deliberately ends at the approval gate. No shift is ever
committed to the database inside this file -- that only happens after a
human clicks "Approve & Dispatch" AND the simulated worker accepts, both
handled by ``app.py`` via ``db.commit_shift_coverage``.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import TypedDict

from dotenv import load_dotenv
from langgraph.graph import StateGraph, START, END

import db
from rules import check_worker_compliance, compute_pay_band, haversine_km, rank_candidates

load_dotenv()  # so `python3 agent.py` picks up .env same as `streamlit run app.py`


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class ShiftPilotState(TypedDict):
    disruption_type: str  # e.g. "SICK_LEAVE"
    absent_worker: str
    shift_details: dict  # keys: role, date, start_time, end_time, duration, location_id, location_name, lat, lon
    search_tier: str  # "local" | "cross_location" | ""
    eligible_candidates: list[dict]
    candidate_audit: list[dict]  # every candidate considered, incl. rejects
    selected_candidate: dict
    pay_band: dict  # {min_rate, max_rate, ceiling_multiplier}
    notice_hours: float
    offered_rate: float
    justification: str
    reasoning_log: list[str]
    drafted_message: str
    status: str  # e.g. "AWAITING_MANAGER_APPROVAL", "RESOLVED", "ESCALATED"
    iterations: int  # bounded-loop guard, see draft_message_node
    max_iterations: int


def new_state(
    *,
    disruption_type: str,
    absent_worker: str,
    shift_details: dict,
    max_iterations: int = 3,
) -> ShiftPilotState:
    """Build a fresh, correctly-defaulted state for a single disruption run."""
    return {
        "disruption_type": disruption_type,
        "absent_worker": absent_worker,
        "shift_details": shift_details,
        "search_tier": "",
        "eligible_candidates": [],
        "candidate_audit": [],
        "selected_candidate": {},
        "pay_band": {},
        "notice_hours": 0.0,
        "offered_rate": 0.0,
        "justification": "",
        "reasoning_log": [],
        "drafted_message": "",
        "status": "IN_PROGRESS",
        "iterations": 0,
        "max_iterations": max_iterations,
    }


def shift_datetimes(date_str: str, start_hhmm: str, end_hhmm: str) -> tuple[datetime, datetime, float]:
    """Combine a shift's date + HH:MM start/end into real datetimes,
    handling the overnight case (an end time earlier than the start time
    means the shift runs past midnight -- normal for a late cafe shift,
    not an error). Returns (start_dt, end_dt, duration_hours).

    The single place this arithmetic happens -- callers (this module's
    __main__ test and app.py) both use it instead of each re-deriving
    duration from HH:MM strings themselves.
    """
    start_dt = datetime.fromisoformat(f"{date_str}T{start_hhmm}:00")
    end_dt = datetime.fromisoformat(f"{date_str}T{end_hhmm}:00")
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    duration_hours = round((end_dt - start_dt).total_seconds() / 3600, 2)
    return start_dt, end_dt, duration_hours


# ---------------------------------------------------------------------------
# LLM helper
# ---------------------------------------------------------------------------


def _get_llm():
    providers = [
        ("ANTHROPIC_API_KEY", "langchain_anthropic", "ChatAnthropic", "claude-sonnet-5"),
        ("OPENAI_API_KEY", "langchain_openai", "ChatOpenAI", "gpt-4o-mini"),
        ("GOOGLE_API_KEY", "langchain_google_genai", "ChatGoogleGenerativeAI", "gemini-1.5-flash"),
        ("GROQ_API_KEY", "langchain_groq", "ChatGroq", "llama-3.1-70b-versatile"),
    ]
    for env_key, module_name, class_name, model_name in providers:
        if not os.environ.get(env_key):
            continue
        try:
            module = __import__(module_name, fromlist=[class_name])
            chat_cls = getattr(module, class_name)
            return chat_cls(model=model_name, temperature=0.4)
        except Exception:
            continue
    return None


def _reliability_text(accepted: int, total: int) -> str:
    if total == 0:
        return "no ad-hoc cover track record yet"
    return f"has accepted {accepted}/{total} past last-minute cover requests"


def _fallback_reasoning(candidate: dict, shift: dict, notice_hours: float, offered_rate: float) -> tuple[str, str]:
    """Deterministic justification + message, used when no LLM provider is
    configured or the LLM response can't be parsed. Still grounded in the
    same real numbers the LLM would have seen -- this isn't a lesser demo,
    just a non-generative one."""
    accepted, total = db.get_worker_reliability(candidate["id"])
    reliability = _reliability_text(accepted, total)
    multiplier = offered_rate / candidate["hourly_rate"] if candidate["hourly_rate"] else 1.0

    justification = (
        f"{candidate['name']} is {candidate['distance_km']:.1f} km from {shift['location_name']} "
        f"and {reliability}. With {notice_hours:.1f} hrs notice, offering "
        f"${offered_rate:.2f}/hr ({multiplier:.2f}x base) -- within policy."
    )
    message = (
        f"Hi {candidate['name']}! This is ShiftPilot on behalf of {shift['location_name']}. "
        f"{shift.get('absent_worker', 'A colleague')} called in sick and we have an open "
        f"{shift['role']} shift today from {shift['start_time'][-8:-3]} to {shift['end_time'][-8:-3]} "
        f"({shift.get('duration', '?')} hrs). Given the short notice, we can offer "
        f"${offered_rate:.2f}/hr (your usual rate is ${candidate['hourly_rate']:.2f}/hr). "
        f"Would you be able to cover it? No worries at all if you can't -- just let us know "
        f"either way. Reply YES or NO. Thank you! 🙏"
    )
    return justification, message


_RATE_RE = re.compile(r"OFFERED_RATE:\s*\$?([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_JUSTIFICATION_RE = re.compile(r"JUSTIFICATION:\s*(.+?)(?=\nMESSAGE:|\Z)", re.IGNORECASE | re.DOTALL)
_MESSAGE_RE = re.compile(r"MESSAGE:\s*(.+)\Z", re.IGNORECASE | re.DOTALL)


def _parse_llm_response(text: str, pay_band: dict) -> tuple[float, str, str] | None:
    """Parse the LLM's plain-text response into (rate, justification, message).
    Returns None if any field is missing -- caller falls back to the
    deterministic template rather than shipping a half-parsed result.
    """
    rate_match = _RATE_RE.search(text)
    justification_match = _JUSTIFICATION_RE.search(text)
    message_match = _MESSAGE_RE.search(text)
    if not (rate_match and justification_match and message_match):
        return None
    try:
        rate = float(rate_match.group(1))
    except ValueError:
        return None
    # Never trust the LLM's number blindly -- clamp into the deterministic band.
    rate = max(pay_band["min_rate"], min(pay_band["max_rate"], rate))
    return rate, justification_match.group(1).strip(), message_match.group(1).strip()


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def investigate_disruption_node(state: ShiftPilotState) -> ShiftPilotState:
    """[Inspect] Log the disruption and the shift it affects."""
    shift = state["shift_details"]
    log = list(state.get("reasoning_log", []))
    log.append(
        f"[Inspect] {state['disruption_type']} reported for {state['absent_worker']}. "
        f"Affected shift: {shift.get('role')} at {shift.get('location_name')} on {shift.get('date')} "
        f"{shift.get('start_time')}-{shift.get('end_time')} "
        f"({shift.get('duration')} hrs)."
    )
    return {**state, "reasoning_log": log}


def _check_pool(
    pool: list[dict], target_shift: dict, shift_lat: float, shift_lon: float
) -> tuple[list[dict], list[dict]]:
    """Run every worker in a pool through compliance + distance. Returns
    (eligible, audit_rows). Pure orchestration -- the actual legal check
    is check_worker_compliance(); the actual distance math is
    haversine_km(); this just calls both and records the outcome.
    """
    eligible: list[dict] = []
    audit: list[dict] = []
    for worker in pool:
        worker = dict(worker)
        worker["distance_km"] = haversine_km(shift_lat, shift_lon, worker["home_lat"], worker["home_lon"])
        compliant, reason = check_worker_compliance(worker, target_shift)
        verdict = "APPROVED" if compliant else "REJECTED"
        audit.append(
            {
                "name": worker["name"],
                "role": worker["role"],
                "hours_worked_this_week": worker["hours_worked_this_week"],
                "distance_km": worker["distance_km"],
                "verdict": verdict,
                "reason": reason,
            }
        )
        if compliant:
            eligible.append(worker)
    return eligible, audit


def evaluate_candidates_node(state: ShiftPilotState) -> ShiftPilotState:
    """[Filter] + [Rule Check] Search the disrupted outlet's own staff
    first; only if nobody local is compliant does the search expand to
    every other outlet. Every candidate considered -- local or
    cross-location -- runs through the same deterministic compliance
    engine in rules.py. Distance is computed here (pure math, not a
    legal decision) so ranking and the pay band both have real numbers
    to work with.
    """
    shift = state["shift_details"]
    target_shift = {
        "role": shift["role"],
        "start_time": shift["start_time"],
        "end_time": shift["end_time"],
        "duration_hours": shift["duration"],
    }

    log = list(state.get("reasoning_log", []))
    location_id = shift["location_id"]
    shift_lat, shift_lon = shift["lat"], shift["lon"]

    local_pool = [dict(row) for row in db.get_candidates_by_location(location_id, exclude_name=state["absent_worker"])]
    log.append(
        f"[Filter] Checking {len(local_pool)} workers based at {shift['location_name']} against "
        f"MOM rules for the {shift['role']} shift (max 44 hrs/week, 11-hr min rest)."
    )
    eligible, audit = _check_pool(local_pool, target_shift, shift_lat, shift_lon)
    for row in audit:
        log.append(f"[Rule Check] {row['name']} ({shift['location_name']}): {row['verdict']} - {row['reason']}")

    search_tier = "local"
    if not eligible:
        log.append(
            f"[Filter] No compliant candidate at {shift['location_name']}. "
            f"Expanding search to every other outlet."
        )
        checked_names = {w["name"] for w in local_pool}
        wider_pool = [
            dict(row) for row in db.get_candidate_pool(exclude_name=state["absent_worker"])
            if row["name"] not in checked_names
        ]
        cross_eligible, cross_audit = _check_pool(wider_pool, target_shift, shift_lat, shift_lon)
        for row in cross_audit:
            log.append(f"[Rule Check] {row['name']} (cross-outlet): {row['verdict']} - {row['reason']}")
        audit += cross_audit
        eligible = cross_eligible
        search_tier = "cross_location"

    ranked = rank_candidates(eligible)
    selected = ranked[0] if ranked else {}

    if selected:
        tier_note = "same outlet" if search_tier == "local" else f"{selected['distance_km']:.1f} km away, cross-outlet"
        log.append(
            f"[Rule Check] Selected {selected['name']} ({tier_note}, "
            f"{selected['hours_worked_this_week']} hrs worked this week -- "
            f"lowest overtime risk among eligible candidates)."
        )
        status = state.get("status", "IN_PROGRESS")
    else:
        log.append("[Rule Check] No eligible candidates found anywhere. Escalating to manager.")
        status = "ESCALATED"

    return {
        **state,
        "search_tier": search_tier,
        "eligible_candidates": ranked,
        "candidate_audit": audit,
        "selected_candidate": selected,
        "reasoning_log": log,
        "status": status,
    }


def draft_message_node(state: ShiftPilotState) -> ShiftPilotState:
    """[Draft] The one node that touches an LLM. Given the selected
    candidate's real, already-computed attributes (distance, reliability
    history, hours) and a deterministic pay band, ask the LLM to: pick a
    specific offer within that band, justify the pick in plain English,
    and draft the outreach message. Retries a failing call up to
    max_iterations before falling back to a deterministic (but still
    real-data-grounded) template -- the bounded-loop guard.
    """
    log = list(state.get("reasoning_log", []))
    candidate = state.get("selected_candidate") or {}
    shift = state["shift_details"]

    if not candidate:
        log.append("[Draft] Skipped -- no eligible candidate to message.")
        return {**state, "reasoning_log": log, "drafted_message": ""}

    # ---- deterministic inputs the LLM will reason over, never invent ----
    shift_start = datetime.fromisoformat(shift["start_time"])
    notice_hours = max(0.1, (shift_start - datetime.now()).total_seconds() / 3600)
    max_multiplier = db.get_max_premium_multiplier()
    pay_band = compute_pay_band(
        base_rate=candidate["hourly_rate"],
        notice_hours=notice_hours,
        distance_km=candidate["distance_km"],
        max_multiplier=max_multiplier,
    )
    accepted, total = db.get_worker_reliability(candidate["id"])
    reliability = _reliability_text(accepted, total)

    log.append(
        f"[Draft] Pay band for {candidate['name']}: ${pay_band['min_rate']:.2f}-"
        f"${pay_band['max_rate']:.2f}/hr (up to {pay_band['ceiling_multiplier']}x base, "
        f"policy cap {max_multiplier}x). Notice: {notice_hours:.1f} hrs."
    )

    llm = _get_llm()
    iterations = state.get("iterations", 0)
    max_iterations = state.get("max_iterations", 3)
    parsed = None

    if llm is None:
        log.append("[Draft] No LLM provider configured -- used deterministic reasoning.")
    else:
        prompt = (
            "You are ShiftPilot, a scheduling assistant helping a retail/F&B store manager "
            f"fill a last-minute {shift['role']} shift at {shift['location_name']} today "
            f"from {shift['start_time'][-8:-3]} to {shift['end_time'][-8:-3]} "
            f"({shift.get('duration')} hrs). {state['absent_worker']} called in sick.\n\n"
            f"You are reaching out to {candidate['name']}, who is {candidate['distance_km']:.1f} km "
            f"from this outlet, normally earns ${candidate['hourly_rate']:.2f}/hr, and {reliability}.\n\n"
            f"Store policy allows offering between ${pay_band['min_rate']:.2f}/hr and "
            f"${pay_band['max_rate']:.2f}/hr for this request ({notice_hours:.1f} hrs notice). "
            "Pick a specific rate inside that range (you may not go outside it) and justify your "
            "choice using the real numbers above -- distance, notice, and reliability. Then draft "
            "a short, warm, professional WhatsApp message (3-5 sentences) making the offer, "
            "explaining briefly that a colleague called in sick, making clear it's okay to decline, "
            "and asking them to reply YES or NO.\n\n"
            "Respond in EXACTLY this format, nothing else:\n"
            "OFFERED_RATE: <number>\n"
            "JUSTIFICATION: <1-2 sentences>\n"
            "MESSAGE: <the WhatsApp message, no signature block>"
        )
        while iterations < max_iterations and parsed is None:
            iterations += 1
            try:
                response = llm.invoke(prompt)
                text = getattr(response, "content", str(response))
                parsed = _parse_llm_response(text, pay_band)
                if parsed is None:
                    log.append(f"[Draft] LLM response on attempt {iterations} didn't match the expected format.")
            except Exception as exc:
                log.append(f"[Draft] LLM call failed on attempt {iterations}/{max_iterations} ({exc}).")

        if parsed is None:
            log.append(f"[Draft] Exhausted {max_iterations} attempts -- used deterministic reasoning.")
        else:
            log.append(f"[Draft] LLM proposed ${parsed[0]:.2f}/hr for {candidate['name']} (attempt {iterations}).")

    if parsed is None:
        offered_rate = round((pay_band["min_rate"] + pay_band["max_rate"]) / 2, 2)
        justification, message = _fallback_reasoning(
            {**candidate, "distance_km": candidate["distance_km"]},
            {**shift, "absent_worker": state["absent_worker"]},
            notice_hours,
            offered_rate,
        )
    else:
        offered_rate, justification, message = parsed

    return {
        **state,
        "reasoning_log": log,
        "pay_band": pay_band,
        "notice_hours": notice_hours,
        "offered_rate": offered_rate,
        "justification": justification,
        "drafted_message": message,
        "iterations": iterations,
    }


def human_approval_gate_node(state: ShiftPilotState) -> ShiftPilotState:
    """Halt the workflow for manager sign-off before anything is dispatched."""
    log = list(state.get("reasoning_log", []))

    if state.get("status") == "ESCALATED":
        log.append("[Gate] No message to approve -- awaiting manual manager intervention.")
        return {**state, "reasoning_log": log}

    log.append(
        f"[Gate] Halting for manager approval. Candidate: "
        f"{state.get('selected_candidate', {}).get('name', 'N/A')} at "
        f"${state.get('offered_rate', 0):.2f}/hr. Message drafted and ready to dispatch."
    )
    return {**state, "reasoning_log": log, "status": "AWAITING_MANAGER_APPROVAL"}


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_graph():
    graph = StateGraph(ShiftPilotState)

    graph.add_node("investigate_disruption", investigate_disruption_node)
    graph.add_node("evaluate_candidates", evaluate_candidates_node)
    graph.add_node("draft_message", draft_message_node)
    graph.add_node("human_approval_gate", human_approval_gate_node)

    graph.add_edge(START, "investigate_disruption")
    graph.add_edge("investigate_disruption", "evaluate_candidates")
    graph.add_edge("evaluate_candidates", "draft_message")
    graph.add_edge("draft_message", "human_approval_gate")
    graph.add_edge("human_approval_gate", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Manual test: Sarah Lee sick-call scenario (local resolution)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    db.reset_database()  # guarantee a clean, reproducible run
    app = build_graph()

    # Pull Sarah Lee's actual seeded shift rather than hardcoding a time,
    # so notice_hours reflects the real (dynamic) seed window.
    sarah_shift = next(
        row for row in db.get_today_schedule() if row["worker_name"] == "Sarah Lee"
    )
    location = db.get_location(sarah_shift["location_id"])
    start_dt, end_dt, duration = shift_datetimes(
        sarah_shift["date"], sarah_shift["start_time"], sarah_shift["end_time"]
    )

    initial_state = new_state(
        disruption_type="SICK_LEAVE",
        absent_worker="Sarah Lee",
        shift_details={
            "role": sarah_shift["role"],
            "date": sarah_shift["date"],
            "start_time": start_dt.isoformat(),
            "end_time": end_dt.isoformat(),
            "duration": duration,
            "location_id": location["id"],
            "location_name": location["name"],
            "lat": location["lat"],
            "lon": location["lon"],
        },
    )

    final_state = app.invoke(initial_state)

    print("=" * 70)
    print("REASONING LOG")
    print("=" * 70)
    for entry in final_state["reasoning_log"]:
        print(f"  {entry}")

    print()
    print("=" * 70)
    print(f"STATUS: {final_state['status']} | search tier: {final_state['search_tier']}")
    print("=" * 70)
    print(f"Selected candidate: {final_state['selected_candidate'].get('name')}")
    print(f"Offered rate: ${final_state['offered_rate']:.2f}/hr (band: {final_state['pay_band']})")
    print(f"Justification: {final_state['justification']}")
    print()
    print("Drafted message:")
    print(f"  {final_state['drafted_message']}")
