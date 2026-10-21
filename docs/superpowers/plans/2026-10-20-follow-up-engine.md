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

**Where this fits:** Pure ranking over Task 1 tables plus `messages.sent_at` from Task 3. Do not edit `attempts.py`, `outbox.py`, or `draft.py`. Import `COOLDOWN_HOURS` from `robby.attempts`.

**Files:** `src/robby/policy.py`, `tests/test_policy.py` only.

**Knobs (in `policy.py`):**

```python
HOT_VIEW_HOURS = 48
URGENCY_REPLY = 1.0
URGENCY_HOT_VIEW = 0.6
URGENCY_QUIET = 0.25
NEGLECT_DAYS = 7
VIEW_COUNT_EPS = 0.02
```

**Score:** `log1p(amount) * urgency * (1 + days_since_contact / NEGLECT_DAYS) * (1 + VIEW_COUNT_EPS * log1p(view_count))`

View-count epsilon keeps 6 views above 1 view without letting a hot view beat an unanswered reply at the same amount/age.

**Reason** (dominant term, not the epsilon): `unanswered_reply` if last `customer_replied` (timestamp <= now) is after last outbound or there is no outbound; else `hot_view` if last `quote_viewed` <= now is within `HOT_VIEW_HOURS` of now **and** that view is after last outbound (or no outbound); else `quiet_open`.

**Last outbound** for a quote: max of `events.type='message_sent'` for that `quote_id` with `timestamp<=now`, and `messages.sent_at` where `status='sent'` and the quote is in `message_quotes` for that message.

**Last contact** for neglect: max of `quotes.last_contact_at`, last outbound, clipped to now; if none, `created_at`. `days_since_contact` = (now − last_contact) in days, float.

**Filters:** only `quotes.status='open'`. Do not drop cooldown phones.

**sendable:** False if that `customer_phone` has a `messages` row `status='sent'` with `sent_at` within `COOLDOWN_HOURS` before `now`. Else True.

**API:**

```python
@dataclass(frozen=True)
class Candidate:
    quote_id: str
    customer_phone: str
    score: float
    reason: str  # unanswered_reply | hot_view | quiet_open
    amount: float
    sendable: bool

def default_now(conn) -> str:  # max(events.timestamp); if no events, raise
def recommend(conn, now: str) -> list[Candidate]:  # sort score desc, then quote_id asc
```

Ignore events with `timestamp > now`.

**Tests (TDD, in-memory, synthetic quotes — do not require the seed file):**

1. Unanswered reply beats same-size hot view: two open quotes amount=5000, created and last_contact `2026-08-01T00:00:00Z`, now=`2026-08-02T00:00:00Z`. A has `customer_replied` at `2026-08-01T20:00:00Z`. B has `quote_viewed` at `2026-08-01T20:00:00Z`. A's score > B's; reasons `unanswered_reply` vs `hot_view`.
2. Two-week neglected $20000 quiet beats same-day $400 hot view: now=`2026-08-15T00:00:00Z`. A amount 20000, created/last_contact `2026-08-01T00:00:00Z`, no events. B amount 400, created `2026-08-15T00:00:00Z`, viewed `2026-08-15T00:00:00Z`. A ranks first.
3. Same-day $20000 hot view beats two-week neglected $400 quiet: invert amounts on fixture 2.
4. View after `now` does not count: quote with only `quote_viewed` at `2026-08-16T00:00:00Z`, now=`2026-08-15T00:00:00Z` → reason `quiet_open`.
5. Cooldown: insert a `messages` sent row for B's phone at `now`; B still appears with `sendable=False`; A `sendable=True` if different phone.
6. `recommend` sorts by score descending.

**Out of scope:** draft.py, HTTP, README, LLM.

**Done when:** `pytest tests/test_policy.py tests/test_ingest.py tests/test_attempts.py` pass; committed.

---

## Task 3: Outbox + attempts

**Where this fits:** Scored guts. Task 1 left empty `messages`/`outbox` tables. This task writes them. Task 4 will replace the placeholder body; keep body construction as a private helper in `attempts.py` (do not add `draft.py`). Task 2 can import nothing from here except that `sent_at` rows will exist.

**Files:** `src/robby/outbox.py`, `src/robby/attempts.py`, `tests/test_outbox.py`, `tests/test_attempts.py`

**Constants:** `COOLDOWN_HOURS = 48` in `attempts.py`. `now` is an ISO-8601 string; parse with `datetime.fromisoformat(now.replace("Z", "+00:00"))`. Do not use wall clock.

**Outbox API (`outbox.py`):**

