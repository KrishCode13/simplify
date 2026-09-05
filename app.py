"""ShiftPilot -- Streamlit operations console.

This file owns presentation and human-in-the-loop control flow only:
    * It renders state, collects manager clicks, and decides *when* to
      call into the agent graph or the database.
    * It never calculates hours, rest periods, eligibility, distance, or
      pay bands itself -- that's `rules.py`'s job, reached only through
      `agent.py`.
    * It never writes to the database directly except through db.py's
      narrow write functions: `mark_shift_sick` (trigger a disruption),
      `commit_shift_coverage` (the only place a schedule mutation is
      persisted, and only after approval + a simulated worker "yes"),
      `cancel_coverage` (undo one), `record_adhoc_offer` (the audit
      trail), and `set_max_premium_multiplier` (a manager policy edit).

Run with: streamlit run app.py
"""

from __future__ import annotations

import html
import os
import re
import textwrap

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import db
from agent import build_graph, draft_message_node, new_state, shift_datetimes

load_dotenv()

st.set_page_config(page_title="ShiftPilot", page_icon="🗓️", layout="wide")

STATUS_META = {
    "scheduled": ("Scheduled", "blue"),
    "sick": ("Sick call", "alert"),
    "covered": ("Covered", "good"),
    "completed": ("Completed", "neutral"),
}

LOG_TAG_ACCENT = {
    "Inspect": "neutral",
    "Filter": "neutral",
    "Rule Check": "blue",
    "Draft": "amber",
    "Gate": "violet",
}


# ---------------------------------------------------------------------------
# Design system
# ---------------------------------------------------------------------------

