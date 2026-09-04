"""ShiftPilot standalone Streamlit UI simulation. Run: streamlit run app.py"""

from __future__ import annotations

import pandas as pd
import streamlit as st


st.set_page_config(page_title="ShiftPilot | Autonomous Ops", page_icon="✦", layout="wide")

ROSTER = [
    {"Worker Name": "Aisha Rahman", "Shift Time": "08:00 - 16:00", "Role": "Shift Lead", "Status": "On Duty"},
    {"Worker Name": "Marcus Lim", "Shift Time": "10:00 - 18:00", "Role": "Cashier", "Status": "On Duty"},
    {"Worker Name": "Sarah Lee", "Shift Time": "14:00 - 22:00", "Role": "Barista", "Status": "Sick / Absent"},
    {"Worker Name": "Priya Nair", "Shift Time": "16:00 - 22:00", "Role": "Service Crew", "Status": "Scheduled"},
    {"Worker Name": "Daniel Tan", "Shift Time": "Off", "Role": "Barista", "Status": "Available"},
]
WORKER_OPTIONS = [
    "Sarah Lee - Barista (14:00 - 22:00)",
    "Aisha Rahman - Shift Lead (08:00 - 16:00)",
    "Marcus Lim - Cashier (10:00 - 18:00)",
    "Priya Nair - Service Crew (16:00 - 22:00)",
]
DEFAULT_MESSAGE = (
    "Hi Daniel, hope you're doing well. Sarah is unwell and we need help covering "
    "the Barista shift today from 14:00 to 22:00. You're qualified and the shift "
    "meets your hours and rest requirements. Would you be available to step in? "
    "No worries if you can't — please let us know when convenient. Thank you!"
)


def initialise_state() -> None:
    """Create the in-memory state machine used by this UI-only simulation."""
    st.session_state.setdefault("repair_triggered", False)
    st.session_state.setdefault("outreach_dispatched", False)
    st.session_state.setdefault("worker_accepted", False)
    st.session_state.setdefault("message", DEFAULT_MESSAGE)


def style_status(value: object) -> str:
    """Highlight the disrupted roster status."""
    if value == "Sick / Absent":
        return "background-color: #fee2e2; color: #b91c1c; font-weight: 700"
    return ""


initialise_state()

st.markdown(
    """
    <style>
        .block-container {padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1500px;}
        [data-testid="stMetric"] {background:#fff; border:1px solid #e5e7eb;
            border-radius:14px; padding:14px 18px; box-shadow:0 4px 16px rgba(15,23,42,.04);}
        .eyebrow {color:#4f46e5; font-size:.78rem; font-weight:800; letter-spacing:.12em;}
        .subtitle {color:#64748b; margin-top:-.65rem; margin-bottom:1.4rem;}
        .agent-step {background:#f8fafc; border-left:3px solid #6366f1;
            border-radius:0 8px 8px 0; margin:.55rem 0; padding:.7rem .9rem;}
        .approval-card {background:linear-gradient(135deg,#eef2ff 0%,#f8fafc 100%);
            border:1px solid #c7d2fe; border-radius:14px; margin:1rem 0; padding:1rem 1.1rem;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="eyebrow">AUTONOMOUS WORKFORCE OPERATIONS</div>', unsafe_allow_html=True)
st.title("✦ ShiftPilot")
st.markdown(
    '<div class="subtitle">Live staffing visibility and human-approved schedule repair.</div>',
    unsafe_allow_html=True,
)

metric_1, metric_2, metric_3 = st.columns(3)
with metric_1:
    st.metric("Store Status", "Normal" if st.session_state.worker_accepted else "Disrupted")
with metric_2:
    st.metric("Staff on Duty", "5 / 5" if st.session_state.worker_accepted else "4 / 5")
with metric_3:
    st.metric("Unfilled Shifts", "0" if st.session_state.worker_accepted else "1 Alert")

st.write("")
schedule_column, console_column = st.columns([1.35, 1], gap="large")

with schedule_column:
    st.subheader("Live Schedule Monitor")
    st.caption("Today · Single outlet · Live roster")
    roster = [row.copy() for row in ROSTER]
    if st.session_state.worker_accepted:
        roster[2] = {"Worker Name": "Daniel Tan", "Shift Time": "14:00 - 22:00", "Role": "Barista", "Status": "Cover Confirmed"}

    st.dataframe(
        pd.DataFrame(roster).style.map(style_status),
        hide_index=True,
        width="stretch",
        height=248,
        column_config={
            "Worker Name": st.column_config.TextColumn("Worker Name", width="medium"),
            "Shift Time": st.column_config.TextColumn("Shift Time", width="medium"),
            "Role": st.column_config.TextColumn("Role", width="small"),
            "Status": st.column_config.TextColumn("Status", width="medium"),
        },
    )

with console_column:
    st.subheader("Disruption & Agent Console")
    st.selectbox("Select Affected Worker", WORKER_OPTIONS, index=0, disabled=st.session_state.repair_triggered)
    if st.button(
        "⚡ Trigger Autonomous Schedule Repair", type="primary", width="stretch",
        disabled=st.session_state.repair_triggered,
    ):
        st.session_state.repair_triggered = True
        st.rerun()

    if not st.session_state.repair_triggered:
        st.info("Select the disrupted shift, then start the repair workflow.")
    else:
        with st.expander("Agent reasoning trace", expanded=True):
            steps = [
                "[1/3] Disruption logged: Sarah Lee absent.",
                "[2/3] Compliance Engine: Daniel Tan qualified (36 hrs total, rest period compliant).",
                "[3/3] Outreach drafted. Awaiting Manager Approval.",
            ]
            for step in steps:
                st.markdown(f'<div class="agent-step">{step}</div>', unsafe_allow_html=True)

        if not st.session_state.outreach_dispatched:
            st.markdown(
                '<div class="approval-card"><strong>Manager approval required</strong><br>'
                '<span style="color:#64748b">Recommended: Daniel Tan · Barista · 36 projected weekly hours</span></div>',
                unsafe_allow_html=True,
            )

        st.session_state.message = st.text_area(
            "Proposed WhatsApp message to Daniel Tan", value=st.session_state.message,
            height=160, disabled=st.session_state.outreach_dispatched,
        )

        approve_column, accept_column = st.columns(2)
        with approve_column:
            if st.button(
                "Approve & Dispatch Outreach", type="primary", width="stretch",
                disabled=st.session_state.outreach_dispatched,
            ):
                st.session_state.outreach_dispatched = True
                st.rerun()
        with accept_column:
            if st.button(
                "Simulate Worker Accept", width="stretch",
                disabled=not st.session_state.outreach_dispatched or st.session_state.worker_accepted,
            ):
                st.session_state.worker_accepted = True
                st.rerun()

        if st.session_state.outreach_dispatched and not st.session_state.worker_accepted:
            st.info("Outreach dispatched to Daniel Tan. Awaiting worker response.")

if st.session_state.worker_accepted:
    st.success("Roster updated: Daniel Tan assigned to 14:00-22:00 shift.", icon="✅")
