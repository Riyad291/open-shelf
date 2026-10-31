# Architecture

How Open Shelf is put together, and why. The decisions that were genuinely
close are recorded as ADRs ([ADR-001](docs/adr/ADR-001-postgresql-and-web-only.md),
[ADR-002](docs/adr/ADR-002-free-resource-platform.md)); the design for the
deployed site, with capacity estimates, is in
[docs/SYSTEM_DESIGN.md](docs/SYSTEM_DESIGN.md).

---

## The shape

```
                   ┌────────────────────────────┐
                   │  Browser client — web/     │
                   │  vanilla JS, no build step │
                   └─────────────┬──────────────┘
                                 │ fetch() + JWT
                   ┌─────────────▼──────────────┐
                   │  FastAPI — bookstore/api   │
                   │  routers · deps · schemas  │
                   └─────────────┬──────────────┘
                                 │ one Session per request
         ┌───────────────────────▼────────────────────────┐
         │  Services — bookstore/services                 │
         │  THE RULES LIVE HERE AND NOWHERE ELSE          │
         └───────────────────────┬────────────────────────┘
                                 │ SQLAlchemy 2
         ┌───────────────────────▼────────────────────────┐
         │  PostgreSQL — 14 tables, Alembic migrations    │
         └───────────────────────▲────────────────────────┘
                                 │ upsert
         ┌───────────────────────┴────────────────────────┐
         │  Importer — bookstore/ingest                   │
         │  parse a catalogue, write it idempotently      │
         └────────────────────────────────────────────────┘
```

One rule, one place. The API layer validates input, decides who is calling
and serialises the answer; it does not know that a trusted contributor's
rating counts double. The browser client renders; it does not know either.

---

## The layers

### `bookstore/db` — schema and session

`models.py` is the schema: fourteen tables with foreign keys, check
constraints and indexes. Two decisions run through it.

**Constraints are in the database, not only in Python.** Stars are 1 to 5, a
username is unique, and an import cannot insert the same resource twice
because `(source_id, external_id)` is UNIQUE. Validation in Python gives a
nicer message; the constraint is what actually holds.

**Imported rows take their identity from upstream.** A resource is known by
the hash of its normalised URL, not by our id, so re-running an import finds
the row it wrote last time.

On PostgreSQL, `resources` also has a generated `tsvector` column with a GIN
index for full-text search. It is not mapped on the model, because SQLite has
no such type; the same DDL runs after `create_all` and in a migration, and
`alembic/env.py` excludes it from drift checks.

`base.py` owns the engine and the session. `get_session` gives each request
one session and commits it once, so a request makes all of its changes or
none of them.

### `bookstore/ingest` — the importer

| Module | Responsibility |
|---|---|
| `parsers.py` | One markdown list line to one record. No database, so it is tested against real upstream lines |
| `pipeline.py` | Records to rows: content-hash diff, `INSERT … ON CONFLICT DO UPDATE`, topic links, an audit row per run |
| `sources/` | One module per catalogue: where to fetch it, its licence, its credit line |

Parsing is where the surprises are; writing is where the damage is. Keeping
them apart means each can be tested without the other.

### `bookstore/services` — the rules

| Module | Responsibility |
|---|---|
| `accounts.py` | Registration, passwords (PBKDF2), suspension, the warning ladder, promotion |
| `reviews.py` | Ratings, trusted weighting, moderation, the recommended and not-recommended lists |
| `resources.py` | Browsing and searching the imported catalogue; counting click-throughs |
| `catalog.py` | Browsing, facets and TF-IDF search over the demo shelf |
| `recommend.py` | Three picks, from what a reader browsed and reviewed |
| `insights.py` | The administrator's figures, in SQL: outer joins, a running-total window, a partitioned rank |
| `auth.py` | JWT access tokens, rotating refresh tokens, reuse detection |
| `chat_adapter.py`, `image_catalogue.py` | Adapters for the assistant and image search (below) |

### `bookstore/api` — transport

Split by resource, one router each. Authorisation is a dependency
(`require_user`, `require_super_user`), not an `if` inside each handler, so an
endpoint either states its privilege or is public on purpose.

`schemas.py` is deliberately separate from the ORM models. Returning ORM
objects is how password hashes end up in JSON: add a column and it appears in
every response. Here a field reaches the client only because somebody wrote it
down.

### The AI modules and their adapters

`chat.py` (intent routing and role tiering), `rag.py` (retrieval),
`imagesearch.py`, `shelfscan.py` and `biometrics.py` were written against
the original file-backed store. `bookstore/ports.py` writes down, as
`typing.Protocol` classes, the five methods they need; the adapters in
`services/` provide those over SQL. The recognition code crossed two storage
changes without being edited.