```python
def enqueue(conn, *, idempotency_key: str, payload: dict) -> None:
    # INSERT OR IGNORE. payload stored as JSON. New rows get uuid4 id and created_at=now from payload? 
    # Use payload["created_at"] if present else the caller passes created_at.
```

Caller passes `created_at` inside send via a `created_at` argument:

```python
def enqueue(conn, *, idempotency_key: str, payload: dict, created_at: str) -> None: ...
def get_by_key(conn, idempotency_key: str) -> dict | None:
    # dict with id, idempotency_key, payload (parsed dict), created_at
```

**Attempts API (`attempts.py`):**

```python
class SendRejected(Exception):
    def __init__(self, code: str):
        self.code = code  # "cooldown" | "not_open" | "illegal_status"

def generate_draft(conn, primary_quote_id: str, now: str, *, reason: str = "quiet_open") -> dict: ...
def edit_draft(conn, message_id: str, body: str, now: str) -> dict: ...
def send_attempt(conn, message_id: str, *, confirmed: bool, now: str,
                 enqueue=None, get_by_key=None) -> dict: ...
def retry_attempt(conn, message_id: str, *, confirmed: bool, now: str,
                  enqueue=None, get_by_key=None) -> dict: ...
def discard_attempt(conn, message_id: str, *, confirmed: bool, now: str) -> dict: ...
```

Returned `dict` keys: `id`, `customer_phone`, `primary_quote_id`, `body`, `reason`, `status`, `idempotency_key`, `outbox_id`, `decision`, `created_at`, `updated_at`, `sent_at`.

`enqueue`/`get_by_key` default to `outbox.enqueue` / `outbox.get_by_key`. Hooks exist so tests can force verify-miss without mocking sqlite.

**generate_draft**
- Always INSERT a new `messages` row (`id` and `idempotency_key` = uuid4). Never resume.
- Primary quote must exist. If its `status != 'open'`, raise `SendRejected("not_open")`.
- Snapshot **all open quotes with the same `customer_phone`** into `message_quotes` (including primary).
- Placeholder body (Task 4 will replace): comma-separated `"{id} (${amount:g})"` for those quotes, primary first, then other ids sorted. Example: `Q-1003 ($850), Q-1025 ($850)`.
- Insert revision version=1, author=`engine`, body=that placeholder.
- `status='draft'`, `decision='pending'`, timestamps=`now`.

**edit_draft**
- Only if `status=='draft'`; else raise `SendRejected("illegal_status")`.
- Update `body`, `updated_at`; append next revision author=`human`.

**discard_attempt**
- `confirmed=False`: return row unchanged (still draft/failed).
- `confirmed=True` and status in `draft`|`failed`: set `cancelled`, `updated_at=now`.
- Other statuses: raise `SendRejected("illegal_status")`.

**send_attempt / retry_attempt**
`retry_attempt` is `send_attempt` allowed from `failed` only. `send_attempt` allowed from `draft` or `failed` (retry can just call send_attempt).

1. `confirmed=False`: return unchanged, no outbox write.
2. Status not `draft` or `failed`: raise `SendRejected("illegal_status")`.
3. Load primary quote. Missing or `status != 'open'`: raise `SendRejected("not_open")`. Message stays `draft`/`failed`.
4. Cooldown: if another row with the same `customer_phone`, `status='sent'`, and `sent_at` within 48 hours **before** `now` (difference < 48h), raise `SendRejected("cooldown")`. This message’s own row does not count (it is not sent).
5. `row = get_by_key(idempotency_key)`. If hit: this is crash recovery — set `status='sent'`, `outbox_id=row["id"]`, `sent_at=now`, `updated_at=now`, `decision` = `approved_as_is` if `body` equals revision v1 else `approved_with_edits`. Never insert a second outbox row.
6. Else `enqueue(...)` with payload JSON `{customer_phone, body, quote_ids, reason, message_id}` (`quote_ids` from `message_quotes`, primary first).
7. `row = get_by_key(...)`. If missing: set `status='failed'`, `updated_at=now`, return. If present: same sent fields as step 5.
8. **Never** write status `sending`.

**Outbox payload `quote_ids`:** list of quote ids from `message_quotes`, primary first.

### Tests (TDD: write first, watch fail, then implement)

**test_outbox.py**
1. enqueue then get_by_key returns the payload.
2. enqueue twice with the same key → one row; get_by_key still works.

