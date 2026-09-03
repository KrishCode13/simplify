# ShiftPilot

Agentic schedule-repair assistant for a single retail/F&B outlet. When a
worker calls in sick, ShiftPilot inspects the gap, runs the replacement
candidate pool through Singapore MOM Part IV rules (44-hr weekly cap,
11-hr minimum rest, role match), drafts an outreach message, and halts for
manager sign-off before anything touches the schedule.

## Architecture

| File          | Responsibility                                                            |
|---------------|----------------------------------------------------------------------------|
| `db.py`       | SQLite schema, seed data, and all reads/writes. No business logic.        |
| `rules.py`    | Pure-Python deterministic MOM compliance checks + ranking. No LLM, no I/O.|
| `agent.py`    | LangGraph state machine: inspect → evaluate → draft → approval gate.      |
| `app.py`      | Streamlit console: roster view, disruption trigger, HITL approval UI.     |

**Hard boundary:** the LLM (in `agent.py`'s `draft_message_node`) only ever
writes the outreach message. It never computes hours, rest gaps, or
eligibility -- that arithmetic lives exclusively in `rules.py` and is
called deterministically. If a candidate is rejected, the reason string
comes straight out of `rules.check_worker_compliance`, not a model.

The LangGraph run always stops at `human_approval_gate` (`END` right after).
Nothing is written back to `shiftpilot.db` until a manager clicks **Approve
& Dispatch** *and* the simulated worker reply is **ACCEPT** -- both handled
in `app.py`, never inside the graph itself.

## Setup

```bash
pip install -r requirements.txt
```

Optional -- for live LLM-drafted messages instead of the template fallback,
create a `.env` file with one of:

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GROQ_API_KEY=gsk-...
```

and install the matching optional package from `requirements.txt` (e.g.
`pip install langchain-anthropic`). No key set → ShiftPilot runs fine using
a deterministic message template, so the demo never breaks on a missing key.

## Run

```bash
streamlit run app.py
```

The database (`shiftpilot.db`) is created and seeded automatically on first
load. Use the **🔄 Reset Demo** button in the sidebar at any time to wipe
and reseed it back to the starting scenario.

## Demo scenario

Seeded roster (today, Barista shift 14:00–22:00 held by Sarah Lee):

| Worker      | Role     | Hrs this week | Outcome when covering Sarah's shift          |
|-------------|----------|---------------|-----------------------------------------------|
| Marcus Lim  | Barista  | 30.0          | ❌ Rejected -- fails 11-hr minimum rest        |
| Ravi Kumar  | Barista  | 43.5          | ❌ Rejected -- exceeds 44-hr weekly cap        |
| Chloe Ng    | Cashier  | 20.0          | ❌ Rejected -- role mismatch                   |
| Daniel Tan  | Barista  | 28.0          | ✅ Approved -- 36 hrs total, 17 hrs rest       |

Trigger **📵 Simulate Sick Call (Sarah Lee)**, watch the reasoning trace and
rule audit populate, review/edit Daniel's drafted message, click **Approve &
Dispatch**, then **Simulate Worker Reply: ACCEPT** -- the roster re-renders
with the shift covered by Daniel.

## Command-line sanity checks

```bash
python3 rules.py   # asserts the MOM rule outcomes above
python3 db.py       # rebuilds shiftpilot.db, prints row counts
python3 agent.py    # runs the graph once end-to-end, prints the trace
```

## Bounded agent loops

`ShiftPilotState` carries `iterations` / `max_iterations`. `draft_message_node`
retries a failing LLM call up to that cap before falling back to the
template -- a flaky provider can't burn unbounded tokens or time inside a
single node.
