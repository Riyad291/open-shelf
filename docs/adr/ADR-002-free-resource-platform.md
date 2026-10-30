# ADR-002: Become a free resource platform, not a shop

**Status:** Proposed
**Date:** 2026-10-30
**Deciders:** Project owner
**Builds on:** [ADR-001](ADR-001-postgresql-and-web-only.md)

## Context

ADR-001 moved the project to PostgreSQL and the web so that it could be shown
from a link. That solved who can *see* it. It did not solve who would *use* it,
and that question has changed the audience again: from a reviewer reading the
code to a reader looking for a book.

Three forces push the same way:

1. **A demo with no users proves less than a small site with real ones.** A
   shop nobody can buy from is a screenshot. A catalogue of free books that
   anyone can open is something people actually use.
2. **The shop was a simulation.** No money moved. The balance, the fines and
   the payment table modelled a business that did not exist, and the hardest
   question in the code ("can two checkouts sell the last copy?") was about a
   copy nobody would ever buy.
3. **Free means the commerce is dead code.** The moment every book is free,
   carts, orders, prices, stock and discounts have nothing left to do.

## Decision

1. **Open Shelf is a discovery platform** for free technology books, courses
   and papers.
2. **It indexes and links out; it never mirrors.** A resource row holds the
   metadata to find and judge a book, plus a link to where its author or
   publisher hosts it. No files are copied.
3. **Only legitimate open sources.** The first is the free-programming-books
   lists (CC BY 4.0), credited on every page that shows their data. Shadow
   libraries are out of scope permanently, whatever their catalogue size.
4. **The ten specification features are kept, reinterpreted where the shop
   made them meaningless** (table below).
5. **The engineering weight moves to ingestion:** idempotent, concurrent-safe
   imports with an audit trail, which is the problem a real catalogue has.
6. **Commerce is not deleted in this change.** Removing it is a migration of
   its own (action item 5), so this one stays reviewable.

### The ten features

| # | Feature | Status | As a free platform |
|---|---|---|---|
| 1 | Four user types | Kept | Visitor, member, trusted contributor, administrator |
| 2 | Top-3 recommendations | Kept | From what you read and rate, not what you bought |
| 3 | Tiered assistant | Kept | The administrator gets catalogue insights, not a finance report |
| 4 | Face sign-in | Kept | Unchanged |
| 5 | Add, browse and search | Kept | Extended to the imported catalogue |
| 6 | Balance, fine, suspension | **Reinterpreted** | Suspension stays, as moderation; money goes |
| 7 | Reviews, weighting, lists, taboo words | Kept | Trusted contributors weigh double instead of purchasers |
| 8 | VIP promotion and discount | **Reinterpreted** | Promotion to trusted contributor, earned by accepted reviews |
| 9 | Local RAG | Kept | Unchanged |
| 10 | A creative feature | Kept | Shelf Scan |

## Options considered

### A. Keep the shop

| | |
|---|---|
| For | No work. The oversell race is a good story. |
| Against | Nobody uses it. Every feature is a simulation. |

### B. Rebuild as a free platform from scratch

| | |
|---|---|
| For | A clean schema. |
| Against | Throws away tested moderation, recommendations, RAG and auth. |

### C. Pivot in place: add a resource catalogue, then remove commerce *(chosen)*

| | |
|---|---|
| For | Keeps everything that still has a job. Each step is a reviewable change with its own migration. |
| Against | For one release the schema holds both the shop and the catalogue. |

### D. Mirror the books' files for faster access

| | |
|---|---|
| For | Works when the upstream host is down. |
| Against | Redistribution needs each book's licence checked, and many forbid it. Storage cost. Rejected. |

### How option C writes rows

Imports run on a schedule and may overlap. Measured on PostgreSQL with eight
importers started on the same feed at the same instant:

| Approach | Duplicate rows | Importers that crashed |
|---|---|---|
| Check, then insert | 0 | **7 of 8** (IntegrityError) |
| `INSERT ... ON CONFLICT DO UPDATE` | 0 | **0 of 8** |

The UNIQUE constraint alone prevents duplicates. It does not keep the
importers alive; the upsert does.

## Consequences

**Easier.** The site has a reason for someone to visit it. Imports are safe to
re-run, so a cron job can keep the catalogue fresh.

**Harder.** Links rot, so link health becomes our problem. Search has to work
on thousands of rows, not a demo shelf of ten.

**Lost.** The checkout race, measured at 9 of 16 oversold without a row lock and
1 of 16 with one, stops being live code. It is preserved in ADR-001 and at
the `v1.0-coursework` tag.

**To revisit.** Semantic search once the catalogue is large enough to need it;
a second source once the first is solid.

## Action items

- [x] 1. Resource catalogue schema and migration
- [x] 2. Idempotent importer for free-programming-books
- [x] 3. This record
- [ ] 4. Link checker
- [ ] 5. Remove commerce, with its own migration
- [ ] 6. Rewrite `tools/verify_features.py` for the reinterpreted features
- [ ] 7. Semantic search
- [ ] 8. Deploy
- [ ] 9. Rewrite README and ARCHITECTURE
