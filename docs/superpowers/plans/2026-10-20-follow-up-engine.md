# Follow-up engine

MVP: ingest messy quote events, rank who to contact at a given `now`, draft a follow-up that names every open quote on that phone, send only after a human confirms. **`sent` means an outbox read-back hit.**

**Stack:** Python 3, stdlib `sqlite3`, `pytest`. Thin localhost UI after the core is testable. Seed JSON is the loader only. No HITL platform. No Docker. No committed `.sqlite`.

**Spec:** assignment PDF at repo root; living brief `.grok/skills/robby-context/SKILL.md`. Seed: `quotes.json`, `events.jsonl`.

## Global Constraints

- Time budget: 3 hours; smaller and thoughtful beats large and sloppy. README must say where we stopped.
- Evaluated: messy-input correctness, guardrails, quote/message state, README judgment.
- Ungraded: UI polish, deploy, auth, framework, containerization.
- Guardrails: no second send to the same customer inside a 48h cooldown; never message accepted/dismissed; re-run/retry never double-sends.
- Policy `now` is a parameter (default: max event timestamp).
- Seed JSON is the loader. Runtime source of truth is SQLite (`data/robby.sqlite`; in-memory in tests). Do not commit the db file.
- Schema applied in code (`CREATE TABLE IF NOT EXISTS`). Migrations/versioning are README-only for later.
- Persist message status only as `draft` | `failed` | `sent` | `cancelled`. Never persist `sending`.
- Generate draft always INSERTs a new row. Cooldown blocks send only.
- One verified send cools all open quotes on that `customer_phone`. Draft body lists all open quotes on that phone.
- TDD for Tasks 1–4. Tests for risky behavior, not 100% coverage.
- Implementer commits on `feature/follow-up-engine`. Do not push.

## Task graph

```
Task 1 schema+ingest
        ├── Task 2 policy          [needs quotes+events+connect]
        │         └── (list data for Task 5)
        ├── Task 3 outbox+attempts [needs full schema from T1]
        │         └── Task 4 drafts [needs generate_draft / revisions API]
        └── Task 5 UI              [needs T2 + T3 + T4]
                    └── Task 6 README [after we know what exists]
```

**Serial (this SDD run):** one implementer at a time on this branch.

**Could be parallel after Task 1 (separate worktrees only):** Task 2 and Task 3 touch different files (`policy.py` vs `outbox.py`/`attempts.py`) and only share `db.connect`. Do not parallelize them in this session. Task 4 waits on Task 3. Task 5 waits on 2+3+4. Task 6 is last.

**Depth gates:** explode a task into signatures, fixtures, and TDD steps in its brief **immediately before dispatch**, not all up front. Task 1 is exploded below. Tasks 2–6 stay coarse until that gate.

## Policy knobs (HVAC)

| Knob | Value | Rationale |
|---|---|---|
| `cooldown_hours` | 48 | Next outbound two mornings later. Daily SMS is spam; a week is too slow while they collect quotes. |
| `hot_view_hours` | 48 | Opened in the last two days = shopping. |
| `urgency_reply` | 1.0 | Unanswered reply. Same-day response is the job. |
| `urgency_hot_view` | 0.6 | Recent view, scaled by `log1p(view_count)`. |
| `urgency_quiet` | 0.25 | Open, no hot signal. Amount × neglect still ranks. |
| `neglect_days` | 7 | `age_factor = 1 + days_since_contact / 7`. |
| Default `now` | max event timestamp | Seed ends 2026-08-16. |

`score = log1p(amount) × urgency × (1 + days_since_contact / 7)`

Reason label: `unanswered_reply` | `hot_view` | `quiet_open`.

Last contact: `max(quote.last_contact_at, last message_sent event, last verified send)` clipped to `now`; else `created_at`.

Cooldown does **not** drop a quote from `recommend()`; it sets `sendable=False`.

## Schema (Task 1 applies all tables)

See Task 1. Tables: `quotes`, `events`, `messages`, `message_quotes`, `message_revisions`, `outbox`.

## File map

```
src/robby/
  db.py         connect, apply schema, in-memory helper
  ingest.py     upsert quotes, insert-or-ignore events
  policy.py     Task 2
  outbox.py     Task 3
  attempts.py   Task 3
  draft.py      Task 4
  app.py        Task 5
tests/
  test_ingest.py    Task 1
  test_policy.py    Task 2
  test_attempts.py  Task 3
  test_outbox.py    Task 3
README.md           Task 6
pyproject.toml      Task 1 (pytest + src layout)
.gitignore          data/, *.sqlite, .venv, __pycache__
```

---

## Task 1: Schema + ingest

**Where this fits:** Substrate. Every later task opens a connection from `db.py` and assumes these tables exist. Policy only reads `quotes`/`events`. Attempts write `messages`/`outbox`. The UI opens the same file and ingest is idempotent on boot.

**Delivery:** Do not commit a sqlite file. `connect(path)` creates parent dirs, applies schema, returns a connection with `row_factory=sqlite3.Row` and foreign keys on. Tests call `connect(":memory:")`. App later uses `data/robby.sqlite` (gitignored).

