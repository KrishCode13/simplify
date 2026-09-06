# ShiftPilot

**An agentic schedule-repair assistant for multi-outlet retail/F&B chains.**
When a worker calls in sick, ShiftPilot finds a compliant, fairly-paid
replacement — checking the law, the budget, and the map before it ever
asks a human to approve anything.

Built for the SimplifyNext Agentic AI Hackathon.

---

## 1. The problem, in one paragraph

A café chain manager's worst 6am text is "I can't come in today." What
happens next is normally a frantic group chat: guessing who's free,
forgetting who's already near overtime, forgetting who legally needs
rest, and eventually over-paying whoever answers first because there's
no time to think it through. ShiftPilot replaces that scramble with an
agent that does the checking instantly and asks a human to approve the
result — not to do the legwork.

## 2. The 60-second demo

1. Open the **Console** tab. Today's board is live across 5 Singapore
   outlets (Orchard, Tampines, Jurong East, Woodlands, Tanjong Pagar).
2. Click **Simulate sick call — Sarah Lee** (or pick any scheduled
   shift from the dropdown).
3. Watch the **reasoning trace** populate live: every candidate at that
   outlet gets checked against Singapore MOM Part IV rules (44-hr
   weekly cap, 11-hr rest, role match) — with the exact reason each one
   was accepted or rejected.
4. If nobody local qualifies, watch the search **expand to every other
   outlet automatically** — real straight-line distance computed for
   each candidate, not guessed.
5. The **approval card** shows a specific pay offer above the worker's
   base rate, with a plain-English justification citing their real
   distance, their real accept/decline history, and how last-minute the
   request is — then a drafted WhatsApp message.
6. Click **Approve & dispatch**, then **Simulate reply: Accept** — the
   roster updates live, nothing was written to the database before this
   click.

Try it twice: **Sarah Lee** resolves locally. **Zhi Hao Lee**
(Woodlands) has no local Barista available, so watch it search outward
and price the offer up to reflect the longer commute.

## 3. What actually makes this "agentic," not just a form

This is the part worth saying out loud to a judge: **every decision
with a legally or financially correct answer is deterministic Python,
not the LLM.** The LLM is used exactly once per disruption, for the one
decision that's genuinely a judgment call.

| Decision | Who makes it | Why |
|---|---|---|
| Is this candidate legally allowed to take the shift? | **Deterministic** (`rules.py`) | MOM hour/rest/role rules aren't negotiable — an LLM must never do scheduling arithmetic |
| How far away do they live? | **Deterministic** (`rules.py`, haversine formula) | Real geometry, not a guess |
| What's the legal/policy pay range for this offer? | **Deterministic** (`rules.py`, scales with urgency + distance, capped by a manager-set policy multiplier) | The LLM is never allowed to invent a number outside this |
| Which specific number to offer, and how to phrase the ask | **The LLM** — one call, `agent.py`'s `draft_message_node` | This is the one genuinely subjective call: reading real context (distance, reliability, urgency) and producing a judgment, not arithmetic |
| Does this shift actually get reassigned? | **A human** (Approve & Dispatch, then a simulated worker reply) | Human-in-the-loop is not optional — nothing is written to the database until both of these happen |

The LLM's output is never trusted blindly: its proposed rate is
re-clamped into the legal band in Python regardless of what it returns,
and if it's unreachable or returns something unparseable, a
deterministic fallback — grounded in the same real numbers — keeps the
demo running without a human noticing a difference in kind, only in
polish.

## 4. Feature tour

**Console**
Live roster across all 5 outlets · disruption trigger · real-time agent
reasoning trace, colour-coded by step · full rule audit (every
candidate considered, not just the winner, with their computed
distance) · the pay-negotiation approval card · cancel a covered shift
and re-open it for a new search.

**Team directory**
Every worker, filterable by outlet, with their base rate, hours this
week, and **ad-hoc reliability** — a real accepted/offered ratio pulled
from an actual history table, never an invented claim.

**Cost dashboard**
Manager-editable pay-premium policy cap · total ad-hoc spend and
premium-paid-over-base · spend by outlet · a live map of all 5 outlets
· the full historical offer log · and an **LLM API budget guard** that
tracks real estimated spend on live LLM calls and automatically stops
making them (falling back to the deterministic path) once a
manager-set ceiling is crossed — a real circuit breaker, not a
dashboard number nobody watches.

**Two demo scenarios, deliberately different shapes**
Sarah Lee (Tanjong Pagar) resolves from the local staff pool. Zhi Hao
Lee (Woodlands) has no local Barista at all, forcing the cross-outlet
search — both shift times float relative to when you reset the demo,
so the urgency math is always a real, non-trivial number.

## 5. Architecture

| File | Responsibility |
|---|---|
| `db.py` | SQLite schema, seed data, all reads/writes. No business logic. |
| `rules.py` | Pure-Python: MOM compliance, candidate ranking, distance, pay-band math. No LLM, no I/O. |
| `agent.py` | The LangGraph state machine. |
| `app.py` | Streamlit console: Console / Team directory / Cost dashboard. |