**test_attempts.py** (in-memory db; insert quotes with `ingest_quotes`, do not need full seed except where noted)
1. Unconfirmed send does not enqueue and leaves `status='draft'`.
2. Unconfirmed discard leaves `status='draft'`.
3. Verify miss: pass `get_by_key` that always returns None after a real enqueue (or a fake get_by_key) → `status='failed'`; retry with real outbox and same message → `sent` and **one** outbox row.
4. Crash recovery: insert an outbox row for the draft’s `idempotency_key` while message is still `draft`; `send_attempt(confirmed=True)` → `sent`, still one outbox row.
5. Phone cooldown: two open quotes, same phone `+19175552003`, ids `Q-1003` and `Q-1025` (minimal rows: names Karen Nguyen, amount 850, status open, created_at `2026-08-01T00:00:00Z`). Send on a Q-1003 draft at `now=2026-08-10T00:00:00Z` → sent. `generate_draft` on Q-1025 still works. `send_attempt` on that new draft at the same `now` raises `SendRejected("cooldown")`. `message_quotes` on the Q-1025 draft includes both Q-1003 and Q-1025.
6. `generate_draft` twice on the same primary → two `draft` rows (different ids).
7. Primary quote `accepted` or `dismissed`: `generate_draft` and `send_attempt` raise `SendRejected("not_open")`. For send, insert an open quote, generate draft, then UPDATE quote status to accepted, then send.
8. Confirmed discard of a draft → `cancelled`.
9. After send, `decision` is `approved_as_is` if never edited; after `edit_draft`, send → `approved_with_edits`.
10. No row in `messages` ever has `status='sending'` (assert after send/crash paths).

**Out of scope:** `draft.py` templates, `policy.py`, HTTP, README, Docker.

**Done when:** `pytest tests/test_outbox.py tests/test_attempts.py tests/test_ingest.py` all pass; committed on `feature/follow-up-engine`.

---

## Task 4: Draft v1 (template + DeepSeek)

**Where this fits:** Replace `_placeholder_body` in `attempts.generate_draft` with `draft.render_v1`. Do not edit `policy.py`. Existing attempt tests must keep passing (placeholder format may change — **update** `tests/test_attempts.py` only if a test asserted the old comma-separated body string; prefer not to couple those tests to copy).

**Provider (user-mandated, not SpaceXAI):** Baseten OpenAI-compatible API.

- `base_url`: `https://inference.baseten.co/v1`
- `model`: `deepseek-ai/DeepSeek-V4.1-Flash-1017`
- Env: `BASETEN_API_KEY` (required to call the model). Optional `BASETEN_BASE_URL` defaulting to the URL above.
- **Never hardcode an API key.** Never commit `.env`.
- Completions are **non-streaming** (`stream=False`). `max_tokens=400`, `temperature=0.4`.
- If `BASETEN_API_KEY` is unset or the `complete` hook is None, use the template. Tests never hit the network.

**Files:** `src/robby/draft.py`, `tests/test_draft.py`, hook in `src/robby/attempts.py`, `pyproject.toml` (`openai` dependency), `.env.example` (`BASETEN_API_KEY=` and `BASETEN_BASE_URL=https://inference.baseten.co/v1`), README.md section **LLM drafts** (signup/billing/key — no secrets).

**API:**

```python
MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
DEFAULT_BASE_URL = "https://inference.baseten.co/v1"

def first_name(customer_name: str) -> str:  # first token
def template_body(*, customer_name: str, tech_name: str, reason: str, primary_quote_id: str, open_quotes: list[dict]) -> str: ...
def render_v1(*, customer_name: str, tech_name: str, reason: str, primary_quote_id: str, open_quotes: list[dict], now: str, complete=None) -> str: ...
```

`open_quotes` items have at least `id`, `amount`, `status`. Template lists **all** of them (caller already filtered to open), primary first then other ids sorted, format:

```
Hi {first_name}, it's {tech} following up. Open quotes: Q-1003 ($850), Q-1025 ($850). Reason: {reason}.
```

`complete(prompt: str) -> str` if provided is used instead of the HTTP client. `render_v1` uses `complete` when not None; else env client; else template. Empty/whitespace LLM output falls back to template.

**attempts.generate_draft:** build `open_quotes` as now, then `body = render_v1(...)`. Add optional `complete=` passed through. Default `complete=None`.

**README LLM drafts section (this task, not Task 6):**
1. Create a Baseten account and add billing / payment.
2. Create an API key.
3. `export BASETEN_API_KEY=...` (or gitignored `.env`).
4. Model id and base URL as above.
5. Without a key, drafts use the template.

