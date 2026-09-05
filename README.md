# ShiftPilot

Agentic schedule-repair assistant for a 5-outlet Singapore café/retail
chain. When a worker calls in sick, ShiftPilot searches their outlet
first (then every other outlet if nobody local qualifies), runs every
candidate through Singapore MOM Part IV rules (44-hr weekly cap, 11-hr
minimum rest, role match), picks a pay premium within a manager-set
policy band, drafts a personalized outreach message, and halts for
manager sign-off before anything touches the schedule.

## Architecture

| File          | Responsibility                                                              |
|---------------|--------------------------------------------------------------------------------|
| `db.py`       | SQLite schema, seed data, all reads/writes. No business logic.                |
| `rules.py`    | Pure-Python: MOM compliance checks, ranking, distance, pay-band math. No LLM, no I/O. |
| `agent.py`    | LangGraph state machine: inspect → evaluate → draft → approval gate.          |
| `app.py`      | Streamlit console: roster, disruption trigger, HITL approval, team directory, cost dashboard. |

**Hard boundary:** the LLM (in `agent.py`'s `draft_message_node`) never
computes hours, rest gaps, distance, or the legal pay range -- that
arithmetic lives exclusively in `rules.py`. What the LLM *does* do:
picks a specific rate inside the deterministic band `rules.compute_pay_band()`
returns, and writes a plain-English justification + the outreach message,
grounded in real numbers (distance, notice, the candidate's actual accept/
decline history) it's handed, not numbers it invents. Its chosen rate is
re-clamped into the band in Python before it's ever shown to a manager --
the LLM proposes, deterministic code enforces.

The LangGraph run always stops at `human_approval_gate` (`END` right after).
Nothing is written back to `shiftpilot.db` until a manager clicks **Approve
& dispatch** *and* the simulated worker reply is **Accept** -- both handled
in `app.py`, never inside the graph itself.

## Setup

**Windows, don't want to touch a terminal:** double-click `start_app.bat`.
It installs dependencies and launches the app. Re-run it any time (it's
idempotent) -- that's the one file to remember.

**Everything else (or if you'd rather use a terminal):**

```bash
python3 -m pip install -r requirements.txt   # Windows: use `python` instead of `python3`
python3 -m streamlit run app.py               # `-m` avoids Windows PATH issues with the bare `streamlit` command
```

The app opens at `http://localhost:8501`. The database (`shiftpilot.db`) is
created and seeded automatically on first load. Use **Reset demo** in the
sidebar at any time to wipe and reseed it back to the starting scenario.

Optional -- for live LLM reasoning (rate + justification + message)
instead of the deterministic fallback, create a `.env` file in the repo
root with one of:

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GROQ_API_KEY=gsk-...
```

and install the matching optional package from `requirements.txt` (e.g.
`pip install langchain-anthropic`). No key set → ShiftPilot runs fine using
a deterministic-but-still-real-data-grounded reasoning path, so the demo
never breaks on a missing key.

**Using AWS event/hackathon credits instead (Amazon Bedrock):**

```
pip install langchain-aws
```

```
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...       # only if your credentials are temporary (most hackathon accounts are)
AWS_REGION=us-east-1        # Bedrock isn't available in every region
```

**Verified working config for an IGNITE Hackathon 2026 Innovation Sandbox
account** (SCPs on these accounts commonly deny the console's "Model
access" page and the ap-southeast-* regions entirely -- don't waste
time chasing that page, it's also been deprecated by AWS itself):

```
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0
```

confirmed against a real sandbox account with a live `InvokeModel` call
via both raw boto3 and the app's actual `ChatBedrock` integration --
`global.` and every `ap-southeast-*` region/profile combination tried
failed with `AccessDeniedException` or `ValidationException`, `us.` +
`us-east-1` succeeded immediately. Other sandbox accounts may differ --
if this default doesn't work for yours, that's an empirical question,
not a guess: run a real `invoke_model` call with a couple of candidate
regions/model IDs and see which one actually returns a response, the
same way this was diagnosed. Don't rely on the "Model access" console
page either way -- it's deprecated and its errors are about *listing*
models, not invoking them.

**If your AWS access is a shared, hard-capped sandbox account** (e.g.
a hackathon's one-time AWS credit, revoked at a spend ceiling with no
renewal): the default model is Claude **Haiku 4.5**, deliberately --
it's the cheapest current-generation Claude on Bedrock. Avoid pointing
`BEDROCK_MODEL_ID` at legacy **Claude 3.5 Sonnet**: it moved to "Public
Extended Access" pricing in Dec 2025 and now costs roughly double its
original rate for the same model.

ShiftPilot also tracks estimated LLM spend itself and hard-stops live
LLM calls once it crosses a ceiling you set (**Cost dashboard → LLM API
budget guard**, default $3) -- falling back to deterministic reasoning
automatically rather than trusting anyone to watch a bill in real time.
This is a rough local estimate based on token counts, not a real-time
read of your provider's bill -- treat it as a safety net, not a
replacement for checking the actual AWS Billing console.

## The console (3 tabs)

- **Console** -- today's board across all 5 outlets, the disruption
  trigger, live reasoning trace, rule audit (with per-candidate distance),
  the pay-negotiation approval card, and **Manage coverage** to cancel a
  covered shift and re-open it.
- **Team directory** -- every worker, filterable by outlet: base rate,
  hours this week, ad-hoc reliability (accepted/offered, from real history).
- **Cost dashboard** -- the manager-set pay-premium policy cap, total
  ad-hoc spend, premium paid over base, spend by outlet, a map of the 5
  outlets, and the full offer history log.

## Demo scenarios

Two disruptions are seeded to resolve differently every run:

**1. Sarah Lee (Tanjong Pagar) -- resolves locally.**

| Worker      | Role     | Hrs this week | Outcome                                  |
|-------------|----------|---------------|-------------------------------------------|
| Marcus Lim  | Barista  | 30.0          | ❌ Rejected -- fails 11-hr minimum rest    |
| Ravi Kumar  | Barista  | 43.5          | ❌ Rejected -- exceeds 44-hr weekly cap    |
| Chloe Ng    | Cashier  | 20.0          | ❌ Rejected -- role mismatch               |
| Daniel Tan  | Barista  | 28.0          | ✅ Approved -- same outlet, ~3 km, 3/3 prior accepts |

**2. Zhi Hao Lee (Woodlands) -- forces a cross-outlet search.** Woodlands'
other staff are all Cashiers, so every local candidate fails on role.
The search expands to all 4 other outlets and picks the least-loaded
compliant Barista, wherever they are -- watch the pay offer climb toward
the policy cap to reflect the longer commute.

Both shift times are seeded a few hours from whenever you reset the demo
(not a fixed clock time), so the "notice_hours" driving the pay premium
is always a real, non-trivial number.

## Command-line sanity checks

```bash
python3 rules.py   # asserts the MOM rule outcomes + pay-band math
python3 db.py       # rebuilds shiftpilot.db, prints row counts
python3 agent.py    # runs the Sarah Lee scenario once end-to-end, prints the trace
```

## Bounded agent loops

`ShiftPilotState` carries `iterations` / `max_iterations`. `draft_message_node`
retries a failing LLM call up to that cap before falling back to the
deterministic reasoning path -- a flaky provider can't burn unbounded
tokens or time inside a single node.