Retrieval is TF-IDF over the help files in `data/kb`, with two extra rules for
deciding whether a question is "local": the best passage must share a rare
term with the question, and the question must not name something the files
never mention. `tools/evaluate_ai.py` measures both on labelled questions.

---

## Things that were harder than they looked

### Eight importers, one catalogue

The importer runs on a schedule and can overlap a manual run. The UNIQUE
constraint stops duplicates, so it is tempting to call the job done.
Measured with eight importers starting together (`tools/bench_imports.py`):

| | Duplicate rows | Importers that crashed |
|---|---|---|
| Check, then insert | 0 | **35 of 40** |
| `INSERT … ON CONFLICT DO UPDATE` | 0 | 0 |

Both are "correct" about duplicates. Only one survives losing the race.

### Search that reads every row

`ILIKE '%term%'` cannot use a B-tree index, because of the leading wildcard.
At 2,434 rows that costs a few milliseconds and nobody notices. At about
100,000 (`tools/bench_search.py`) it took 128–157 ms; the full-text index
returned the same 1,558 rows for "machine learning" in 1.7–2.6 ms. Word
matching is also more correct: `ILIKE '%rust%'` finds *Zero Trust Networks*.

### Bugs that only real data showed

Putting the 2,434 imported books on a page found three faults that every
test had passed: topic names carrying upstream HTML anchors
(`<a id="csharp"></a>C\#`); a slug function that dropped "+" and "#", merging
C, C++ and C# into one topic; and topic links that were only ever added, so a
re-categorised book kept its old topic forever. Each now has a test built from
the real line that exposed it.

### An evaluation that was too easy

The first evaluation of the assistant used questions phrased much like the
help files, and showed 94% recall. Rewritten the way a visitor asks, recall
was 40%. The causes were a stemmer that turned "store" and "stored" into
different words, a filter that refused any question containing a word the
files never used, and role-locked intents that told visitors "access denied"
for questions about how the rules work. After fixing each on principle and
measuring once: 85% recall, 80% of unrelated questions kept out (55% without
the filter). The 90% goal is not met; that gap is the case for semantic
search.

### A moderation rule with a back door

Posting a comment with taboo words earned a warning; editing one did not.
Post something clean, then edit it as often as you like: five blocked edits,
zero warnings. Posting and editing now share one function, and the warning is
committed before the error is returned, because the request's rollback would
otherwise undo it.

### Security theatre

Refresh-token reuse detection revoked the whole token family and then raised
an error. The session dependency rolls back when a request raises, which
undid the revocation: the API said *"this session was ended for security"*
while every token kept working. A service committing its own work is normally
a smell; here it is the point.

### The same code behaving differently on two databases

PostgreSQL returns `TIMESTAMP WITH TIME ZONE` as an aware datetime; SQLite
returns a naive one. Comparing a token's expiry against now raised
`TypeError` on SQLite only. `models.as_utc` normalises on read, and CI runs
the suite against both.

### Face sign-in that worked half the time

Signing in used one photograph, and three consecutive frames of the same
person scored 0.774, 0.662 and 0.484 against a 0.62 threshold. The client
sends a short burst and the server keeps the best frame. A stranger's best
frame still has to clear the same threshold.

### History: selling the last copy twice

Before Open Shelf was free (ADR-002), checkout checked stock and then
decremented it. Sixteen simultaneous checkouts of one copy: **9 of 16** paid
without a row lock, 1 of 16 with `SELECT … FOR UPDATE`. `CHECK (stock >= 0)`
does not catch it — in a lost update every session reads 1 and writes 0, so
no row goes negative. That code is preserved in [the desktop version](https://github.com/Riyad291/open-shelf/tree/2e6cc23).

---

## Resolved ambiguities

The original feature list contradicted itself in places. The two that still
apply are flags under `policy` in `config/settings.json`, so the
reading taken is visible and reversible.

| Flag | Default | Why |
|---|---|---|
| `order_lists_use_weighted_average` | `true` | The weighted average classifies; the unweighted count decides whether there are "more than 3 ratings". Mixing them puts the wrong book on the list. |
| `visitors_may_review` | `true` | Reviews and moderation says *any* user can comment. A visitor has no account to carry a warning, so a blocked visitor comment is refused and logged instead. |

A third, that a demoted contributor is not promoted straight back, is now a
column (`users.promotion_blocked`) rather than a flag.

---

## What is deliberately not here

- **No ML framework for the recognition code.** Eigenfaces, the image
  descriptor and TF-IDF are numpy. OpenCV is used only for face detection.
- **No LangChain.** Retrieval is about 340 lines that can be read in one
  sitting, and the evaluation measures them directly.
- **No front-end framework.** Three files, no build step.
- **No queue, no Redis, no microservices.** Imports run from cron in the same
  image; the reasoning is in SYSTEM_DESIGN.md.
- **No rate limiting and no link checker yet.** Real gaps, listed in the
  README rather than left to be found.