_CSS_BLOCK = textwrap.dedent(
    """
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
    <style>
      :root{
        --paper:#FAFAF8; --surface:#FFFFFF; --sunken:#F2F1EE;
        --border:#E8E6E1; --border-soft:#EFEDE8;
        --ink:#37352F; --ink-dim:#787774; --ink-faint:#9B9A97;
        --accent:#0071E3; --accent-soft:#E8F1FD; --accent-ink:#0B5FBF;
        --good:#1F8A4C; --good-soft:#E6F6EC;
        --alert:#C0392B; --alert-soft:#FBEAE8;
        --amber:#A5690C; --amber-soft:#F7EDD9;
        --violet:#6247AA; --violet-soft:#EFEAFA;
        --neutral-pill:#787774; --neutral-pill-soft:#F1F0ED;
        --radius:10px; --radius-lg:14px;
        --shadow:0 1px 2px rgba(55,53,47,.04), 0 4px 16px -8px rgba(55,53,47,.10);
      }
      #MainMenu, footer, [data-testid="stDecoration"]{display:none !important;}
      header[data-testid="stHeader"]{background:transparent !important; height:2.25rem;}
      div[data-testid="stToolbar"]{right:1rem;}
      html, body, [class^="css"], .stApp{
        font-family:"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        color:var(--ink);
      }
      .stApp{background:var(--paper);}
      .block-container{padding-top:1.5rem; max-width:1080px;}
      section[data-testid="stSidebar"]{background:var(--sunken); border-right:1px solid var(--border);}
      section[data-testid="stSidebar"] .block-container{padding-top:2rem;}
      code, .mono{font-family:"JetBrains Mono", ui-monospace, monospace;}
      h1{font-weight:700 !important; font-size:1.7rem !important; letter-spacing:-.01em;}
      .app-lede{color:var(--ink-dim); font-size:.95rem; margin:-.6rem 0 2rem;}
      .section-head{display:flex; align-items:baseline; justify-content:space-between; margin:0 0 .9rem;}
      .section-head h3{
        font-size:.8rem !important; font-weight:600 !important; letter-spacing:.04em;
        text-transform:uppercase; color:var(--ink-dim); margin:0 !important;
      }
      .section-note{font-size:.8rem; color:var(--ink-faint);}
      hr{border-color:var(--border) !important; margin:2.2rem 0 !important;}
      .brand{display:flex; align-items:center; gap:.55rem; margin-bottom:.15rem;}
      .brand .mark{
        width:26px; height:26px; border-radius:7px; background:var(--accent);
        display:flex; align-items:center; justify-content:center;
        color:#fff; font-weight:700; font-size:.8rem; flex:none;
      }
      .brand .name{font-weight:700; font-size:1rem;}
      .brand-sub{color:var(--ink-faint); font-size:.78rem; margin:0 0 1.4rem 34px;}
      .status-badge{
        display:flex; align-items:center; gap:.5rem;
        border:1px solid var(--border); background:var(--surface);
        border-radius:var(--radius); padding:.55rem .7rem; margin-bottom:1rem; font-size:.82rem;
      }
      .status-badge .dot{width:7px; height:7px; border-radius:50%; flex:none;}
      .status-badge .dot.on{background:var(--good);}
      .status-badge .dot.off{background:var(--ink-faint);}
      .pill{
        display:inline-flex; align-items:center; gap:.4rem;
        font-size:.76rem; font-weight:600; padding:.22rem .6rem .22rem .5rem;
        border-radius:999px; white-space:nowrap; line-height:1.3;
      }
      .pill .dot{width:6px; height:6px; border-radius:50%; flex:none;}
      .pill-blue{background:var(--accent-soft); color:var(--accent-ink);}
      .pill-blue .dot{background:var(--accent);}
      .pill-alert{background:var(--alert-soft); color:var(--alert);}
      .pill-alert .dot{background:var(--alert);}
      .pill-good{background:var(--good-soft); color:var(--good);}
      .pill-good .dot{background:var(--good);}
      .pill-neutral{background:var(--neutral-pill-soft); color:var(--neutral-pill);}
      .pill-neutral .dot{background:var(--neutral-pill);}
      .pill-amber{background:var(--amber-soft); color:var(--amber);}
      .pill-amber .dot{background:var(--amber);}
      .roster{
        border:1px solid var(--border); border-radius:var(--radius-lg);
        background:var(--surface); overflow:hidden; box-shadow:var(--shadow);
      }
      .roster-row{
        display:grid; grid-template-columns:1.2fr .9fr .9fr 1fr auto;
        align-items:center; gap:1rem; padding:.85rem 1.1rem;
        border-bottom:1px solid var(--border-soft);
      }
      .roster-row:last-child{border-bottom:none;}
      .roster-row.head{
        font-size:.72rem; font-weight:600; letter-spacing:.05em; text-transform:uppercase;
        color:var(--ink-faint); background:var(--sunken); padding:.6rem 1.1rem;
      }
      .worker-name{font-weight:600; font-size:.92rem;}
      .worker-role{color:var(--ink-dim); font-size:.85rem;}
      .shift-time{font-family:"JetBrains Mono",monospace; font-size:.82rem; color:var(--ink-dim);}
      .callout{
        border-radius:var(--radius); padding:.75rem .95rem; font-size:.87rem;
        border:1px solid var(--border-soft); display:flex; gap:.6rem; align-items:flex-start;
      }
      .callout .bar{width:3px; align-self:stretch; border-radius:2px; flex:none;}
      .callout-neutral{background:var(--sunken);}
      .callout-neutral .bar{background:var(--ink-faint);}
      .callout-alert{background:var(--alert-soft); color:#7A241B;}
      .callout-alert .bar{background:var(--alert);}
      .callout-good{background:var(--good-soft); color:#155C34;}
      .callout-good .bar{background:var(--good);}
      .callout-amber{background:var(--amber-soft); color:#6B4E08;}
      .callout-amber .bar{background:var(--amber);}
      .trace{display:flex; flex-direction:column; gap:.3rem;}
      .trace-row{
        display:flex; gap:.65rem; align-items:baseline;
        padding:.4rem .7rem; border-radius:6px; border-left:2.5px solid var(--ink-faint);
        background:var(--sunken); font-size:.83rem;
      }
      .trace-row .tag{
        font-family:"JetBrains Mono",monospace; font-weight:600; font-size:.7rem;
        color:var(--ink-dim); white-space:nowrap; min-width:5.6rem;
      }
      .trace-row .msg{color:var(--ink);}
      .trace-row.neutral{border-left-color:var(--ink-faint);}
      .trace-row.blue{border-left-color:var(--accent);}
      .trace-row.amber{border-left-color:var(--amber);}
      .trace-row.violet{border-left-color:var(--violet);}
      .trace-row.good{border-left-color:var(--good); background:var(--good-soft);}
      .trace-row.alert{border-left-color:var(--alert); background:var(--alert-soft);}
      .audit{border:1px solid var(--border); border-radius:var(--radius-lg); overflow:hidden; background:var(--surface);}
      .audit-row{
        display:grid; grid-template-columns:1.1fr .6fr .7fr auto 1.4fr;
        gap:1rem; align-items:center; padding:.7rem 1.1rem;
        border-bottom:1px solid var(--border-soft); font-size:.85rem;
      }
      .audit-row:last-child{border-bottom:none;}
      .audit-row.head{
        font-size:.7rem; font-weight:600; letter-spacing:.05em; text-transform:uppercase;
        color:var(--ink-faint); background:var(--sunken); padding:.55rem 1.1rem;
      }
      .audit-reason{color:var(--ink-dim);}
      .profile{display:flex; align-items:center; gap:.75rem; margin-bottom:1.1rem;}
      .profile .avatar{
        width:38px; height:38px; border-radius:50%; background:var(--accent-soft);
        color:var(--accent-ink); display:flex; align-items:center; justify-content:center;
        font-weight:700; font-size:.85rem; flex:none;
      }
      .profile .name{font-weight:600; font-size:.98rem;}
      .profile .meta{color:var(--ink-dim); font-size:.82rem;}
      .rate-compare{
        display:flex; align-items:baseline; gap:.6rem; margin:.2rem 0 .9rem;
        font-family:"JetBrains Mono",monospace;
      }
      .rate-compare .base{color:var(--ink-faint); font-size:.85rem; text-decoration:line-through;}
      .rate-compare .arrow{color:var(--ink-faint);}
      .rate-compare .offer{color:var(--good); font-weight:700; font-size:1.15rem;}
      div[data-testid="stVerticalBlockBorderWrapper"]:has(div[data-testid="stTextArea"]){
        border:1px solid var(--border) !important; border-radius:var(--radius-lg) !important;
        background:var(--surface) !important; box-shadow:var(--shadow); padding:.3rem .5rem;
      }
      div[data-testid="stButton"] > button{
        border-radius:8px !important; font-weight:600 !important; font-size:.85rem !important;
        padding:.5rem 1rem !important; border:1px solid var(--border) !important;
        background:var(--surface) !important; color:var(--ink) !important;
        box-shadow:none !important; transition:background .12s ease, border-color .12s ease;
      }
      div[data-testid="stButton"] > button:hover{
        border-color:var(--ink-faint) !important; background:var(--sunken) !important; color:var(--ink) !important;
      }
      div[data-testid="stButton"] > button[kind="primary"]{
        background:var(--accent) !important; border-color:var(--accent) !important; color:#fff !important;
      }
      div[data-testid="stButton"] > button[kind="primary"]:hover{background:var(--accent-ink) !important;}
      div[data-testid="stTextArea"] textarea{
        border-radius:var(--radius) !important; border-color:var(--border) !important;
        font-size:.88rem !important; background:var(--surface) !important;
      }
      div[data-testid="stSelectbox"] > div{border-radius:var(--radius) !important;}
      div[data-baseweb="select"] > div{border-color:var(--border) !important; border-radius:var(--radius) !important;}
      div[data-testid="stExpander"]{
        border:1px solid var(--border) !important; border-radius:var(--radius-lg) !important;
        background:var(--surface) !important; box-shadow:var(--shadow);
      }
      button[data-baseweb="tab"]{font-weight:600 !important; font-size:.85rem !important;}
      .stat-grid{display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:14px; margin-bottom:1.4rem;}
      .stat-tile{
        border:1px solid var(--border); border-radius:var(--radius-lg); background:var(--surface);
        padding:1rem 1.15rem; box-shadow:var(--shadow);
      }
      .stat-tile .label{
        font-size:.72rem; font-weight:600; letter-spacing:.05em; text-transform:uppercase;
        color:var(--ink-faint); margin-bottom:.35rem;
      }
      .stat-tile .value{font-family:"JetBrains Mono",monospace; font-size:1.5rem; font-weight:700; color:var(--ink);}
      .stat-tile .sub{font-size:.78rem; color:var(--ink-dim); margin-top:.2rem;}
      .emp-grid{display:grid; grid-template-columns:repeat(auto-fill, minmax(230px, 1fr)); gap:14px;}
      .emp-card{
        border:1px solid var(--border); border-radius:var(--radius-lg); background:var(--surface);
        padding:1rem 1.1rem; box-shadow:var(--shadow);
      }
      .emp-card .top{display:flex; align-items:center; gap:.65rem; margin-bottom:.7rem;}
      .emp-card .avatar{
        width:36px; height:36px; border-radius:50%; background:var(--accent-soft); color:var(--accent-ink);
        display:flex; align-items:center; justify-content:center; font-weight:700; font-size:.8rem; flex:none;
      }
      .emp-card .name{font-weight:600; font-size:.92rem;}
      .emp-card .role{color:var(--ink-dim); font-size:.78rem;}
      .emp-card .row{display:flex; justify-content:space-between; font-size:.8rem; padding:.2rem 0; color:var(--ink-dim);}
      .emp-card .row b{color:var(--ink); font-weight:600;}
      .history-table{border:1px solid var(--border); border-radius:var(--radius-lg); overflow:hidden; background:var(--surface);}
      .history-row{
        display:grid; grid-template-columns:1fr 1fr .8fr .8fr auto;
        gap:1rem; align-items:center; padding:.6rem 1.1rem;
        border-bottom:1px solid var(--border-soft); font-size:.82rem;
      }
      .history-row:last-child{border-bottom:none;}
      .history-row.head{
        font-size:.68rem; font-weight:600; letter-spacing:.05em; text-transform:uppercase;
        color:var(--ink-faint); background:var(--sunken); padding:.5rem 1.1rem;
      }
    </style>
    """
)
_CSS_BLOCK = "\n".join(line for line in _CSS_BLOCK.splitlines() if line.strip())
st.markdown(_CSS_BLOCK, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Small render helpers -- presentation only, no legal/business logic.
# ---------------------------------------------------------------------------


def _esc(value) -> str:
    return html.escape(str(value))


def _pill(label: str, kind: str) -> str:
    return f'<span class="pill pill-{kind}"><span class="dot"></span>{_esc(label)}</span>'


def _callout(kind: str, text: str) -> None:
    st.markdown(
        f'<div class="callout callout-{kind}"><span class="bar"></span><span>{text}</span></div>',
        unsafe_allow_html=True,
    )


def _initials(name: str) -> str:
    parts = name.split()
    return (parts[0][0] + parts[-1][0]).upper() if len(parts) > 1 else name[:2].upper()


def _reliability_label(worker_id: int) -> str:
    accepted, total = db.get_worker_reliability(worker_id)
    return "No track record yet" if total == 0 else f"{accepted}/{total} accepted"


def _render_roster(rows: list[dict]) -> None:
    body = [
        '<div class="roster">',
        '<div class="roster-row head"><span>Worker</span><span>Outlet</span><span>Role</span>'
        '<span>Shift</span><span>Status</span></div>',
    ]
    for row in rows:
        label, kind = STATUS_META.get(row["status"], (row["status"], "neutral"))
        body.append(
            '<div class="roster-row">'
            f'<span class="worker-name">{_esc(row["worker_name"])}</span>'
            f'<span class="worker-role">{_esc(row["location_name"])}</span>'
            f'<span class="worker-role">{_esc(row["role"])}</span>'
            f'<span class="shift-time">{_esc(row["start_time"])}–{_esc(row["end_time"])}</span>'
            f'<span>{_pill(label, kind)}</span>'
            '</div>'
        )
    body.append('</div>')
    st.markdown("".join(body), unsafe_allow_html=True)


def _render_trace(log_lines: list[str]) -> None:
    rows = []
    for line in log_lines:
        match = re.match(r"^\[(.*?)\]\s*(.*)$", line)
        tag, msg = match.groups() if match else ("", line)
        accent = LOG_TAG_ACCENT.get(tag, "neutral")
        if "REJECTED" in msg:
            accent = "alert"
        elif "APPROVED" in msg or "Selected" in msg:
            accent = "good"
        rows.append(f'<div class="trace-row {accent}"><span class="tag">{_esc(tag)}</span><span class="msg">{_esc(msg)}</span></div>')
    st.markdown(f'<div class="trace">{"".join(rows)}</div>', unsafe_allow_html=True)


def _render_audit(rows: list[dict]) -> None:
    body = [
        '<div class="audit">',
        '<div class="audit-row head"><span>Candidate</span><span>Hrs/wk</span><span>Distance</span>'
        '<span>Verdict</span><span>Reason</span></div>',
    ]
    for row in rows:
        approved = row["verdict"] == "APPROVED"
        pill = _pill("Approved", "good") if approved else _pill("Rejected", "alert")
        body.append(
            '<div class="audit-row">'
            f'<span class="worker-name">{_esc(row["name"])} <span class="worker-role">· {_esc(row["role"])}</span></span>'
            f'<span class="mono">{_esc(row["hours_worked_this_week"])}</span>'
            f'<span class="mono">{row["distance_km"]:.1f} km</span>'
            f'<span>{pill}</span>'
            f'<span class="audit-reason">{_esc(row["reason"])}</span>'
            '</div>'
        )
    body.append('</div>')
    st.markdown("".join(body), unsafe_allow_html=True)


def _render_profile(candidate: dict, location_name: str) -> None:
    st.markdown(
        '<div class="profile">'
        f'<div class="avatar">{_esc(_initials(candidate["name"]))}</div>'
        '<div>'
        f'<div class="name">{_esc(candidate["name"])}</div>'
        f'<div class="meta">{_esc(candidate["role"])} · {candidate["distance_km"]:.1f} km from {_esc(location_name)} '
        f'· {_esc(_reliability_label(candidate["id"]))}</div>'
        '</div></div>',
        unsafe_allow_html=True,
    )


def _render_rate_compare(base_rate: float, offered_rate: float) -> None:
    st.markdown(
        '<div class="rate-compare">'
        f'<span class="base">${base_rate:.2f}/hr</span>'
        '<span class="arrow">→</span>'
        f'<span class="offer">${offered_rate:.2f}/hr</span>'
        '</div>',
        unsafe_allow_html=True,
    )


def _render_stat_tile(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="sub">{_esc(sub)}</div>' if sub else ""
    return f'<div class="stat-tile"><div class="label">{_esc(label)}</div><div class="value">{_esc(value)}</div>{sub_html}</div>'


# ---------------------------------------------------------------------------
# Orchestration helpers (no legal arithmetic)
# ---------------------------------------------------------------------------


def _ensure_db() -> None:
    if db.needs_reset():
        db.reset_database()


def _reset_flow_state() -> None:
    for key in ("agent_result", "disrupted_shift_id", "declined_names", "dispatched", "edited_message"):
        st.session_state.pop(key, None)


def _run_agent_for_shift(shift_row: dict) -> None:
    """Kick off the LangGraph run for a given shift and stash the result."""
    db.mark_shift_sick(shift_row["id"])
    location = db.get_location(shift_row["location_id"])
    start_dt, end_dt, duration = shift_datetimes(shift_row["date"], shift_row["start_time"], shift_row["end_time"])

    initial_state = new_state(
        disruption_type="SICK_LEAVE",
        absent_worker=shift_row["worker_name"],
        shift_details={
            "role": shift_row["role"],
            "date": shift_row["date"],
            "start_time": start_dt.isoformat(),
            "end_time": end_dt.isoformat(),
            "duration": duration,
            "location_id": location["id"],
            "location_name": location["name"],
            "lat": location["lat"],
            "lon": location["lon"],
        },
    )

    result = build_graph().invoke(initial_state)

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
    st.markdown(
        '<div class="brand"><div class="mark">SP</div><div class="name">ShiftPilot</div></div>'
        '<div class="brand-sub">Schedule-repair console</div>',
        unsafe_allow_html=True,
    )

    llm_key_present = any(
        os.environ.get(key) for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY")
    ) or bool(os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))
    dot_class = "on" if llm_key_present else "off"
    status_text = "Live LLM reasoning" if llm_key_present else "Deterministic fallback (no API key)"
    st.markdown(f'<div class="status-badge"><span class="dot {dot_class}"></span>{status_text}</div>', unsafe_allow_html=True)

    if st.button("Reset demo", width='stretch'):
        db.reset_database()
        _reset_flow_state()
        st.rerun()

    st.markdown('<p class="section-note">Reseeds the database and clears the current run.</p>', unsafe_allow_html=True)


_ensure_db()

st.title("Operations console")
st.markdown(
    '<p class="app-lede">5 outlets, live disruption handling, distance- and urgency-aware pay, and agent-drafted outreach.</p>',
    unsafe_allow_html=True,
)

tab_console, tab_team, tab_cost = st.tabs(["Console", "Team directory", "Cost dashboard"])


# ---------------------------------------------------------------------------
# TAB 1 -- Console
# ---------------------------------------------------------------------------

with tab_console:
    st.markdown('<div class="section-head"><h3>Today\'s schedule</h3></div>', unsafe_allow_html=True)

    schedule = [dict(row) for row in db.get_today_schedule()]
    locations = [dict(row) for row in db.get_locations()]
    location_names = ["All outlets"] + [loc["name"] for loc in locations]
    location_filter = st.selectbox("Filter by outlet", location_names, label_visibility="collapsed")

    visible_schedule = schedule if location_filter == "All outlets" else [
        row for row in schedule if row["location_name"] == location_filter
    ]

    if visible_schedule:
        _render_roster(visible_schedule)
    else:
        _callout("neutral", "No shifts scheduled today. Reset the demo to reload sample data.")

    covered_shifts = [row for row in schedule if row["status"] == "covered"]
    if covered_shifts:
        with st.expander(f"Manage coverage ({len(covered_shifts)} shift(s) covered)"):
            options = {
                f"{row['worker_name']} covering {row['original_worker_name']}'s {row['role']} shift "
                f"at {row['location_name']} ({row['start_time']}–{row['end_time']})": row
                for row in covered_shifts
            }
            pick_label = st.selectbox("Covered shift", list(options.keys()))
            picked = options[pick_label]
            if st.button("Cancel this coverage", key="cancel_coverage_btn"):
                db.cancel_coverage(picked["id"])
                st.success(f"Coverage cancelled. {picked['original_worker_name']}'s shift is unstaffed again.")
                st.rerun()

    st.markdown("<hr/>", unsafe_allow_html=True)

    st.markdown('<div class="section-head"><h3>Disruption trigger</h3></div>', unsafe_allow_html=True)

    triggerable = [row for row in schedule if row["status"] == "scheduled"]
    active_run = st.session_state.get("agent_result") is not None

    if not triggerable and not active_run:
        _callout("neutral", "Nothing to disrupt right now -- every shift is already sick, covered, or completed.")
    elif not active_run:
        options = {
            f"{row['worker_name']} — {row['role']} @ {row['location_name']} ({row['start_time']}–{row['end_time']})": row
            for row in triggerable
        }
        labels = list(options.keys())
        default_index = next((i for i, row in enumerate(triggerable) if row["worker_name"] == "Sarah Lee"), 0)
        choice_label = st.selectbox("Pick the shift going down", labels, index=default_index)
        chosen = options[choice_label]

        if st.button(f"Simulate sick call — {chosen['worker_name']}", type="primary"):
            _run_agent_for_shift(chosen)
            st.rerun()
    else:
        _callout("neutral", "A disruption is already in progress below -- resolve or reset it first.")

    st.markdown("<hr/>", unsafe_allow_html=True)

    if active_run:
        result = st.session_state.agent_result
        location_name = result["shift_details"]["location_name"]

        st.markdown('<div class="section-head"><h3>Agent reasoning trace</h3></div>', unsafe_allow_html=True)
        if result.get("search_tier") == "cross_location":
            _callout("amber", f"No compliant candidate at {location_name} -- search expanded to every other outlet.")
            st.write("")
        with st.expander("Step-by-step log", expanded=True):
            _render_trace(result["reasoning_log"])

        if result.get("candidate_audit"):
            st.markdown(
                '<div class="section-head" style="margin-top:1.6rem;"><h3>Rule audit</h3>'
                '<span class="section-note">Every candidate considered</span></div>',
                unsafe_allow_html=True,
            )
            _render_audit(result["candidate_audit"])

        st.markdown("<hr/>", unsafe_allow_html=True)

        if result["status"] == "ESCALATED" or not result.get("selected_candidate"):
            _callout("alert", "No compliant candidate available anywhere. Escalated to manager for manual handling.")
            st.write("")
            if st.button("Acknowledge & reset"):
                _reset_flow_state()
                st.rerun()

        elif not st.session_state.get("dispatched"):
            candidate = result["selected_candidate"]
            st.markdown('<div class="section-head"><h3>Manager approval gate</h3></div>', unsafe_allow_html=True)

            with st.container(border=True):
                _render_profile(candidate, location_name)
                _render_rate_compare(candidate["hourly_rate"], result["offered_rate"])
                _callout("amber", _esc(result["justification"]))
                st.write("")

                edited = st.text_area(
                    "Drafted message (editable before sending)",
                    value=st.session_state.get("edited_message", result["drafted_message"]),
                    height=130,
                    label_visibility="collapsed",
                )
                st.session_state.edited_message = edited

                col1, col2 = st.columns([1, 1])
                with col1:
                    if st.button("Approve & dispatch", type="primary", width='stretch'):
                        st.session_state.dispatched = True
                        st.rerun()
                with col2:
                    if st.button("Cancel disruption", width='stretch'):
                        db.set_shift_status(st.session_state.disrupted_shift_id, "scheduled")
                        _reset_flow_state()
                        st.rerun()

        else:
            candidate = result["selected_candidate"]
            st.markdown('<div class="section-head"><h3>Awaiting worker reply</h3></div>', unsafe_allow_html=True)

            with st.container(border=True):
                _render_profile(candidate, location_name)
                _render_rate_compare(candidate["hourly_rate"], result["offered_rate"])
                _callout("good", f"Message dispatched to {_esc(candidate['name'])}. Awaiting their reply.")
                st.write("")
                st.text_area("Message sent", value=st.session_state.edited_message, height=110, disabled=True, label_visibility="collapsed")

                col1, col2 = st.columns([1, 1])
                with col1:
                    if st.button("Simulate reply: Accept", type="primary", width='stretch'):
                        shift = result["shift_details"]
                        db.commit_shift_coverage(st.session_state.disrupted_shift_id, candidate["id"], shift["duration"])
                        db.record_adhoc_offer(
                            worker_id=candidate["id"], shift_role=shift["role"], shift_hours=shift["duration"],
                            base_rate=candidate["hourly_rate"], offered_rate=result["offered_rate"],
                            distance_km=candidate["distance_km"], notice_hours=result["notice_hours"],
                            outcome="accepted", reasoning=result["justification"],
                        )
                        _reset_flow_state()
                        st.rerun()
                with col2:
                    if st.button("Simulate reply: Decline", width='stretch'):
                        shift = result["shift_details"]
                        db.record_adhoc_offer(
                            worker_id=candidate["id"], shift_role=shift["role"], shift_hours=shift["duration"],
                            base_rate=candidate["hourly_rate"], offered_rate=result["offered_rate"],
                            distance_km=candidate["distance_km"], notice_hours=result["notice_hours"],
                            outcome="declined", reasoning=result["justification"],
                        )
                        st.session_state.declined_names.add(candidate["name"])
                        _advance_to_next_candidate()
                        st.rerun()


# ---------------------------------------------------------------------------
# TAB 2 -- Team directory
# ---------------------------------------------------------------------------

with tab_team:
    st.markdown('<div class="section-head"><h3>Team directory</h3></div>', unsafe_allow_html=True)

    locations = [dict(row) for row in db.get_locations()]
    location_names = ["All outlets"] + [loc["name"] for loc in locations]
    team_filter = st.selectbox("Filter by outlet", location_names, key="team_filter", label_visibility="collapsed")

    all_workers = [dict(row) for row in db.get_candidate_pool(exclude_name="")]
    visible_workers = all_workers if team_filter == "All outlets" else [
        w for w in all_workers if next(loc["name"] for loc in locations if loc["id"] == w["location_id"]) == team_filter
    ]
    visible_workers.sort(key=lambda w: w["name"])

    cards = []
    for worker in visible_workers:
        loc_name = next(loc["name"] for loc in locations if loc["id"] == worker["location_id"])
        cards.append(
            '<div class="emp-card">'
            '<div class="top">'
            f'<div class="avatar">{_esc(_initials(worker["name"]))}</div>'
            f'<div><div class="name">{_esc(worker["name"])}</div><div class="role">{_esc(worker["role"])} · {_esc(loc_name)}</div></div>'
            '</div>'
            f'<div class="row"><span>Base rate</span><b>${worker["hourly_rate"]:.2f}/hr</b></div>'
            f'<div class="row"><span>Hours this week</span><b>{worker["hours_worked_this_week"]:.1f}</b></div>'
            f'<div class="row"><span>Ad-hoc reliability</span><b>{_esc(_reliability_label(worker["id"]))}</b></div>'
            '</div>'
        )
    st.markdown(f'<div class="emp-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# TAB 3 -- Cost dashboard
# ---------------------------------------------------------------------------

with tab_cost:
    st.markdown('<div class="section-head"><h3>Ad-hoc coverage policy</h3></div>', unsafe_allow_html=True)

    current_cap = db.get_max_premium_multiplier()
    new_cap = st.number_input(
        "Maximum pay-premium multiplier the agent may offer (e.g. 1.5 = never more than 1.5x base rate)",
        min_value=1.0, max_value=3.0, value=current_cap, step=0.1, format="%.1f",
    )
    if new_cap != current_cap:
        db.set_max_premium_multiplier(new_cap)
        st.success(f"Policy updated -- future offers are capped at {new_cap}x base rate.")

    st.markdown("<hr/>", unsafe_allow_html=True)
    st.markdown('<div class="section-head"><h3>Spend summary</h3></div>', unsafe_allow_html=True)

    offers = [dict(row) for row in db.get_all_offers()]
    accepted_offers = [o for o in offers if o["outcome"] == "accepted"]

    total_spend = sum(o["offered_rate"] * o["shift_hours"] for o in accepted_offers)
    total_premium = sum((o["offered_rate"] - o["base_rate"]) * o["shift_hours"] for o in accepted_offers)
    avg_multiplier = (
        sum(o["offered_rate"] / o["base_rate"] for o in accepted_offers) / len(accepted_offers)
        if accepted_offers else 0.0
    )
    accept_rate = (len(accepted_offers) / len(offers) * 100) if offers else 0.0

    tiles = [
        _render_stat_tile("Total ad-hoc spend", f"${total_spend:,.2f}", f"{len(accepted_offers)} covers"),
        _render_stat_tile("Premium paid over base", f"${total_premium:,.2f}", "the cost of flexibility"),
        _render_stat_tile("Avg. offer multiplier", f"{avg_multiplier:.2f}x", f"policy cap {current_cap}x"),
        _render_stat_tile("Offer acceptance rate", f"{accept_rate:.0f}%", f"{len(offers)} total offers"),
    ]
    st.markdown(f'<div class="stat-grid">{"".join(tiles)}</div>', unsafe_allow_html=True)

    if accepted_offers:
        st.markdown('<p class="section-note" style="margin-bottom:.5rem;">Spend by outlet</p>', unsafe_allow_html=True)
        spend_df = pd.DataFrame(accepted_offers)
        spend_df["spend"] = spend_df["offered_rate"] * spend_df["shift_hours"]
        by_location = spend_df.groupby("location_name")["spend"].sum().sort_values(ascending=False)
        st.bar_chart(by_location)

    st.markdown("<hr/>", unsafe_allow_html=True)
    st.markdown('<div class="section-head"><h3>Outlets</h3></div>', unsafe_allow_html=True)
    map_df = pd.DataFrame(locations)[["lat", "lon", "name"]]
    st.map(map_df, latitude="lat", longitude="lon", size=120)

    st.markdown("<hr/>", unsafe_allow_html=True)
    st.markdown('<div class="section-head"><h3>Ad-hoc offer history</h3></div>', unsafe_allow_html=True)

    if offers:
        rows = ['<div class="history-table">', '<div class="history-row head"><span>Worker</span><span>Outlet</span><span>Rate</span><span>Distance</span><span>Outcome</span></div>']
        for offer in offers[:25]:
            outcome_pill = _pill("Accepted", "good") if offer["outcome"] == "accepted" else _pill("Declined", "alert")
            rows.append(
                '<div class="history-row">'
                f'<span class="worker-name">{_esc(offer["worker_name"])}</span>'
                f'<span class="worker-role">{_esc(offer["location_name"])}</span>'
                f'<span class="mono">${offer["offered_rate"]:.2f}/hr</span>'
                f'<span class="mono">{offer["distance_km"]:.1f} km</span>'
                f'<span>{outcome_pill}</span>'
                '</div>'
            )
        rows.append('</div>')
        st.markdown("".join(rows), unsafe_allow_html=True)
    else:
        _callout("neutral", "No ad-hoc offers recorded yet.")