**Tests (TDD, no network):**
1. `template_body` includes every open quote id and amounts, primary first; first name from `Karen Nguyen` is `Karen`.
2. `render_v1` with `complete=None` equals template.
3. `render_v1` with `complete=lambda prompt: "LLM SMS"` returns `LLM SMS`.
4. `complete=lambda prompt: "  "` falls back to template.
5. `generate_draft(..., complete=lambda prompt: "LLM SMS")` stores body `LLM SMS` as revision v1.
6. Accepted sibling is omitted (still generate_draft's snapshot — two open + one accepted on same phone → body/template lists two).

**Out of scope:** policy, HTTP UI, streaming in the UI, real API calls in CI.

**Done when:** `pytest tests/test_draft.py tests/test_attempts.py tests/test_ingest.py` pass; no secrets in git; committed.

---

## Task 5: Localhost workspace

**Where this fits:** Technician critical path on top of T1–T4. No new policy. Every mutating button calls `generate_draft` / `edit_draft` / `send_attempt` / `discard_attempt`. Streaming uses `on_chunk`; save still happens only when `generate_draft` returns.

**Stack:** FastAPI + Jinja2 + a small amount of vanilla JS (SSE + confirm dialogs). `uvicorn`. Localhost. No auth.

**Files:** `src/robby/app.py`, `src/robby/templates/base.html`, `list.html`, `quote.html`, `tests/test_app.py`. Add `fastapi`, `jinja2`, `uvicorn`, `httpx` in `pyproject.toml`.

**Boot:** `connect(ROBBY_DB or data/robby.sqlite)`. If `quotes` is empty, `ingest_seed` from repo-root `quotes.json` / `events.jsonl`. `now` = query `?now=` or `default_now(conn)`.

**Awareness copy (verbatim):**
- Banner on every page: `Sends write to the shop outbox. Nothing goes to the customer's phone in this demo.`
- Reasons: `unanswered_reply` → `They replied — waiting on us`; `hot_view` → `Opened the quote in the last 48 hours`; `quiet_open` → `Open, no recent engagement`.
- Cooldown: `You already followed up this number. You can still draft. Send unlocks after 48 hours.`
- Writer: `Written with AI` if `BASETEN_API_KEY` is set, else `Template (no API key)`.
- After send: `In the outbox` (never “texted”).
- Show labeled `now` on the list.

**Fail-safe:**
- Send / discard require `confirmed=true` (JS confirm, then POST). Unconfirmed is not sent from the UI.
- Leave-with-draft: Keep (default, just go to list) / Discard this draft / Stay.
- Generate disabled while a stream is open. Model error: flash `The model couldn't generate a draft. Wait a moment and try again.` No new row.
- Send disabled when not `sendable` or quote not open. Generate still enabled on cooldown.
- One primary button: Generate or Send or Try again.

**Customizability:** textarea edit; Generate another (new row); checkbox `Use template` (`complete` forced to template_body path — pass a complete hook that ignores the model, or a `use_template=1` flag that calls `render` with empty key). Implement `use_template` query/form: `generate_draft(..., complete=lambda prompt: template_body(...))` using quote fields already loaded.

**Routes:**
- `GET /` list
- `GET /quotes/{quote_id}` workspace (customer, sibling open quotes, thread, drafts for **phone**, compose if `draft_id`)
- `GET /quotes/{quote_id}/draft-stream` SSE: events `chunk` / `done` (message_id) / `error` (retry text). Query `use_template=1` optional.
- `POST /messages/{message_id}/edit` body= textarea, redirect workspace
- `POST /messages/{message_id}/send` confirmed=true, redirect workspace
- `POST /messages/{message_id}/discard` confirmed=true, redirect workspace
- `GET /` is Back

**Thread:** events for the quote with `timestamp<=now` plus `messages` status=`sent` for that phone.

**Tests (TestClient, no network — conftest already strips API key):**
1. GET `/` lists open quotes; accepted/dismissed absent; reason labels appear.
2. GET quote page shows customer name and Generate.
3. POST generate via draft-stream or a test-only POST `/quotes/{id}/drafts` that calls `generate_draft` without SSE — prefer a synchronous `POST /quotes/{id}/drafts` used by tests and as noscript fallback; JS uses SSE. After POST, a `messages` row exists and page shows the body.
4. Send without confirmed does not enqueue; send with confirmed=true creates outbox row; page says `In the outbox`.
5. Cooldown: after send, quote page still has Generate, Send is disabled / cooldown copy shown.
6. Discard confirmed cancels draft.
7. Model error: monkeypatch `generate_draft` to raise `DraftGenerationError`; stream/POST returns the retry sentence; still zero new messages if we count before/after.

**Done when:** `pytest tests/` passes; `uvicorn robby.app:app` serves the path on localhost.

---

---

## Task 6: README

**Deepen before dispatch:** fill “where we stopped” from the branch.

How to run; HVAC knobs and why; ~2 paragraphs no code on 50 shops; another day (LLM draft, learn from revisions, Postgres, schema_version migrations, containers); where we stopped.
