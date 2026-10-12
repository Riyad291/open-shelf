# Feature parity: every requirement, and where it now lives

This project was built to a university specification and later extended for a
different audience. This document is the bridge between the two. It answers one
question for each of the specification's requirements: **is it still there, and
where can it be seen?**

Run the check yourself:

```bash
python tools/verify_features.py
```

It builds a throwaway shop in a temporary SQLite database, drives the real HTTP
API the way a grader would, and prints the evidence beside each requirement. It
needs no server, no PostgreSQL and no camera. The exit status is 0 only when
every requirement passes.

Last run: **10 of 10 verified.**

---

## The ten features

| # | Requirement | Status | Where to see it | Verified by |
|---|---|---|---|---|
| 1 | Four user types with different privileges | ✅ | Sign in as each demo account; the navigation and prices change | Same route returns 401 / 403 / 200 for visitor / customer / super-user |
| 2 | Top 3 recommendations, personalised for customers, store-wide for visitors and staff | ✅ | **Shelf** — the three spines at the top, with the reason underneath | Visitor gets *"one of the shop's best sellers"*, a customer gets *"you read a lot of literature"* |
| 3 | Assistant whose answers grow with privilege; super-user gets the finance report | ✅ | **Ask** — the scope line changes with your tier | Visitor refused (`access denied`); super-user not refused |
| 4 | Registered, VIP and super-user log in by face | ✅ | **Account → Sign in with your face**, then **Sign in → Sign in with your face** | Enrolled owner signed in; a different face refused with 401 |
| 5 | Super-user adds books; browse by category, author and period; keyword and picture search | ✅ | **Manage → Add a book**; **Shelf** filters; **Search** | Book created (201) and deleted (204); facets on all three axes; keyword and picture search both return hits |
| 6 | Balance, $1 fine, suspension, super-user reopening | ✅ | Sign in as `dave` and try to buy something | A refused purchase left −$0.50 and a $1.00 fine, suspended the account; the super-user reopened it |
| 7 | Reviews, purchaser weighting, order lists, taboo words, warnings | ✅ | Any book page; **Manage → Order lists** and **Moderation** | Purchasers weigh double (**weighted 4.29 vs plain 4.00**); both order lists populated; a blocked comment issued a warning; visitors may comment |
| 8 | VIP promotion and 10% discount | ✅ | Compare a book's price as `alice` and as `carol` | Same book: customer $8.50, VIP $7.65 — exactly 10% off |
| 9 | Local knowledge base, RAG routing, source attribution | ✅ | **Ask** — the tag above each answer says where it came from | *"What are your opening hours?"* → `via RAG`, citing `store.txt → Opening hours`. *"What is the capital of Mongolia?"* is **not** answered from the files |
| 10 | A creative feature of our own | ✅ | **Shelf Scan** — upload a photograph of a bookshelf | The endpoint segments the photo, matches covers and returns recommendations |

---

## The four notes, and what changed

The specification closed with four notes. Two were simplifications offered to
students; this project goes beyond both, with the professor's agreement that a
better implementation is acceptable. The other two are honoured exactly.

| Note | What it said | What this project does |
|---|---|---|
| 11 | *"A local GUI system is fine; no need for a web-based version."* | **Exceeded.** A web application, reachable from a link. The note permits a local GUI and does not require one, nor forbid a web version. The original Tkinter client is preserved at the `v1.0-coursework` tag. |
| 12 | A free Ollama-based LLM, no paid model | **Honoured.** `llm.py` talks to Ollama over plain `urllib` — no vendor SDK, no API key, no account. Without Ollama running, shop questions are still answered from local files and the app says the model is offline. |
| 13 | No database; local text files | **Exceeded.** PostgreSQL through SQLAlchemy, with migrations and transactions. The reasons, the options weighed and what was given up are recorded in [ADR-001](adr/ADR-001-postgresql-and-web-only.md). |
| 14 | Populate from free books (gutenberg.org) | **Honoured.** `tools/seed_db.py --gutenberg` downloads the real public-domain texts, which feed full-text keyword search. |

### Why notes 11 and 13 were exceeded

Both simplifications made the project easier to submit and harder to use as a
portfolio piece. A desktop GUI cannot be demonstrated from a link, and text
files demonstrate none of the schema design, transactions or SQL that employers
ask for.

Moving to a database also fixed a defect that could not be fixed cleanly
without one. `purchase()` checked stock and then decremented it with no
atomicity across the pair. Measured on PostgreSQL with sixteen customers
checking out the last copy simultaneously:

| | Orders paid | Correct? |
|---|---|---|
| Without a row lock | **9 of 16** | Nine customers charged for one copy |
| With `SELECT … FOR UPDATE` | **1 of 16** | Correct; fifteen told somebody was faster |

The full reasoning is in ADR-001.

---

## What was given up, stated plainly

- **The Tkinter desktop client has been removed** from the main line. It is
  preserved, working, at the `v1.0-coursework` tag:
  `git checkout v1.0-coursework`.
- **Voice login is not exposed over HTTP.** The speaker recogniser (MFCCs
  computed from scratch in numpy) is still in the codebase and still tested,
  but only face login has a web interface. Requirement 4 asks for face
  **and/or** voice, so this satisfies it — but it is a reduction from the
  desktop version, and worth saying out loud rather than leaving to be
  discovered.
- **Face recognition accuracy is not claimed.** It has been verified to work
  end to end — enrolment, training, recognition of the enrolled person, and
  rejection of a different face — using synthetic faces and a simulated
  camera. That proves the pipeline, not an accuracy figure for real faces, and
  no accuracy figure is claimed anywhere in this project.
