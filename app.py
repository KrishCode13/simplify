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

import html
import os
import re
import textwrap

import streamlit as st
from dotenv import load_dotenv

import db
from agent import build_graph, draft_message_node, new_state

load_dotenv()

st.set_page_config(page_title="ShiftPilot", page_icon="🗓️", layout="wide")

STATUS_META = {
    "scheduled": ("Scheduled", "blue"),
    "sick": ("Sick call", "alert"),
    "covered": ("Covered", "good"),
    "completed": ("Completed", "neutral"),
}

# [Bracket tag] -> accent used for the reasoning-trace rail
LOG_TAG_ACCENT = {
    "Inspect": "neutral",
    "Filter": "neutral",
    "Rule Check": "blue",
    "Draft": "amber",
    "Gate": "violet",
}


# ---------------------------------------------------------------------------
# Design system -- fonts, tokens, component CSS.
# Injected once; every custom block below only ever emits classed HTML,
# never inline styles, so this stylesheet is the single source of truth.
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

      /* Strip Streamlit's own chrome so the app doesn't read as a dev tool */
      #MainMenu, footer, [data-testid="stDecoration"]{display:none !important;}
      header[data-testid="stHeader"]{background:transparent !important; height:2.25rem;}
      div[data-testid="stToolbar"]{right:1rem;}

      html, body, [class^="css"], .stApp{
        font-family:"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        color:var(--ink);
      }
      .stApp{background:var(--paper);}
      .block-container{padding-top:1.5rem; max-width:900px;}

      section[data-testid="stSidebar"]{
        background:var(--sunken); border-right:1px solid var(--border);
      }
      section[data-testid="stSidebar"] .block-container{padding-top:2rem;}

      code, .mono{font-family:"JetBrains Mono", ui-monospace, monospace;}

      /* ---- headings & rhythm ---- */
      h1{font-weight:700 !important; font-size:1.7rem !important; letter-spacing:-.01em;}
      .app-lede{color:var(--ink-dim); font-size:.95rem; margin:-.6rem 0 2rem;}
      .section-head{
        display:flex; align-items:baseline; justify-content:space-between;
        margin:0 0 .9rem;
      }
      .section-head h3{
        font-size:.8rem !important; font-weight:600 !important; letter-spacing:.04em;
        text-transform:uppercase; color:var(--ink-dim); margin:0 !important;
      }
      .section-note{font-size:.8rem; color:var(--ink-faint);}
      hr{border-color:var(--border) !important; margin:2.4rem 0 !important;}

      /* ---- sidebar brand ---- */
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
        border-radius:var(--radius); padding:.55rem .7rem; margin-bottom:1rem;
        font-size:.82rem;
      }
      .status-badge .dot{width:7px; height:7px; border-radius:50%; flex:none;}
      .status-badge .dot.on{background:var(--good);}
      .status-badge .dot.off{background:var(--ink-faint);}

      /* ---- pills ---- */
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

      /* ---- roster ---- */
      .roster{
        border:1px solid var(--border); border-radius:var(--radius-lg);
        background:var(--surface); overflow:hidden; box-shadow:var(--shadow);
      }
      .roster-row{
        display:grid; grid-template-columns:1.4fr 1fr 1fr auto;
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

      /* ---- callouts (replace st.info/success/error/warning look) ---- */
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

      /* ---- reasoning trace ---- */
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

      /* ---- audit table ---- */
      .audit{border:1px solid var(--border); border-radius:var(--radius-lg); overflow:hidden; background:var(--surface);}
      .audit-row{
        display:grid; grid-template-columns:1.1fr .7fr auto 1.6fr;
        gap:1rem; align-items:center; padding:.7rem 1.1rem;
        border-bottom:1px solid var(--border-soft); font-size:.85rem;
      }
      .audit-row:last-child{border-bottom:none;}
      .audit-row.head{
        font-size:.7rem; font-weight:600; letter-spacing:.05em; text-transform:uppercase;
        color:var(--ink-faint); background:var(--sunken); padding:.55rem 1.1rem;
      }
      .audit-reason{color:var(--ink-dim);}

      /* ---- candidate profile row ---- */
      .profile{display:flex; align-items:center; gap:.75rem; margin-bottom:1.1rem;}
      .profile .avatar{
        width:38px; height:38px; border-radius:50%; background:var(--accent-soft);
        color:var(--accent-ink); display:flex; align-items:center; justify-content:center;
        font-weight:700; font-size:.85rem; flex:none;
      }
      .profile .name{font-weight:600; font-size:.98rem;}
      .profile .meta{color:var(--ink-dim); font-size:.82rem;}

      /* ---- st.container(border=True) restyled to match the design system ----
         (native Streamlit container, not a hand-rolled div -- a div opened in
         one st.markdown call can't visually wrap widgets from later calls) */
      div[data-testid="stVerticalBlockBorderWrapper"]:has(div[data-testid="stTextArea"]){
        border:1px solid var(--border) !important; border-radius:var(--radius-lg) !important;
        background:var(--surface) !important; box-shadow:var(--shadow); padding:.3rem .5rem;
      }

      /* ---- streamlit widget restyling ---- */
      div[data-testid="stButton"] > button{
        border-radius:8px !important; font-weight:600 !important; font-size:.85rem !important;
        padding:.5rem 1rem !important; border:1px solid var(--border) !important;
        background:var(--surface) !important; color:var(--ink) !important;
        box-shadow:none !important; transition:background .12s ease, border-color .12s ease;
      }
      div[data-testid="stButton"] > button:hover{
        border-color:var(--ink-faint) !important; background:var(--sunken) !important;
        color:var(--ink) !important;
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
    </style>
    """
)
# CommonMark ends a raw-HTML block at the first blank line -- strip them so
# the whole <style> block survives as one HTML block instead of spilling
# out into visible plain-text markdown partway through.
_CSS_BLOCK = "\n".join(line for line in _CSS_BLOCK.splitlines() if line.strip())

st.markdown(_CSS_BLOCK, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Small render helpers -- presentation only, no legal/business logic.
# ---------------------------------------------------------------------------


def _esc(value) -> str:
    return html.escape(str(value))


def _pill(label: str, kind: str) -> str:
    return f'<span class="pill pill-{kind}"><span class="dot"></span>{_esc(label)}</span>'


def _callout(kind: str, text: str) -> str:
    st.markdown(
        f'<div class="callout callout-{kind}"><span class="bar"></span><span>{text}</span></div>',
        unsafe_allow_html=True,
    )


def _render_roster(rows: list[dict]) -> None:
    body = ['<div class="roster">', '<div class="roster-row head"><span>Worker</span><span>Role</span><span>Shift</span><span>Status</span></div>']
    for row in rows:
        label, kind = STATUS_META.get(row["status"], (row["status"], "neutral"))
        body.append(
            '<div class="roster-row">'
            f'<span class="worker-name">{_esc(row["worker_name"])}</span>'
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
        rows.append(
            f'<div class="trace-row {accent}"><span class="tag">{_esc(tag)}</span><span class="msg">{_esc(msg)}</span></div>'
        )
    st.markdown(f'<div class="trace">{"".join(rows)}</div>', unsafe_allow_html=True)


def _render_audit(rows: list[dict]) -> None:
    body = [
        '<div class="audit">',
        '<div class="audit-row head"><span>Candidate</span><span>Hrs this week</span><span>Verdict</span><span>Reason</span></div>',
    ]
    for row in rows:
        approved = row["verdict"] == "APPROVED"
        pill = _pill("Approved", "good") if approved else _pill("Rejected", "alert")
        body.append(
            '<div class="audit-row">'
            f'<span class="worker-name">{_esc(row["name"])} <span class="worker-role">· {_esc(row["role"])}</span></span>'
            f'<span class="mono">{_esc(row["hours_worked_this_week"])}</span>'
            f'<span>{pill}</span>'
            f'<span class="audit-reason">{_esc(row["reason"])}</span>'
            '</div>'
        )
    body.append('</div>')
    st.markdown("".join(body), unsafe_allow_html=True)


def _initials(name: str) -> str:
    parts = name.split()
    return (parts[0][0] + parts[-1][0]).upper() if len(parts) > 1 else name[:2].upper()


def _render_profile(candidate: dict) -> None:
    st.markdown(
        '<div class="profile">'
        f'<div class="avatar">{_esc(_initials(candidate["name"]))}</div>'
        '<div>'
        f'<div class="name">{_esc(candidate["name"])}</div>'
        f'<div class="meta">{_esc(candidate["role"])} · {_esc(candidate["hours_worked_this_week"])} hrs this week · ${_esc(candidate.get("hourly_rate", "N/A"))}/hr</div>'
        '</div></div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Orchestration helpers (no legal arithmetic)
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
    st.markdown(
        '<div class="brand"><div class="mark">SP</div><div class="name">ShiftPilot</div></div>'
        '<div class="brand-sub">Schedule-repair console</div>',
        unsafe_allow_html=True,
    )

    llm_key_present = any(
        os.environ.get(key)
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY")
    )
    dot_class = "on" if llm_key_present else "off"
    status_text = "Live LLM drafting" if llm_key_present else "Template drafting (no API key)"
    st.markdown(
        f'<div class="status-badge"><span class="dot {dot_class}"></span>{status_text}</div>',
        unsafe_allow_html=True,
    )

    if st.button("Reset demo", width='stretch'):
        db.reset_database()
        _reset_flow_state()
        st.rerun()

    st.markdown('<p class="section-note">Reseeds the database and clears the current run.</p>', unsafe_allow_html=True)


_ensure_db()

st.title("Operations console")
st.markdown('<p class="app-lede">Today\'s schedule, live disruption handling, and agent-drafted outreach.</p>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Live roster
# ---------------------------------------------------------------------------

st.markdown('<div class="section-head"><h3>Today\'s schedule</h3></div>', unsafe_allow_html=True)

schedule = [dict(row) for row in db.get_today_schedule()]
if schedule:
    _render_roster(schedule)
else:
    _callout("neutral", "No shifts scheduled today. Reset the demo to reload sample data.")

st.markdown("<hr/>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Disruption trigger
# ---------------------------------------------------------------------------

st.markdown('<div class="section-head"><h3>Disruption trigger</h3></div>', unsafe_allow_html=True)

triggerable = [row for row in schedule if row["status"] == "scheduled"]
active_run = st.session_state.get("agent_result") is not None

if not triggerable and not active_run:
    _callout("neutral", "Nothing to disrupt right now — every shift is already sick, covered, or completed.")
elif not active_run:
    options = {f"{row['worker_name']} — {row['role']} ({row['start_time']}–{row['end_time']})": row for row in triggerable}
    labels = list(options.keys())
    default_index = next((i for i, row in enumerate(triggerable) if row["worker_name"] == "Sarah Lee"), 0)
    choice_label = st.selectbox("Pick the shift going down", labels, index=default_index)
    chosen = options[choice_label]

    if st.button(f"Simulate sick call — {chosen['worker_name']}", type="primary"):
        _run_agent_for_shift(chosen)
        st.rerun()
else:
    _callout("neutral", "A disruption is already in progress below — resolve or reset it first.")

st.markdown("<hr/>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Agent trace + approval gate
# ---------------------------------------------------------------------------

if active_run:
    result = st.session_state.agent_result

    st.markdown('<div class="section-head"><h3>Agent reasoning trace</h3></div>', unsafe_allow_html=True)
    with st.expander("Step-by-step log", expanded=True):
        _render_trace(result["reasoning_log"])

    if result.get("candidate_audit"):
        st.markdown('<div class="section-head" style="margin-top:1.6rem;"><h3>Rule audit</h3><span class="section-note">Every candidate considered</span></div>', unsafe_allow_html=True)
        _render_audit(result["candidate_audit"])

    st.markdown("<hr/>", unsafe_allow_html=True)

    if result["status"] == "ESCALATED" or not result.get("selected_candidate"):
        _callout("alert", "No compliant candidate available. Escalated to manager for manual handling.")
        st.write("")
        if st.button("Acknowledge & reset"):
            _reset_flow_state()
            st.rerun()

    elif not st.session_state.get("dispatched"):
        candidate = result["selected_candidate"]
        st.markdown('<div class="section-head"><h3>Manager approval gate</h3></div>', unsafe_allow_html=True)

        with st.container(border=True):
            _render_profile(candidate)

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
            _render_profile(candidate)
            _callout("good", f"Message dispatched to {_esc(candidate['name'])}. Awaiting their reply.")
            st.write("")
            st.text_area("Message sent", value=st.session_state.edited_message, height=110, disabled=True, label_visibility="collapsed")

            col1, col2 = st.columns([1, 1])
            with col1:
                if st.button("Simulate reply: Accept", type="primary", width='stretch'):
                    shift = result["shift_details"]
                    db.commit_shift_coverage(
                        st.session_state.disrupted_shift_id,
                        candidate["id"],
                        shift["duration"],
                    )
                    _reset_flow_state()
                    st.rerun()
            with col2:
                if st.button("Simulate reply: Decline", width='stretch'):
                    st.session_state.declined_names.add(candidate["name"])
                    _advance_to_next_candidate()
                    st.rerun()