**The graph** (`agent.py`, `build_graph()`):

```
START → investigate_disruption → evaluate_candidates → draft_message → human_approval_gate → END
```

- `investigate_disruption` — logs the disruption. Orchestration only.
- `evaluate_candidates` — searches the local outlet first, then every
  outlet if nobody local qualifies; runs every candidate through
  `rules.check_worker_compliance`; computes real distance for each.
  **Deterministic.**
- `draft_message` — the only node that calls an LLM. Given the
  selected candidate's real distance, reliability history, and a
  deterministic pay band, it picks a rate inside that band, justifies
  it, and drafts the outreach message — one structured response,
  regex-parsed, rate re-clamped afterward regardless of what came back.
  Retries a failing call up to a bounded cap before falling back to a
  deterministic (but still real-data-grounded) version of the same
  reasoning — this is the bounded-agent-loop guard: a flaky provider
  can't burn unlimited tokens or time.
- `human_approval_gate` — halts the graph. `END` immediately after.
  Nothing is written to `shiftpilot.db` from inside the graph, ever —
  only `app.py`, and only after a human clicks Approve *and* a
  simulated worker Accept.

**Why this satisfies "must use LangGraph" for real, not nominally:**
`agent.py` builds an actual `StateGraph`, registers real nodes, wires
typed edges, and `.compile()`/`.invoke()`s it — this is genuine
orchestration, not four functions called in sequence with a label on
top.

## 6. Setup

### Quick start (just cloned this repo)

```bash
git clone https://github.com/KrishCode13/simplify.git
cd simplify
python3 -m pip install -r requirements.txt   # Windows: `python` instead of `python3`
python3 -m streamlit run app.py               # -m avoids Windows PATH issues
```
Opens at `http://localhost:8501`, seeding its own database on first
load — no setup, no API key needed to try it. **Reset demo** (sidebar)
wipes and reseeds at any time. See *Enabling live LLM reasoning* below
to turn on real LLM output instead of the deterministic fallback.

Windows, prefer not to type commands: double-click `start_app.bat`
instead of the last two lines above. Installs dependencies, launches
the app, safe to re-run.

### Enabling live LLM reasoning (optional)

Not connected → the app runs the deterministic-but-real-data-grounded
reasoning path, so this is never required to run or demo the app.

There's exactly one LLM path — **Claude Haiku 4.5 via AWS Bedrock**,
funded by this project's AWS hackathon credit. No provider picker, no
model choice: `langchain-aws` is a core dependency (already installed
by `pip install -r requirements.txt` above), so there's no extra
install step either.

**To connect it:** sidebar → **Connect Claude** → paste your AWS
Access Key ID + Secret Access Key (+ Session Token, if your credentials
are temporary — most hackathon/event credits are) → **Save & connect**.
Takes effect immediately, no restart — it writes to a local `.env`
(created for you, never committed to git) and updates the running app
in the same click.

**Manual alternative** — create `.env` in the repo root yourself:
```
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...          # required for temporary/event credentials
AWS_REGION=us-east-1                                          # optional, this is the default
BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0   # optional, this is the default
```
The last two lines are already the app's defaults — only set them if
your account needs something different. They're not a guess: this
exact region/model pair is empirically verified against a real IGNITE
Hackathon 2026 Innovation Sandbox account via a live `InvokeModel`
call — that account's Service Control Policies deny the `global.`
inference profile and every `ap-southeast-*` region outright, and the
Bedrock "Model access" console page is deprecated and unrelated to
whether invoking actually works (its errors are about *listing*
models, not using them). If this default doesn't work for your
account, don't guess again — run one real `invoke_model` call against
a candidate region/model pair and read the actual error.

Haiku 4.5 is the model on purpose, not a placeholder: it's the
cheapest current-generation Claude on Bedrock. Avoid pointing
`BEDROCK_MODEL_ID` at legacy **Claude 3.5 Sonnet** — it moved to
"Public Extended Access" pricing in Dec 2025 and now costs roughly
double its original rate for the same model.

**Cost, concretely:** one full disruption cycle (one LLM call) costs
about **$0.0012** at Haiku 4.5 rates. A $20 shared budget covers
roughly 16,000 of these — LLM cost is not the constraint for this app.
The **Cost dashboard → LLM API budget guard** still tracks and
hard-stops live calls past a ceiling you set (default $3) as a safety
net, since it costs nothing to have one.

## 7. Command-line sanity checks

```bash
python3 rules.py    # asserts MOM rule outcomes + pay-band math
python3 db.py        # rebuilds shiftpilot.db, prints row counts
python3 agent.py     # runs the Sarah Lee scenario once end-to-end, prints the full trace
```

## 8. Known scope boundaries

Out of scope by design, not by oversight: no real Twilio/WhatsApp
delivery (dispatch is simulated in-UI), no multi-store logistics beyond
the 5 seeded outlets, no full monthly roster generation, no
double-booking detection across simultaneous shifts at different
outlets. All named up front so a judge's question has a clean answer
rather than a surprised one.
