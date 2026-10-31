# Feature parity: every requirement, and where it lives now

Open Shelf began as a university project built to a ten-feature
specification for a bookshop. It became a web application (ADR-001) and then
a free library (ADR-002). This document answers one question for each of
the original requirements: **is it still there, and how can it be seen?**

Run the check yourself:

```bash
python tools/verify_features.py
```

It builds a throwaway library in a temporary SQLite database, drives the real
HTTP API, and prints the evidence beside each requirement. It needs no
server, no PostgreSQL and no camera, and exits 0 only when every requirement
passes. It runs in CI on every push.

Last run: **10 of 10 verified.**

---

## The ten features

Two features were about money, which a free library does not have. They are
reinterpreted rather than dropped, and marked below. ADR-002 records why.

| # | Requirement | Now | Where to see it | Evidence from the last run |
|---|---|---|---|---|
| 1 | Four user types with different privileges | Visitor, member, trusted contributor, administrator | Sign in as each demo account; the navigation changes | Same route: visitor 401, member 403, administrator 200 |
| 2 | Top 3 recommendations, personalised | From what a reader browsed and reviewed | **Shelf**, the three spines at the top | Visitor: *"One of the most reviewed here"*; member: *"You have read Jane Austen before"* |
| 3 | Assistant whose answers grow with privilege | The administrator gets catalogue and community figures instead of a finance report | **Ask** | Visitor refused; administrator gets the report, computed in SQL |
| 4 | Sign in by face | Unchanged | **Account → enrol**, then **Sign in with your face** | Owner signed in from a held-out photo; a different face refused with 401 |
| 5 | Add books; browse by category, author and period; keyword and picture search | Unchanged, plus the 2,434-book library with topic filters and full-text search | **Manage → Add a book**; **Shelf**; **Search**; **Library** | Book created and deleted; facets on all three axes; keyword and picture search return hits |
| 6 | Balance, $1 fine, suspension, reopening | **Reinterpreted.** Suspension stays, triggered by moderation warnings; money goes | Sign in as `dave` | Three warnings suspended the account; the administrator reopened it and cleared them |
| 7 | Reviews, weighting, lists, taboo words, warnings | A trusted contributor's rating weighs double instead of a buyer's; the lists are "recommended" and "not recommended" | Any book; **Manage → Recommendation lists** and **Moderation** | Weighted 4.17 vs plain 4.00; both lists populated; a blocked comment issued a warning |
| 8 | VIP promotion and 10% discount | **Reinterpreted.** Promotion to trusted contributor, earned by three reviews that pass moderation | Sign in as `alice`, who is one review away | Promoted after 3 accepted reviews |
| 9 | Local knowledge base, RAG routing, source attribution | Unchanged; the knowledge base now describes Open Shelf | **Ask**: the tag above each answer says where it came from | A covered question answered via RAG with a citation; an off-topic one not answered from the files |
| 10 | A creative feature | Unchanged | **Shelf Scan**: upload a photo of a bookshelf | The photo is segmented, covers matched, recommendations returned |

How well the AI features work, beyond whether they work, is measured
separately by `python tools/evaluate_ai.py` on labelled questions.

---

## The four notes

| Note | What it said | What this project does |
|---|---|---|
| 11 | *"A local GUI system is fine; no need for a web-based version."* | **Exceeded.** A web application. The original Tkinter client is preserved at the `v1.0-coursework` tag. |
| 12 | A free LLM, no paid model | **Honoured, differently.** The requirement was a free model, and the example given was Ollama running locally. A deployed site has no local GPU, so `llm.py` now calls any OpenAI-compatible API over plain `urllib` — no vendor SDK — and defaults to Groq's free tier. Still free, still no paid model, and now it works on a server. Without a key the help files still answer, and the assistant says the model is not configured. |
| 13 | No database; local text files | **Exceeded.** PostgreSQL with migrations and transactions ([ADR-001](adr/ADR-001-postgresql-and-web-only.md)). |
| 14 | Populate from free books (gutenberg.org) | **Honoured, and extended.** The demo shelf uses public-domain Gutenberg titles; the library imports 2,434 free technology books from free-programming-books. |

---

## What was given up, stated plainly

- **The Tkinter desktop client** is preserved, working, at the
  `v1.0-coursework` tag.
- **Voice sign-in was removed.** It had no web interface, and a server has no
  microphone. Requirement 4 asks for face **or** voice; face remains.
- **Money is gone**, and with it the checkout race that was this project's
  best concurrency story. It is preserved in ADR-001 and at the tag, and the
  concurrent importer now carries that lesson.
- **No accuracy figure is claimed for face recognition.** It is verified end
  to end with synthetic faces, which proves the pipeline, not a number for
  real faces.