**Files:** `src/robby/__init__.py`, `src/robby/db.py`, `src/robby/ingest.py`, `tests/test_ingest.py`, `pyproject.toml`

**Public API (use these names):**

```python
# db.py
SCHEMA: str  # all CREATE TABLE / INDEX from the plan schema
def connect(path: str = ":memory:") -> sqlite3.Connection: ...
def apply_schema(conn: sqlite3.Connection) -> None: ...

# ingest.py
def ingest_quotes(conn, quotes) -> None:  # iterable[dict], upsert by id
def ingest_events(conn, events) -> None:  # iterable[dict], INSERT OR IGNORE event_id
def ingest_seed(conn, quotes_path: str, events_path: str) -> None:
```

Event rows store extra fields (`channel`, `direction`, …) as JSON in `payload`. Core columns: `event_id`, `quote_id`, `type`, `timestamp`.

**Schema SQL (verbatim):**

```sql
CREATE TABLE IF NOT EXISTS quotes (
  id TEXT PRIMARY KEY,
  customer_name TEXT NOT NULL,
  customer_phone TEXT NOT NULL,
  tech_name TEXT NOT NULL,
  amount REAL NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','accepted','dismissed')),
  created_at TEXT NOT NULL,
  last_contact_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  quote_id TEXT NOT NULL,
  type TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_quote_ts ON events(quote_id, timestamp);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  customer_phone TEXT NOT NULL,
  primary_quote_id TEXT NOT NULL,
  body TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('draft','failed','sent','cancelled')),
  idempotency_key TEXT NOT NULL UNIQUE,
  outbox_id TEXT,
  decision TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_phone_status ON messages(customer_phone, status);

CREATE TABLE IF NOT EXISTS message_quotes (
  message_id TEXT NOT NULL,
  quote_id TEXT NOT NULL,
  PRIMARY KEY (message_id, quote_id)
);

CREATE TABLE IF NOT EXISTS message_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  body TEXT NOT NULL,
  author TEXT NOT NULL CHECK (author IN ('engine','human')),
  created_at TEXT NOT NULL,
  UNIQUE (message_id, version)
);

CREATE TABLE IF NOT EXISTS outbox (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

**Tests (TDD: write these first, watch fail, then implement):**

1. `ingest_seed` on the real `quotes.json` + `events.jsonl` yields 30 quotes and 82 events.
2. Running `ingest_seed` twice leaves 30 quotes and 82 events (idempotent).
3. Duplicate `event_id` in an input list inserts one row.
4. Events with extra keys land those keys in `payload` JSON; `type` can be something other than the known set.
5. Inserting events in reverse timestamp order, `SELECT ... WHERE quote_id=? AND timestamp<=? ORDER BY timestamp` still returns chronological rows.
6. `connect(":memory:")` has all six tables.

**Out of scope for Task 1:** policy, attempts, HTTP, README prose, Docker, writing any rows to `messages`/`outbox`.

**Done when:** `pytest tests/test_ingest.py` passes, output pristine; `pyproject.toml` makes `pytest` collect `tests/` with `src` on the path; work committed.

---

## Task 2: Policy

**Deepen before dispatch:** exact `Candidate` dataclass, fold queries, four HVAC contract fixtures with concrete amounts/timestamps, `sendable` via `messages.sent_at` (table exists, may be empty).

**Depends on:** Task 1. **Unblocks:** Task 5 list. **Parallel with:** Task 3 (not in this session).

Create `policy.py`. `recommend(conn, now) -> list[Candidate]` with `quote_id`, `customer_phone`, `score`, `reason`, `amount`, `sendable`. Knobs in one module. Tests: four contract fixtures; cooldown sets `sendable=False` but quote still appears; events after `now` ignored.

---

## Task 3: Outbox + attempts

**Deepen before dispatch:** exception types, `confirmed: bool` on send/discard, crash test (outbox row + message still `draft`), phone-level cooldown using seed pair Q-1003/Q-1025 (Karen Nguyen, +19175552003).

**Depends on:** Task 1. **Unblocks:** Task 4, Task 5 compose. **Parallel with:** Task 2 (not in this session).

Send path: confirm → guards → outbox lookup-or-enqueue → read-back → `sent` or `failed`. Never write `sending`. Generate draft always INSERT.

---

## Task 4: Draft template + revisions

**Deepen before dispatch:** exact template string, first-name split, sibling listing format.

**Depends on:** Task 3 (`generate_draft` / revisions). **Unblocks:** Task 5 compose body.

v1 lists every open quote on the phone. Accepted sibling omitted. Two edits → three revisions. Send copies final body into outbox payload.

---

## Task 5: Localhost workspace

**Deepen before dispatch:** FastAPI vs stdlib http.server, route list, confirm modal behavior. After Tasks 2–4 exist.

**Depends on:** Tasks 2, 3, 4.

List, quote detail, drafts for that phone, generate, compose, confirms, post-send Back | Generate another.

---

## Task 6: README

**Deepen before dispatch:** fill “where we stopped” from the branch.

How to run; HVAC knobs and why; ~2 paragraphs no code on 50 shops; another day (LLM draft, learn from revisions, Postgres, schema_version migrations, containers); where we stopped.
