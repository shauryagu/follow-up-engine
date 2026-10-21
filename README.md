# Follow-up engine

Morning queue for open HVAC quotes: rank who to contact, draft a text, send only after a person confirms. Sends write to a shop outbox.

## How to run

Python 3.10+. Drafts call Baseten (`deepseek-ai/DeepSeek-V4.1-Flash-1017`). You need a billed account and an API key before Generate will work.

### 1. Baseten API key

1. Go to [baseten.co](https://www.baseten.co) and log in (or create an account).
2. Set up payment / billing. Inference will 403 until that is in place.
3. Open the model **DeepSeek V4.1 Flash 1017** (`deepseek-ai/DeepSeek-V4.1-Flash-1017`) and confirm it is available on your account.
4. Generate an API key.
5. In the repo root, copy `.env.example` to `.env` and paste the key:

```bash
cp .env.example .env
```

```
BASETEN_API_KEY=paste-the-key-here
BASETEN_BASE_URL=https://inference.baseten.co/v1
```

`.env` is gitignored. Do not commit the key.

### 2. Install and start

```bash
cd robby_seed
python3 -m venv .venv
source .venv/bin/activate
pip install openai python-dotenv fastapi jinja2 uvicorn httpx
PYTHONPATH=src uvicorn robby.app:app --reload --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). First boot ingest `quotes.json` and `events.jsonl` into `data/robby.sqlite`.

To replay a frozen clock (the seed data is mid-August 2026): `?now=2026-08-16T16:00:00Z`.

## Follow-up policy

A tech’s morning is a scroll of open quotes and a text to whoever they remember. The engine ranks **value at risk**: the open ticket that would hurt most to miss sits at the top. Accepted and dismissed quotes never appear.

Engagement falls into three buckets, which set urgency:

- **They replied, we have not** — same-day work.
- **They viewed in the last 48 hours** — still warm; more views in that window nudge the quote up.
- **Quiet open** — sitting with no recent reply or view.

Ticket size and time sitting then stretch that urgency. Amount uses a log so a $12,500 job outranks a $400 job without wiping the rest of the book. Each week without contact raises the quote; after seven quiet days that factor has doubled. The three buckets are labels on **one** list, so a large quiet quote can still beat a tiny recent view.

After you send, the list is recomputed. That phone goes on a 48-hour cooldown (you can still draft; you cannot send again). The next person to contact is whoever remains with the most to lose — not whoever was #2 a minute ago, because a send on a shared phone can cool several quotes at once, and a reply or view that arrives in the meantime can jump someone else. The reason and body on an in-flight draft stay frozen at generate time; send-time checks still refuse accepted, dismissed, and cooled phones.

Those constants are an opinion for residential HVAC: unanswered replies are same-day, daily SMS feels like spam, a week of silence is already slow. They are meant to produce that ordering, not to fit this seed.

## Fifty shops under one parent

Functionally the tenant is the shop, and the actor is a user. Each location keeps its own quote book, phones, and follow-up knobs (cooldown, how hot a view is, how fast silence climbs). A tech’s morning list is that shop’s open book; a parent rollup is coverage and dollars sitting, never one mega-queue. The same homeowner at two franchises is two customers until both shops agree to link them. Every view of a quote in the workspace, every draft, edit, and send carries a `user_id`, so “who followed up” is a person, not a shop. That identity also scopes access: a tech sees their jobs, a shop manager sees the location, the parent sees aggregates. Cooldown and sibling open quotes stay shop-plus-phone, so one send still cools every open ticket for that customer at that shop, and two techs cannot both text them inside the window.

The system behind that has to leave a single SQLite file and a full recompute on every page load. Fifty shops and concurrent users want a server database (Postgres) with `shop_id` and `user_id` on quotes, events, messages, and outbox, indexed for “open quotes in this shop” and “last send for this shop+phone.” Ranking should run on a small derived row per open quote (last view, last reply, last outbound, view counts) updated when events land, so the list does not rescan the event log. Send stays an outbox write with an idempotency key, inside a transaction that locks the phone’s cooldown row so two app servers cannot double-send. In-flight draft reason and body stay frozen; send-time checks still read committed shop state. Closed quotes drop out of the hot set; events partition or age out of the ranking path. Several app instances, no in-process cache as source of truth, and a real clock instead of `?now=`.

## Another day

The seed is already messy on purpose: duplicate `event_id`s, out-of-order timestamps, extra keys, a quote book that can disagree with the log. I would freeze `now` on that file and assert the order we care about (Ray first, Karen’s two opens on one phone, events after `now` ignored). Phone numbers would go through one canonicalizer before ingest and before cooldown lookup, so `+1 (917) 555-2016` and `19175552016` are the same customer. Unknown event types would still land in `events.payload`; ranking would keep reading only the types we have named. When `quotes.last_contact_at` and the log disagree, the log wins — `recommend()` already unions outbound from events and sent messages; a test would pin that.

Quiet hours would sit next to the 48-hour phone cooldown: a filter on `sendable`, not a term in the score. `recommend()` already returns every open quote with a flag; shop-local 8am–7pm would clear that flag when `now` is outside the window, and `confirm_send` would re-read open status, cooldown, quiet hours, and the outbox idempotency key in one transaction so a second tab cannot sneak through. Before the outbox insert, a cheap scan of the body would refuse a last name or phone the model leaked, and a character cap would keep the text in SMS range.

Generate always inserts today, which is why a quote collects leftover drafts. I would keep that insert (each generate is a new revision trail) and mark any earlier `draft`/`failed` row for that quote `cancelled`, so the compose target is a single live row and history stays queryable. An ingest or status upsert that flips a quote to accepted or dismissed would cancel open drafts on that quote in the same commit. A verified send already cools the phone; sibling drafts on that number would flip `sendable` off until the window ends, which is a list/read change, not a new table. Failed completions already skip the insert.

`now` stays a query parameter so overnight, cooldown, and out-of-order cases are demonstrable. Human edits stay on `message_revisions` until someone marks a diff as the kind of change to repeat.

## Process and judgments

I started from the seed and the send rules, not the page. The take-home scores messy input, guardrails, and quote/message state, so ingest, ranking, and draft → confirm → outbox had to be true before anything rendered. I wrote those calls down with a finality tag so ranking did not get reopened while send was being wired. Tests cover the failures a UI would hide: duplicate events, a second enqueue of the same send, cooldown, a crash before the outbox read-back.

### Policy

Follow-up is a morning of ~60 opens and a text to whoever is remembered. I ranked **value at risk**: the open ticket that would hurt most to miss. Size, urgency, and time sitting all matter, and they multiply so a large quiet job can still beat a tiny recent view.

I kept must-not-send out of that score. Accepted, dismissed, and a phone already texted are refusals. Folding them into rank still lets a send through at a lower slot. After a send, the list is recomputed, because that phone is dark and every open quote on it should move. An in-flight draft keeps the reason and body from generate time, so the text a person approved is the text that goes out; send still re-checks the refusals.

I dropped a quota of small sends before a large one. That only works if the book is a barbell of many small tickets and a few large ones. HVAC shops do not promise that mix. The three reasons — they replied, they viewed, it is quiet — are labels on one list. Splitting the queue into lanes would bury a $12k hot view behind a $400 stale quote.

The customer is a phone. One person can have several open quotes; one text is enough. Cooldown and the draft both key on that number so a send on Karen’s Q-1003 cools Q-1025 with her.

The constants are an HVAC opinion: unanswered replies are same-day work, a view is warm for 48 hours, daily SMS feels like spam, a week of silence is already slow. I defended them with intended orderings (a neglected $20k quiet beat a same-day $400 view; invert the amounts and the order flips), not a fit to this seed. `now` is an argument so August 2026 is replayable and a wall clock cannot make a ranking test flaky.

### Schema

The quote book is current status. The event log is everything that happened, including duplicates and out-of-order timestamps. Ranking at a given `now` reads both and ignores anything after that clock. I stored events append-only with a unique `event_id` because the file repeats ids on purpose; extra keys go in payload so an unknown type cannot break ingest. Quotes upsert by id because the book is a snapshot, not a history.

I did not store the ranked list. A recommendation run goes stale the moment an event lands or a send cools a phone. `recommend()` is a function of now.

A follow-up attempt is a `messages` row: one phone, a primary quote, a frozen reason, a body, and a status that is only `draft`, `failed`, `sent`, or `cancelled`. I never persist `sending`. A crash mid-send leaves the row as draft; the outbox key is what proves the work happened. `message_quotes` ties that one SMS to every open quote on the phone at generate time, so the body can name them and a send can cool them together. `message_revisions` is the audit trail (engine v1, then human edits) and the source of `approved_as_is` versus `approved_with_edits`. It is not a training set.

The outbox is its own table with a unique idempotency key. “In the outbox” is a query. Retry and double-click reuse the same key, so a second insert is a no-op and a read-back of that row is what the UI may call Sent.

### Privacy

The model only needs enough to write a short text. The prompt gets this customer’s first name, the tech’s name, the reason, and open quote ids and amounts. It does not get a last name, a phone, an address, other customers, or a raw event dump. Sibling quotes that are already accepted stay out. Failed completions write nothing, so a truncated or erroring call cannot leave PII in a draft row.

Human edits stay on `message_revisions`. Feeding every rewrite back into the model would send customer copy to the provider a second time and treat drive-by changes as a signal. Stance — they are waiting on us, we are checking back, or this is first touch — is the copy split I would defend without looking at diffs.

The API key lives in a gitignored `.env`. Tests delete it so pytest never hits the network. Provider error text shown in the UI is redacted of credentials.

### Send path

Confirm is a person gate on send, discard, and leave-with-draft, not a status an attempt can get stuck in. Creating a draft is intent. Sent means the outbox row is there. Generate stays allowed on cooldown so a tech can still prepare copy; Send does not. The workspace is derived from quotes, events, and messages. Navigation is not stored.
