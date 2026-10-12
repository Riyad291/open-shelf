# Inkwell & Co. — an AI-enabled multimodal bookstore

[![CI](https://github.com/USERNAME/smart-bookstore/actions/workflows/ci.yml/badge.svg)](https://github.com/USERNAME/smart-bookstore/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%E2%80%93%203.12-blue)
![Tests](https://img.shields.io/badge/tests-100%20passing-brightgreen)
![Spec](https://img.shields.io/badge/requirements-10%2F10-brightgreen)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

A bookstore with four privilege tiers, **face-recognition login from the
browser**, keyword *and* picture search, a cart and checkout that cannot
oversell, moderated reviews, an assistant that answers from the shop's own
files, and a feature that reads your taste from a photograph of your bookshelf.

The face recogniser, the speaker recogniser, the image descriptor and the
retrieval are all written here in numpy. No ML framework does the interesting
part.

<p align="center">
  <img src="docs/screenshots/web-shelf.png" width="80%" alt="The catalogue drawn as book spines on a shelf">
</p>

---

## Run it

```bash
docker compose up --build
```

Then open **http://localhost:8000/app/**. That brings up PostgreSQL, applies
the migrations, seeds a demo shop and serves the application. The API
documents itself at `/docs`.

<details>
<summary>Without Docker</summary>

```bash
pip install -r requirements.txt

# SQLite needs no server and is enough to see everything except row locking.
export DATABASE_URL="sqlite:///bookstore.db"
export JWT_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"

alembic upgrade head
python tools/seed_db.py
python -m uvicorn bookstore.api.app:app --reload
```

For PostgreSQL instead, set
`DATABASE_URL=postgresql+psycopg://user:pass@localhost/bookstore`.
MySQL works too — it is a connection string and a driver.
</details>

### Demo accounts

| Username | Password | Role | Why it's interesting |
|---|---|---|---|
| `margaret` | `admin123` | Super-user | Finances, SQL reports, accounts, moderation |
| `alice` | `alice123` | Registered | Three purchases — one more promotes her |
| `carol` | `carol123` | VIP | 10% off, visible in the cart |
| `dave` | `dave1234` | Suspended | Try signing in: refused, and why |
| `bob` | `bob12345` | Registered | Carries a warning from a blocked comment |

The demo rows in the sign-in panel log you straight in, so you can switch
tiers in one click and watch the same page change.

---

## What it does

| | |
|---|---|
| **Four privilege tiers** | Visitor, registered customer, VIP, super-user. Privilege changes prices, recommendations, and how much the assistant will tell you. |
| **Face login** | Enrol from the browser camera, then sign in by looking at it. Eigenfaces in numpy. |
| **Cart and checkout** | Real line items, orders, payments — and the last copy cannot be sold twice. |
| **Keyword search** | TF-IDF over titles, authors, descriptions and full book texts, with a stemmer. |
| **Picture search** | Hand it a photo of a cover or an internal figure; it finds the book. |
| **Shelf Scan** | Photograph a bookshelf. It segments the photo, names each book, reads your taste and recommends three more. |
| **Moderated reviews** | Taboo words masked; more than three blocks the comment and earns a warning; three warnings suspends the account. |
| **Order lists** | More than three ratings and a weighted average below 2 or above 4 puts a title on the no-order or to-order list. |
| **Role-tiered assistant** | Answers from shop records, from RAG over local files, or from a local LLM — and always says which. |
| **SQL reporting** | CTEs, window functions and deliberate outer joins, in the management area. |

---

## Screenshots

**The cart, with the VIP discount applied.** The same cart costs a registered
customer more.

![The cart showing a VIP discount](docs/screenshots/web-cart.png)

**The assistant, answering from the shop's own files and citing them.**

![The assistant answering via RAG with a citation](docs/screenshots/web-chat.png)

**Reports, computed in SQL** — CTEs, window functions and outer joins.

![SQL reports in the management area](docs/screenshots/web-reports.png)

**Shelf Scan**, the creative feature.

![The Shelf Scan upload screen](docs/screenshots/web-scan.png)

---

## Architecture

Business rules live in `bookstore/services/` and nowhere else. The HTTP layer
is transport; the browser client is a view. Neither contains a rule.

```
                   ┌────────────────────────────┐
                   │  Browser client — web/     │
                   │  vanilla JS, no build step │
                   └─────────────┬──────────────┘
                                 │ fetch() + JWT
                   ┌─────────────▼──────────────┐
                   │  FastAPI — bookstore/api   │
                   │  43 routes, OpenAPI docs   │
                   └─────────────┬──────────────┘
                                 │ one session per request
         ┌───────────────────────▼────────────────────────┐
         │  Services — accounts, cart, checkout, reviews, │
         │  catalog, recommend, finance, auth             │
         └───────────────────────┬────────────────────────┘
                                 │ SQLAlchemy
         ┌───────────────────────▼────────────────────────┐
         │  PostgreSQL — 15 tables, Alembic migrations    │
         └────────────────────────────────────────────────┘

   Adapters ──▶ biometrics · imagesearch · shelfscan · rag · chat
   These were written against the old file store and are indifferent to
   where a book record comes from. They read it through five names that
   services/image_catalogue.py and services/chat_adapter.py now serve
   from SQL, so none of them changed when the storage did.
```

### Tech stack

| | |
|---|---|
| **Language** | Python 3.11–3.12, JavaScript (no framework), SQL |
| **API** | FastAPI · Pydantic · uvicorn — 43 routes |
| **Database** | PostgreSQL · SQLAlchemy 2 · Alembic. Runs on SQLite and MySQL too |
| **Auth** | JWT access tokens + rotating refresh tokens with reuse detection; PBKDF2 passwords |
| **ML / CV** | numpy and OpenCV. Eigenfaces, MFCCs, TF-IDF and the image descriptor are implemented here, not imported |
| **Testing** | pytest — 100 tests, run against both PostgreSQL and SQLite |
| **CI/CD** | GitHub Actions — tests on two databases, migration drift check, conformance, lint, Docker |

---

## The interesting problems

**Two people bought the last copy.** `purchase()` checked stock and then
decremented it with no atomicity across the pair. Measured on PostgreSQL with
sixteen customers checking out simultaneously:

| | Orders paid | |
|---|---|---|
| Without a row lock | **9 of 16** | nine customers charged for one copy |
| With `SELECT … FOR UPDATE` | **1 of 16** | correct |

The locks are taken in ascending book id, because two customers buying the
same two books in opposite orders is a deadlock otherwise. `CHECK (stock >= 0)`
does *not* fix this and it is worth knowing why: in a lost update every session
reads 1 and writes 0, so no row goes negative and the constraint stays silent.

**Face login was a coin toss.** Enrolment worked; signing in used a single
photograph. Against a trained model, three consecutive frames of the same
person scored 0.774, 0.662 and **0.484** against a 0.62 threshold — a blink or
a slight turn and you were refused for no visible reason. The client now sends
a short burst and the server keeps the best. Measured afterwards: the enrolled
person signed in 6 of 6 times, a stranger 0 of 6.

**Refresh worked on PostgreSQL and crashed on SQLite.** PostgreSQL returns
timezone-aware datetimes; SQLite returns naive ones. Comparing an expiry
against now raised `TypeError` — on the path every signed-in person takes every
fifteen minutes. The suite now runs against both databases for this reason.

**Reuse detection detected reuse and did nothing.** A reused refresh token
revoked the whole family, then raised — and the session dependency rolls back
when a request raises, undoing the revocation. The API said *"this session was
ended for security"* while every token kept working, which is worse than not
detecting it, because it looks handled.

**OpenCV 5 removed the thing everyone uses.** `cv2.face.LBPHFaceRecognizer`,
`cv2.CascadeClassifier` and the bundled Haar cascades are gone. The recogniser
is eigenfaces written here: crop, histogram-equalise, 96×96, mean-centre, PCA
by SVD, nearest neighbour, confidence-thresholded.

**"What is the capital of Mongolia?" was answered from the shop's FAQ.** It
scored just high enough on filler words. Retrieval now requires the best chunk
to clear a similarity threshold **and** share a rare term with the corpus.

---

## Tests

```bash
pytest tests/                                    # 96 passing, 4 skipped
TEST_DATABASE_URL=postgresql+psycopg://… pytest  # 100 passing
python tools/verify_features.py                  # 10 of 10 requirements
```

| Suite | What it is for |
|---|---|
| `test_rules.py` | Every numbered requirement, in isolation |
| `test_api.py` | Status codes, privilege on every route, order scoping, credential leakage |
| `test_concurrency.py` | The last copy, bought by sixteen people at once |
| `test_integration.py` | One long session, then audited like a shop's books: the report must reconcile with the ledger, stock must be conserved, no account may hold an impossible state |

`test_concurrency.py` includes a canary that reimplements checkout **without**
the row lock and asserts that it oversells. If the race ever stops reproducing,
the real test has stopped being evidence and says so, rather than passing
quietly. Those tests skip on SQLite rather than pass vacuously, and CI fails
the build if they skipped on PostgreSQL.

`tools/verify_features.py` is a conformance check rather than a test: it builds
a throwaway shop, drives the real API the way a grader would, and prints the
evidence beside each requirement. A passing suite proves the code does what the
tests say; this proves the tests say what the specification says.

---

## Known limitations

Stated here rather than left to be discovered.

- **No rate limiting on sign-in.** Passwords are PBKDF2 over 120,000 rounds,
  which makes guessing slow, but nothing stops an attacker trying.
- **Face login is a convenience, not a hard security boundary.** A photograph
  of the account holder will usually pass. The threshold is set to reject
  strangers, not to be unspoofable, and the password remains.
- **No accuracy figure is claimed for face recognition.** It is verified end to
  end with synthetic faces and a simulated camera. That proves the pipeline,
  not a number for real faces.
- **Voice login has no web interface.** The MFCC speaker recogniser is still in
  the codebase; only face login is exposed over HTTP.
- **`CORS_ORIGINS` defaults to `*`.** Fine for a local demo with bearer tokens
  and no cookies; name your origin before deploying.
- **The LLM fallback needs Ollama running locally.** Without it, shop questions
  are still answered from the knowledge base and the app says the model is
  offline.

---

## Project history

This began as a university software-engineering project with two constraints:
a local GUI, and text files instead of a database. It met both — that version
is tagged **`v1.0-coursework`** and still runs:

```bash
git checkout v1.0-coursework   # Tkinter desktop app, JSON storage, 198 tests
```

Everything after that tag targets a different audience. The reasoning, the
options weighed and what was given up are in
[ADR-001](docs/adr/ADR-001-postgresql-and-web-only.md), and
[docs/FEATURE_PARITY.md](docs/FEATURE_PARITY.md) maps every original
requirement to where it lives now.

---

## What's where

```
smart_bookstore/
├── docker-compose.yml          PostgreSQL + the app, one command
├── alembic/                    migrations
├── bookstore/
│   ├── db/                     schema and session
│   ├── services/               THE BUSINESS RULES
│   │   ├── checkout.py         the transaction, with row locks
│   │   ├── auth.py             JWT and refresh-token rotation
│   │   └── …                   accounts, cart, reviews, catalog, finance
│   ├── api/
│   │   ├── routers/            one per resource
│   │   └── deps.py             authorisation, declared not remembered
│   ├── biometrics.py           eigenfaces + MFCC speaker ID, in numpy
│   ├── imagesearch.py          the image descriptor
│   ├── shelfscan.py            the creative feature
│   ├── rag.py  chat.py  llm.py the assistant
│   └── moderation.py           taboo masking and blocking
├── web/                        the browser client — 3 files, no build step
├── tools/
│   ├── seed_db.py              the demo shop
│   ├── verify_features.py      every requirement, against running code
│   └── check_camera.py         diagnose a camera that will not work
├── tests/
└── docs/                       ADR, feature parity, screenshots
```

## License

MIT — see [LICENSE](LICENSE).
