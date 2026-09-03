"""ShiftPilot agent state machine.

LangGraph orchestration for the "unexpected disruption -> replacement staffing"
workflow described in the ShiftPilot hackathon spec.

Boundary this file respects:
    * Deterministic legal/scheduling logic (MOM hour caps, rest periods,
      candidate ranking) lives in ``rules.py`` and is imported, never
      re-implemented here.
    * Data access (workers, shifts) is expected to live in ``db.py``. That
      module isn't built yet in this sprint, so ``evaluate_candidates_node``
      falls back to an in-memory candidate pool that has the exact shape
      ``rules.py`` expects. Swapping in real ``db.py`` calls later is a
      one-function change.
    * The LLM is used ONLY for natural-language framing (the outreach
      message). It never does scheduling arithmetic or eligibility
      decisions -- that's already done by the time ``draft_message_node``
      runs.

Graph shape (this sprint):
    START -> investigate_disruption -> evaluate_candidates -> draft_message
          -> human_approval_gate -> END

The graph deliberately ends at the approval gate. Dispatch, worker replies,
and schedule mutation are later sprints (UI + db.py own that).
"""

from __future__ import annotations

import os
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

from rules import check_worker_compliance, rank_candidates


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class ShiftPilotState(TypedDict):
    disruption_type: str  # e.g. "SICK_LEAVE"
    absent_worker: str
    shift_details: dict  # keys: role, date, start_time, end_time, duration
    eligible_candidates: list[dict]
    selected_candidate: dict
    reasoning_log: list[str]
    drafted_message: str
    status: str  # e.g. "AWAITING_MANAGER_APPROVAL", "RESOLVED", "ESCALATED"


# ---------------------------------------------------------------------------
# Temporary in-memory worker pool
#
# Stand-in for db.py, which will own real SQLite reads. Shape matches what
# rules.check_worker_compliance() / rank_candidates() expect.
# ---------------------------------------------------------------------------

_CANDIDATE_POOL = [
    {
        "id": "W001",
        "name": "Marcus",
        "role": "Barista",
        "hours_worked_this_week": 24.0,
        "last_shift_end_time": "2026-09-03T06:00:00+08:00",
        "hourly_rate": 12.5,
    },
    {
        "id": "W002",
        "name": "Ravi",
        "role": "Barista",
        "hours_worked_this_week": 40.0,
        "last_shift_end_time": "2026-09-02T22:00:00+08:00",
        "hourly_rate": 12.5,
    },
    {
        "id": "W003",
        "name": "Daniel",
        "role": "Barista",
        "hours_worked_this_week": 16.0,
        "last_shift_end_time": "2026-09-02T22:00:00+08:00",
        "hourly_rate": 13.0,
    },
    {
        "id": "W004",
        "name": "Aisha",
        "role": "Barista",
        "hours_worked_this_week": 20.0,
        "last_shift_end_time": "2026-09-02T20:00:00+08:00",
        "hourly_rate": 12.5,
    },
]


# ---------------------------------------------------------------------------
# LLM helper
#
# Tries, in order: ChatAnthropic, ChatOpenAI, ChatGoogleGenerativeAI,
# ChatGroq -- whichever provider has an API key set in the environment and
# is installed. Falls back to a deterministic template so the graph still
# runs end-to-end (and the demo doesn't die) if no key/package is present.
# ---------------------------------------------------------------------------


