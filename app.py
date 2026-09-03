"""ShiftPilot -- Streamlit operations console.

This file owns presentation and human-in-the-loop control flow only:
    * It renders state, collects manager clicks, and decides *when* to
      call into the agent graph or the database.
    * It never calculates hours, rest periods, or eligibility itself --
      that's `rules.py`'s job, reached only through `agent.py`.
    * It never writes to the database directly except through the two
      narrow db.py entry points meant for the UI: `mark_shift_sick`
      (trigger a disruption) and `commit_shift_coverage` (the only place
      a schedule mutation is persisted, and only after approval + a
      simulated worker "yes").

Run with: streamlit run app.py
"""

from __future__ import annotations

import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import db
from agent import build_graph, draft_message_node, new_state

load_dotenv()

st.set_page_config(page_title="ShiftPilot", page_icon="🗓️", layout="wide")

STATUS_LABEL = {
    "scheduled": "🔵 scheduled",
    "sick": "🔴 sick",
    "covered": "🟣 covered",
    "completed": "⚪ completed",
}


# ---------------------------------------------------------------------------
# Small helpers (display/orchestration only -- no legal arithmetic)
# ---------------------------------------------------------------------------


def _iso(date_str: str, hhmm: str) -> str:
    return f"{date_str}T{hhmm}:00"


def _shift_duration_hours(start_hhmm: str, end_hhmm: str) -> float:
    start_h, start_m = (int(part) for part in start_hhmm.split(":"))
    end_h, end_m = (int(part) for part in end_hhmm.split(":"))
    return round(((end_h * 60 + end_m) - (start_h * 60 + start_m)) / 60, 2)


def _ensure_db() -> None:
    if not db.DB_PATH.exists():
        db.reset_database()


def _reset_flow_state() -> None:
    for key in ("agent_result", "disrupted_shift_id", "declined_names", "dispatched", "edited_message"):
        st.session_state.pop(key, None)


def _run_agent_for_shift(shift_row: dict) -> None:
    """Kick off the LangGraph run for a given shift and stash the result."""
    db.mark_shift_sick(shift_row["id"])

    initial_state = new_state(
        disruption_type="SICK_LEAVE",
        absent_worker=shift_row["worker_name"],
        shift_details={
            "role": shift_row["role"],
            "date": shift_row["date"],
            "start_time": _iso(shift_row["date"], shift_row["start_time"]),
            "end_time": _iso(shift_row["date"], shift_row["end_time"]),
            "duration": _shift_duration_hours(shift_row["start_time"], shift_row["end_time"]),
        },
    )

    graph = build_graph()
    result = graph.invoke(initial_state)

    st.session_state.agent_result = result
    st.session_state.disrupted_shift_id = shift_row["id"]
    st.session_state.declined_names = set()
    st.session_state.dispatched = False
    st.session_state.edited_message = result["drafted_message"]


def _advance_to_next_candidate() -> None:
    """After a simulated decline, redraft for the next eligible candidate."""
    result = st.session_state.agent_result
    declined = st.session_state.declined_names
    remaining = [c for c in result["eligible_candidates"] if c["name"] not in declined]

    if not remaining:
        result["status"] = "ESCALATED"
        result["selected_candidate"] = {}
        result["drafted_message"] = ""
        st.session_state.dispatched = False
        return

    result["selected_candidate"] = remaining[0]
    result = draft_message_node(result)
    result["status"] = "AWAITING_MANAGER_APPROVAL"
    st.session_state.agent_result = result
    st.session_state.dispatched = False
    st.session_state.edited_message = result["drafted_message"]


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("🗓️ ShiftPilot")
    st.caption("Agentic schedule-repair console")

    llm_key_present = any(
        os.environ.get(key)
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY")
    )
    if llm_key_present:
        st.success("LLM drafting: live", icon="🤖")
    else:
        st.warning("LLM drafting: template fallback (no API key set)", icon="🛟")

    st.divider()
    if st.button("🔄 Reset Demo", width='stretch'):
        db.reset_database()
        _reset_flow_state()
        st.rerun()

    st.caption("Resets the database and clears the current disruption run.")


_ensure_db()

st.title("Operations Console")
st.caption("Today's schedule, live disruption handling, and agent-drafted outreach.")


# ---------------------------------------------------------------------------
# Live roster
# ---------------------------------------------------------------------------

st.subheader("📋 Today's Schedule")

schedule = [dict(row) for row in db.get_today_schedule()]
if schedule:
    roster_df = pd.DataFrame(
        [
            {
                "Worker": row["worker_name"],
                "Role": row["role"],
                "Shift": f"{row['start_time']}–{row['end_time']}",
                "Status": STATUS_LABEL.get(row["status"], row["status"]),
            }
            for row in schedule
        ]
    )
    st.dataframe(roster_df, width='stretch', hide_index=True)
