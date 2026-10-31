# ADR-001: Move to PostgreSQL and retire the desktop client

**Status:** Accepted
**Date:** 2026-10-12
**Deciders:** Project owner
**Supersedes:** the storage and client decisions implicit in the desktop version (commit `2e6cc23`)

## Context

The first version was a Tkinter desktop application storing records in
atomic JSON files -- no database, no web interface. It worked: 198 tests
passing, every feature implemented. That version is preserved in
[the desktop version](https://github.com/Riyad291/open-shelf/tree/2e6cc23).

Four things about it do not survive contact with its actual audience, which
is someone deciding whether to read further:

1. **A desktop GUI cannot be demonstrated from a link.** A recruiter will not
   clone a repository and install Tkinter. They will click a URL or move on.
2. **JSON files are not a data layer anyone hires for.** Job postings ask for
   SQL, schema design, migrations and transactions. The project currently
   demonstrates none of them in the write path.
3. **There is no cart, no order and no payment history** — the three entities
   that make an e-commerce schema worth discussing in an interview.
4. **A latent concurrency defect exists and cannot be fixed cleanly in the
   current design.** `rules.purchase()` checks stock and then decrements it,
   with no atomicity across the pair — a textbook time-of-check to time-of-use
   race. Two callers that both pass the check both settle; `max(0, stock - 1)`
   then hides the symptom, so the ledger records two sales of one copy while
   stock reads zero rather than negative.

   **What was actually observed**, stated precisely, because the distinction
   matters:

   | Test | Result |
   |---|---|
   | 12 threads calling `rules.purchase()` directly, `sys.setswitchinterval(1e-6)` | **Oversold in 15 of 30 trials**; up to 4 buyers charged for 1 copy |
   | 16 truly concurrent HTTP requests against a live uvicorn server | **Not reproduced** — 1 succeeded, as intended |
   | 16 concurrent PostgreSQL sessions, no row lock *(the replacement design, deliberately unlocked)* | **9 of 16 succeeded** against stock of 1 |
   | 16 concurrent PostgreSQL sessions, `SELECT ... FOR UPDATE` | **1 succeeded.** Correct |

   A note on what the schema does *not* do. `books.stock` carries a
   `CHECK (stock >= 0)`, and it is tempting to treat that as the oversell
   guard. It is not. In a lost update every session reads `stock = 1` and
   writes `stock = 0`; no row goes negative, the constraint never fires, and
   nine customers are charged for one copy — measured above, with zero
   database refusals. The constraint catches a single transaction decrementing
   past zero. Only the row lock catches the race.

   The race is real in the code and demonstrable deterministically under
   forced thread preemption. It was *not* reproducible over HTTP: the window
   between the check and the decrement is a few bytecodes wide with no I/O in
   it, and request jitter is orders of magnitude larger.

   This is a reason to fix it structurally rather than a reason to ignore it.
   A bug that needs a microsecond-wide coincidence is not a bug that will
   never happen — it is a bug that will happen once, in production, and be
   unreproducible afterwards. Fixing it with file locks is possible and
   miserable. It is one `SELECT ... FOR UPDATE` in a relational database,
   where correctness stops depending on how fast the loop runs.

### Forces

- The owner is a student, so **operational cost must be zero** and anyone must
  be able to run the project without an account or a credit card.
- A recruiter's patience is about **one command**. Setup longer than that is
  equivalent to no demo.
- The existing domain layer is already free of UI and storage assumptions —
  this is why adding an HTTP API earlier required writing transport only. That
  property should survive this change, not be spent by it.
- The biometric, retrieval and image-search code is the distinctive part of
  the project. None of it should be disturbed.

## Decision

1. **Adopt PostgreSQL as the system of record**, accessed through SQLAlchemy
   with Alembic migrations.
2. **Retire the Tkinter client.** The FastAPI service and its browser client
   become the only interface.
3. **Add the e-commerce entities** the current model lacks: cart, cart items,
   order, order items, payment.
4. **Keep the desktop version reachable in history** (commit `2e6cc23`), so the original design stays readable.

## Options considered

### Option A: Keep JSON files, add file locking

| Dimension | Assessment |
|---|---|
| Complexity | Medium — correct cross-platform file locking is subtle |
| Cost | Zero |
| Scalability | Poor — whole-file rewrites, no partial reads |
| Résumé value | **Negative.** Demonstrates avoiding the standard tool |

**Pros:** no new dependency; nothing else has to change.
**Cons:** solves the concurrency bug badly; teaches nothing a reviewer values;
leaves the cart and order model unmodelled or modelled poorly.

### Option B: SQLite as the system of record

| Dimension | Assessment |
|---|---|
| Complexity | Low — standard library, no server |
| Cost | Zero |
| Scalability | Adequate here; one writer at a time |
| Résumé value | Moderate |

**Pros:** nothing to install; `git clone && run` genuinely works; real SQL,
real transactions, real schema.
**Cons:** a single-writer database is a weaker talking point than a client-server
one; `SELECT ... FOR UPDATE` is a no-op, so the concurrency fix is a
coarse-grained database lock rather than a row lock, and the interesting
conversation disappears with it.

### Option C: PostgreSQL via SQLAlchemy, with SQLite as a fallback driver

| Dimension | Assessment |
|---|---|
| Complexity | Medium — a service to run, migrations to manage |
| Cost | Zero locally (Docker); free tiers exist for hosting |
| Scalability | Strong — row-level locking, concurrent writers, real indexes |
| Résumé value | **High.** The most requested database in backend postings |

**Pros:** row-level locking makes the oversell fix exact and explainable;
schema design, migrations and transaction isolation all become demonstrable;
an ORM-agnostic session layer means the same code runs on SQLite for tests and
for anyone who does not want Docker.
**Cons:** a reviewer without Docker needs the SQLite path; two supported
backends is a small ongoing tax.

### Option D: MySQL via SQLAlchemy

Equivalent to Option C on every dimension that matters here. MySQL is as
widely used and as well regarded by employers.

## Trade-off analysis

**A is rejected outright.** It is more work than C for a worse result in every
dimension the owner cares about.

**The real choice is B versus C**, and it is a trade between *setup friction*
and *demonstrated depth*. B is the lowest-friction thing that is still honest
SQL. C is strictly more impressive and strictly more work.

The deciding argument is the concurrency bug. On SQLite, the fix is "the
database serialises writers for me" — correct, but it is the database's
property rather than a demonstrated piece of engineering. On PostgreSQL, the
fix is a row lock that can be pointed at, explained, and shown failing a test
when removed. Given the race could not be triggered over HTTP, a fix that is
*structurally* correct matters more than one that merely narrows the window
further.

**C over D** because SQLAlchemy makes the difference a connection string.
PostgreSQL is chosen as the default for its slightly stronger presence in
backend job postings; switching to MySQL is one environment variable and a
driver, and the ADR records that as a non-decision rather than a commitment.

The cost of C — that a reviewer needs Docker — is paid off by keeping SQLite
working as a second driver. The application targets the SQLAlchemy session,
not a specific dialect, so this is a configuration concern rather than a
branch in the domain code.

## Consequences

**Easier**

- Concurrency correctness becomes expressible: one transaction, one row lock.
- Cart, order and payment are natural to model, with foreign keys and
  cascades doing work that would otherwise be hand-written.
- Reporting stops needing a projection layer; `analytics.py` loses its reason
  to exist and its queries move onto the real schema.
- Anything a reviewer asks about — indexes, joins, migrations, isolation
  levels — now has an answer in the repository.

**Harder**

- Two supported dialects mean the test suite must run against both, or the
  SQLite path will quietly rot.
- Migrations are a new discipline. A schema change is now two artefacts.
- Running the project gains a dependency on Docker for the default path.

**Lost**

- The two original constraints -- a local GUI, and records in text files
  rather than a database. Both are deliberately given up here, and
  commit `2e6cc23` preserves the version that kept them.
- The Tkinter client, with its camera dialog and Shelf Scan view. The
  underlying biometric and shelf-scanning code is retained and exposed over
  HTTP instead — the logic is not being deleted, only its desktop window.
- The checks covering those two constraints must be rewritten to state what
  is now true rather than quietly continuing to pass.

**To revisit**

- If the SQLite path proves a maintenance burden, drop it and require Docker.
- If deployment is attempted, connection pooling and migration-on-startup
  need a decision of their own.

## Action items

All complete as of 2026-10-12.

1. [x] Schema design; SQLAlchemy models; initial Alembic migration
       — 15 tables, `alembic/versions/7cf44f5aa99b_initial_schema.py`, verified
       up → down → up on both PostgreSQL and SQLite
2. [x] Port the rules engine to a session-per-request, with `FOR UPDATE` on
       the stock decrement — `services/checkout.py`; locks taken in ascending
       book id so two customers buying the same pair in opposite orders cannot
       deadlock
3. [x] Regression test: concurrent checkout of the last copy. It must
       demonstrate the oversell against an unlocked implementation and must
       never oversell against the locked one — `tests/test_concurrency.py`,
       including a canary that reimplements checkout without the lock and
       asserts that it oversells
4. [x] Cart, order, order item and payment entities and endpoints
5. [x] Full CRUD with correct status codes — 43 paths, 52 operations
6. [x] JWT access and refresh tokens, replacing the in-memory dict
       — `services/auth.py`, with rotation and reuse detection
7. [x] Delete `bookstore/ui`, `run.py` and the GUI tests
8. [x] `docker-compose` with PostgreSQL; CI against a real service container
       — five jobs, including an `alembic check` drift guard. The image has
       **not** been built locally: Docker Hub is unreachable from the
       development environment, so CI is the first place it builds
9. [x] State what is true now about notes 11 and 13 — `verify_spec.py` was
       replaced rather than patched. It asserted "no server component
       anywhere", which the API made false; `tools/verify_features.py` drives
       the real HTTP API and reports 10 of 10
10. [x] Rewrite README and ARCHITECTURE for the new shape