def _get_llm():
    providers = [
        ("ANTHROPIC_API_KEY", "langchain_anthropic", "ChatAnthropic", "claude-sonnet-4-5"),
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
            return chat_cls(model=model_name, temperature=0.3)
        except Exception:
            continue
    return None


def _fallback_message(candidate: dict, shift: dict) -> str:
    """Deterministic template used when no LLM provider is configured."""
    return (
        f"Hi {candidate['name']}! This is ShiftPilot on behalf of the store. "
        f"We have an open {shift['role']} shift on {shift.get('date', 'today')} "
        f"from {shift['start_time']} to {shift['end_time']} "
        f"({shift.get('duration', '?')} hrs) at ${candidate.get('hourly_rate', 'N/A')}/hr. "
        f"Would you be able to cover it? No worries at all if you can't -- "
        f"just let us know either way. Thank you! 🙏"
    )


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def investigate_disruption_node(state: ShiftPilotState) -> ShiftPilotState:
    """[Inspect] Log the disruption and the shift it affects."""
    shift = state["shift_details"]
    log = list(state.get("reasoning_log", []))
    log.append(
        f"[Inspect] {state['disruption_type']} reported for {state['absent_worker']}. "
        f"Affected shift: {shift.get('role')} on {shift.get('date')} "
        f"{shift.get('start_time')}-{shift.get('end_time')} "
        f"({shift.get('duration')} hrs)."
    )
    return {**state, "reasoning_log": log}


def evaluate_candidates_node(state: ShiftPilotState) -> ShiftPilotState:
    """[Filter] + [Rule Check] Run every candidate through the deterministic
    compliance engine in rules.py, then rank the survivors.

    No scheduling arithmetic happens here that isn't already inside
    rules.py -- this node just orchestrates calls into it and records why
    each candidate was accepted or rejected.
    """
    shift = state["shift_details"]
    target_shift = {
        "role": shift["role"],
        "start_time": shift["start_time"],
        "end_time": shift["end_time"],
        "duration_hours": shift["duration"],
    }

    log = list(state.get("reasoning_log", []))
    log.append(
        f"[Filter] Checking {len(_CANDIDATE_POOL)} candidates in the "
        f"{shift['role']} role pool against MOM rules "
        f"(max 44 hrs/week, 11-hr min rest)."
    )

    eligible: list[dict] = []
    for worker in _CANDIDATE_POOL:
        if worker["name"] == state["absent_worker"]:
            continue  # can't cover your own shift
        compliant, reason = check_worker_compliance(worker, target_shift)
        verdict = "APPROVED" if compliant else "REJECTED"
        log.append(f"[Rule Check] {worker['name']}: {verdict} - {reason}")
        if compliant:
            eligible.append(worker)

    ranked = rank_candidates(eligible)
    selected = ranked[0] if ranked else {}

    if selected:
        log.append(
            f"[Rule Check] Selected {selected['name']} "
            f"({selected['hours_worked_this_week']} hrs worked this week -- "
            f"lowest among eligible candidates, minimizing overtime risk)."
        )
        status = state.get("status", "IN_PROGRESS")
    else:
        log.append("[Rule Check] No eligible candidates found. Escalating to manager.")
        status = "ESCALATED"

    return {
        **state,
        "eligible_candidates": ranked,
        "selected_candidate": selected,
        "reasoning_log": log,
        "status": status,
    }


def draft_message_node(state: ShiftPilotState) -> ShiftPilotState:
    """[Draft] Ask the LLM to write a friendly WhatsApp outreach message to
    the selected candidate. Skipped if no candidate was found upstream.
    """
    log = list(state.get("reasoning_log", []))
    candidate = state.get("selected_candidate") or {}
    shift = state["shift_details"]

    if not candidate:
        log.append("[Draft] Skipped -- no eligible candidate to message.")
        return {**state, "reasoning_log": log, "drafted_message": ""}

    llm = _get_llm()
    if llm is None:
        message = _fallback_message(candidate, shift)
        log.append("[Draft] No LLM provider configured -- used template message.")
    else:
        prompt = (
            "You are ShiftPilot, a scheduling assistant messaging a retail/F&B "
            "worker on behalf of their store manager. Write a short, warm, "
            "professional WhatsApp message (3-5 sentences, casual but respectful) "
            f"asking {candidate['name']} to cover an open {shift['role']} shift "
            f"on {shift.get('date', 'today')} from {shift['start_time']} to "
            f"{shift['end_time']} ({shift.get('duration')} hrs), paid at "
            f"${candidate.get('hourly_rate', 'N/A')}/hr. Explain briefly that a "
            f"colleague ({state['absent_worker']}) called in sick. Make clear "
            "it's okay to decline. End by asking them to reply YES or NO. "
            "Do not include a subject line or signature block."
        )
        try:
            response = llm.invoke(prompt)
            message = getattr(response, "content", str(response))
            log.append(f"[Draft] Drafted personalized message via LLM for {candidate['name']}.")
        except Exception as exc:
            message = _fallback_message(candidate, shift)
            log.append(f"[Draft] LLM call failed ({exc}) -- used template message.")

    return {**state, "reasoning_log": log, "drafted_message": message}


def human_approval_gate_node(state: ShiftPilotState) -> ShiftPilotState:
    """Halt the workflow for manager sign-off before anything is dispatched."""
    log = list(state.get("reasoning_log", []))

    if state.get("status") == "ESCALATED":
        log.append("[Gate] No message to approve -- awaiting manual manager intervention.")
        return {**state, "reasoning_log": log}

    log.append(
        f"[Gate] Halting for manager approval. Candidate: "
        f"{state.get('selected_candidate', {}).get('name', 'N/A')}. "
        f"Message drafted and ready to dispatch."
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
# Manual test: Sarah Lee sick-call scenario
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = build_graph()

    initial_state: ShiftPilotState = {
        "disruption_type": "SICK_LEAVE",
        "absent_worker": "Sarah Lee",
        "shift_details": {
            "role": "Barista",
            "date": "2026-09-03",
            "start_time": "2026-09-03T14:00:00+08:00",
            "end_time": "2026-09-03T22:00:00+08:00",
            "duration": 8.0,
        },
        "eligible_candidates": [],
        "selected_candidate": {},
        "reasoning_log": [],
        "drafted_message": "",
        "status": "IN_PROGRESS",
    }

    final_state = app.invoke(initial_state)

    print("=" * 70)
    print("REASONING LOG")
    print("=" * 70)
    for entry in final_state["reasoning_log"]:
        print(f"  {entry}")

    print()
    print("=" * 70)
    print(f"STATUS: {final_state['status']}")
    print("=" * 70)
    print(f"Selected candidate: {final_state['selected_candidate']}")
    print()
    print("Drafted message:")
    print(f"  {final_state['drafted_message']}")