else:
    st.info("No shifts scheduled today. Reset the demo to reload sample data.")

st.divider()


# ---------------------------------------------------------------------------
# Disruption trigger
# ---------------------------------------------------------------------------

st.subheader("🚨 Disruption Trigger")

triggerable = [row for row in schedule if row["status"] == "scheduled"]
active_run = st.session_state.get("agent_result") is not None

if not triggerable and not active_run:
    st.caption("Nothing to disrupt right now -- every shift is already sick, covered, or completed.")
elif not active_run:
    options = {f"{row['worker_name']} — {row['role']} ({row['start_time']}–{row['end_time']})": row for row in triggerable}
    labels = list(options.keys())
    # Default to the flagship "Sarah Lee" scenario when present, so the
    # one-click judge demo doesn't depend on shift sort order.
    default_index = next((i for i, row in enumerate(triggerable) if row["worker_name"] == "Sarah Lee"), 0)
    choice_label = st.selectbox("Pick the shift going down", labels, index=default_index)
    chosen = options[choice_label]

    if st.button(f"📵 Simulate Sick Call ({chosen['worker_name']})", type="primary"):
        _run_agent_for_shift(chosen)
        st.rerun()
else:
    st.caption("A disruption is already in progress below -- resolve or reset it first.")

st.divider()


# ---------------------------------------------------------------------------
# Agent trace + approval gate
# ---------------------------------------------------------------------------

if active_run:
    result = st.session_state.agent_result
    st.subheader("🧠 Agent Reasoning Trace")

    with st.expander("Step-by-step reasoning log", expanded=True):
        for entry in result["reasoning_log"]:
            st.markdown(f"- {entry}")

    if result.get("candidate_audit"):
        st.markdown("**Rule audit -- every candidate considered:**")
        audit_df = pd.DataFrame(
            [
                {
                    "Candidate": row["name"],
                    "Role": row["role"],
                    "Hrs this week": row["hours_worked_this_week"],
                    "Verdict": "✅ APPROVED" if row["verdict"] == "APPROVED" else "❌ REJECTED",
                    "Reason": row["reason"],
                }
                for row in result["candidate_audit"]
            ]
        )
        st.dataframe(audit_df, width='stretch', hide_index=True)

    st.divider()

    if result["status"] == "ESCALATED" or not result.get("selected_candidate"):
        st.error("🚩 No compliant candidate available. Escalated to manager for manual handling.")
        if st.button("Acknowledge & Reset"):
            _reset_flow_state()
            st.rerun()

    elif not st.session_state.get("dispatched"):
        candidate = result["selected_candidate"]
        st.subheader("✅ Manager Approval Gate")
        st.markdown(
            f"**Top candidate:** {candidate['name']} ({candidate['role']}) -- "
            f"{candidate['hours_worked_this_week']} hrs worked this week, "
            f"${candidate.get('hourly_rate', 'N/A')}/hr"
        )

        edited = st.text_area(
            "Drafted WhatsApp message (editable before sending):",
            value=st.session_state.get("edited_message", result["drafted_message"]),
            height=140,
        )
        st.session_state.edited_message = edited

        col1, col2 = st.columns(2)
        with col1:
            if st.button("✅ Approve & Dispatch", type="primary", width='stretch'):
                st.session_state.dispatched = True
                st.rerun()
        with col2:
            if st.button("❌ Cancel Disruption", width='stretch'):
                db.set_shift_status(st.session_state.disrupted_shift_id, "scheduled")
                _reset_flow_state()
                st.rerun()

    else:
        candidate = result["selected_candidate"]
        st.success(f"📨 Message dispatched to {candidate['name']}. Awaiting their reply...")
        st.text_area("Message sent:", value=st.session_state.edited_message, height=120, disabled=True)

        col1, col2 = st.columns(2)
        with col1:
            if st.button("👍 Simulate Worker Reply: ACCEPT", type="primary", width='stretch'):
                shift = result["shift_details"]
                db.commit_shift_coverage(
                    st.session_state.disrupted_shift_id,
                    candidate["id"],
                    shift["duration"],
                )
                _reset_flow_state()
                st.success(f"Shift reassigned to {candidate['name']}. Roster updated below.")
                st.rerun()
        with col2:
            if st.button("👎 Simulate Worker Reply: DECLINE", width='stretch'):
                st.session_state.declined_names.add(candidate["name"])
                _advance_to_next_candidate()
                st.rerun()
