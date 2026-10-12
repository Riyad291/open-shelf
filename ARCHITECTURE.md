# Architecture

How this is put together, and why. The decisions that were genuinely close
are in [ADR-001](docs/adr/ADR-001-postgresql-and-web-only.md); the mapping
from the original requirements to where each one now lives is in
[docs/FEATURE_PARITY.md](docs/FEATURE_PARITY.md).

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
         │  THE BUSINESS RULES LIVE HERE AND NOWHERE ELSE │
         └───────────────────────┬────────────────────────┘
                                 │ SQLAlchemy 2
         ┌───────────────────────▼────────────────────────┐
         │  PostgreSQL — 15 tables, Alembic migrations    │
         └────────────────────────────────────────────────┘
```

One rule, one place. The API layer validates input, decides who is calling and
serialises the answer; it does not know that a VIP gets 10% off. The browser
client renders; it does not know either. This is why adding the HTTP layer to
what had been a desktop application required writing transport and nothing
else, and it is the property most worth preserving.

---

## The layers

### `bookstore/db` — schema and session

`models.py` is the schema: fifteen tables with foreign keys, check
constraints and indexes. Three decisions run through it.

**Money is `Numeric(10, 2)` and `Decimal`, never `float`.** A float cannot
represent 0.10, so a balance built from float arithmetic drifts. This is the
most common defect in a hobby e-commerce schema.

**Prices are captured, not referenced.** An `order_item` stores the price
actually paid. Referencing the book's current price instead means raising a
price silently rewrites every past receipt.

**Derived figures are computed, not stored.** `purchase_count` and
`total_spent` are `column_property` correlated subqueries over the orders
table. They were Python properties walking relationships at first, which cost
**41 queries to list ten books** — on the catalogue, the most requested page
in the shop. A counter that can disagree with the rows it counts is a bug
waiting for a demo; a subquery in the same `SELECT` is neither.

`base.py` owns the engine and the session. `get_session` gives each request
one session and commits it once, so a request either makes all of its changes
or none of them.

### `bookstore/services` — the rules

| Module | Responsibility |
|---|---|
| `accounts.py` | Registration, sign-in, suspension, deposits, the warning ladder |
| `cart.py` | The basket. Reserves nothing; checkout is where stock is decided |
| `checkout.py` | The transaction: row locks, pricing, order, payment, ledger |
| `reviews.py` | Ratings, purchaser weighting, moderation, the two order lists |
| `catalog.py` | Browsing, facets, TF-IDF keyword search |
| `recommend.py` | Three picks, personalised by privilege |
| `finance.py` | The finance report and the SQL analytics |
| `auth.py` | JWT access tokens, rotating refresh tokens, reuse detection |
| `pricing.py` | What a given customer pays — one answer, three callers |

`pricing.py` exists because the catalogue, the cart and checkout all need the
same number. A price shown and a price charged that differ by a cent is a
defect customers notice before anybody else does.

### `bookstore/api` — transport

Split by resource, one router each. Authorisation is declared as a dependency
(`require_user`, `require_super_user`) rather than written into each handler,
so a new endpoint either states its privilege or is public on purpose.
Spreading `if user.role != "SU"` through twenty handlers is how one of them
ends up missing it.

`schemas.py` is deliberately separate from the ORM models. Returning ORM
objects is convenient and is how password hashes end up in JSON: add a column
and it appears in every response that serialises that model. Here a field
reaches the client only because somebody wrote it down.

### The adapters

`biometrics.py`, `imagesearch.py`, `shelfscan.py` and the 683-line assistant
in `chat.py` were written against the original file-backed store. Between them
they used eleven methods from it. Rather than rewrite four carefully tuned
modules for a storage change they are indifferent to,
`services/image_catalogue.py` and `services/chat_adapter.py` serve those same
eleven names over SQL. The image and recognition code crossed the migration
untouched.

---

## Things that were harder than they looked

### Selling the last copy twice

`purchase()` checked stock and then decremented it. Two callers that both pass
the check both settle, and `max(0, stock - 1)` hides it: stock reads zero
rather than negative while two customers are charged.

Measured on PostgreSQL, sixteen customers checking out one copy at once:

| | Orders paid |
|---|---|
| No row lock | **9 of 16** |
| `SELECT … FOR UPDATE` | 1 of 16 |

Two details matter. Locks are taken in **ascending book id**, because two
customers buying the same two books in opposite orders deadlock otherwise.
And `CHECK (stock >= 0)` does **not** prevent this: in a lost update every
session reads 1 and writes 0, so no row goes negative and the constraint never
fires. The constraint catches a single transaction going past zero. Only the
lock catches the race.

`tests/test_concurrency.py` keeps a canary that reimplements checkout without
the lock and asserts it oversells, so if the race ever stops reproducing the
real test says so instead of passing quietly.

### Face login that worked half the time

Enrolment was never the problem. Signing in used one photograph, and against a
trained model three consecutive frames of the same person scored 0.774, 0.662
and 0.484 against a 0.62 threshold. A blink or a slight turn and the account
owner was refused with nothing to act on. The client sends a short burst and
the server keeps the best. Rejection is unweakened: a stranger's best frame
still has to clear the same threshold.

### The same code behaving differently on two databases

PostgreSQL returns `TIMESTAMP WITH TIME ZONE` as an aware datetime; SQLite has
no timezone type and returns a naive one. Comparing a token's expiry against
now raised `TypeError` on SQLite only — on the path every signed-in person
takes every fifteen minutes. `models.as_utc` normalises on read, and the suite
runs against both databases so the next one of these is caught by CI.

### Security theatre

Refresh-token reuse detection revoked the whole token family and then raised
an error. The session dependency rolls back when a request raises, which undid
the revocation: the API answered *"this session was ended for security"* while
every token kept working. A service committing its own work is normally a
smell; here it is the point, and the reasoning is in the code beside it.

### Stale ORM collections

Three separate bugs came from the same misunderstanding: `session.add()` and
`session.delete()` do not update an already-loaded relationship collection. A
second checkout in one session saw an empty cart; removing a line left it on
screen; clearing the cart issued every `DELETE` twice. Mutating through the
collection, or querying instead of trusting it, fixes each.

---

## Resolved ambiguities

The original specification contradicted itself in four places. Each resolution
is a flag under `spec_interpretation` in `config/settings.json`, so the
reading taken is visible and reversible rather than buried.

| Flag | Default | Why |
|---|---|---|
| `allow_overdraft_purchase` | `false` | A purchase you cannot afford is refused *and* fined. The other reading lets it through and allows a negative balance. |
| `demotion_blocks_repromotion` | `true` | Item 7 demotes a VIP to registered; item 8 promotes a registered customer who has bought enough. Together they promote the person straight back, so a demoted VIP is marked ineligible. |
| `order_lists_use_weighted_average` | `true` | The weighted average classifies; the unweighted count decides whether there are "more than 3 ratings". Mixing them puts the wrong book on the list. |
| `visitors_may_review` | `true` | Item 7 says *any* user can comment. A visitor has no account to carry a warning, so a blocked visitor comment is refused and logged instead. |

---

## What is deliberately not here

- **No ORM for the recognition code.** Eigenfaces, MFCCs, the image descriptor
  and TF-IDF are numpy. OpenCV is used only for camera access and face
  detection, because OpenCV 5 removed the contrib `face` module.
- **No front-end framework.** Three files, no build step, no `node_modules`.
- **No message queue, no cache, no microservices.** A bookstore with sixteen
  titles does not need them, and adding them to look serious is how a project
  stops being readable.
- **No rate limiting.** A real gap, listed under Known limitations in the
  README rather than left to be found.
