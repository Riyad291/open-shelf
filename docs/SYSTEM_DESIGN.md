# Open Shelf: system design

How the public platform is meant to work, what it costs, and what was measured
before deciding. This is the design for the deployed site. What runs today is
a subset of it, and each section says which parts exist.

**Contents:** 1. Requirements · 2. High-level design · 3. Deep dive ·
4. Capacity · 5. Trade-offs · 6. Build order · 7. What to revisit

---

## 1. Requirements

### Functional

- **Browse and filter** free technology books by topic, format and language.
- **Search** by title, author and description.
- **Open** a resource: count the click, redirect to where the book is hosted.
- **Import** catalogues from legitimate open sources on a schedule, safely.
- **Review and rate**, with moderation (taboo words, warnings, suspension).
- **Recommend** three titles, personalised once someone has rated things.
- **Ask** a retrieval-augmented assistant about the site, with citations.

### Non-functional

| Property | Target | Why this number |
|---|---|---|
| Browse latency (p95, server) | < 50 ms | A list page should feel instant |
| Search latency (p95, server) | < 100 ms | Typing-speed search |
| Availability | Best effort, single region | A portfolio site with no SLA |
| Import correctness | Re-runs change nothing; overlaps never crash | Imports run on a schedule and can overlap |
| Cost | Free tier | No revenue, by design |

### Constraints

- **Free hosting.** The database target is Neon's free tier: 1 GB of storage
  per project, 100 compute-hours a month, and scale-to-zero after 5 minutes
  idle, so the first request after a quiet spell pays a cold start.
- **Link out, never mirror** ([ADR-002](adr/ADR-002-free-resource-platform.md)).
- **Attribution travels with the data.** Every page that shows a source's
  rows shows its credit line.

---

## 2. High-level design

```
           browser
              │
              ▼
   ┌─────────────────────┐        ┌───────────────────────────┐
   │  Web client          │  JSON  │  FastAPI                  │
   │  (static, today;     │ ─────► │  routers → services       │
   │   Next.js SSR later) │        │  (one session per request)│
   └─────────────────────┘        └─────────────┬─────────────┘
                                                │ SQLAlchemy
                                                ▼
                                  ┌───────────────────────────┐
                                  │  PostgreSQL 16            │
                                  │  resources, topics, users,│
                                  │  reviews, import_runs     │
                                  │  (+ pgvector, planned)    │
                                  └─────────────▲─────────────┘
                                                │
             ┌──────────────────────────────────┴───────────┐
             │  Scheduled workers: same image, different     │
             │  command. Importer (daily), link checker      │
             │  (planned, weekly). No queue.                 │
             └───────────────────────────────────────────────┘

   Optional: an LLM (Ollama locally) behind the assistant. Without it, the
   assistant still answers from the local knowledge base.
```

**A reader arriving from a search engine.** The browser requests a resource
page, the API reads one row with its topics and source (three statements,
fixed), and the page shows the book with its attribution. Clicking *Open*
calls `/api/resources/{id}/open`, which increments the click count in one
`UPDATE` and returns a 303 to the book's real host.

---

## 3. Deep dive

### 3.1 Data model

```
sources 1──* resources *──* topics        (through resource_topics)
sources 1──* import_runs
users   1──* reviews *──1 books
```

- **`resources`** is identified by `(source_id, external_id)`, with a UNIQUE
  constraint. `external_id` is a hash of the normalised URL, so the same
  entry maps to the same row on every import.
- **`content_hash`** fingerprints everything upstream controls. An import
  compares hashes and writes only rows that changed.
- **Columns we own** (`click_count`, `dead`, `last_status`,
  `last_checked_at`) are never in the import's update list.
- **`work_key`** groups the same work listed more than once (a PDF and an
  HTML edition) without merging the rows.

**Planned: `resource_embeddings`** — `resource_id`, `vector(768)`,
`source_hash` (re-embed only when the text changed), and an HNSW index for
approximate nearest-neighbour search.

### 3.2 API contracts

| Method and path | Purpose | Notes |
|---|---|---|
| `GET /api/resources` | Browse and filter | `topic`, `media`, `language`, `q`, `sort`, `limit` ≤ 100, `offset` |
| `GET /api/resources/topics` | Topics with counts | Dead links excluded |
| `GET /api/resources/{id}` | One resource | Attribution and other editions |
| `GET /api/resources/{id}/open` | Click through | 303, never cached, so every click counts |

Page size is capped at 100 so that one request cannot ask for the whole table.

### 3.3 Search

Measured on PostgreSQL 16 by `tools/bench_search.py`: first at the real
catalogue size (2,434 rows), then grown to 99,794 rows by copying every row
forty times with its own identity. Each figure is the median of five runs
after a warm-up; three full runs varied by about 20%, so ranges are quoted.

| Query | 2,434 rows | 99,794 rows |
|---|---|---|
| Browse, first page by title | 0.05 ms | 0.06 ms (`ix_resources_title`) |
| Search "machine learning", `ILIKE` on three columns | 3 ms | **128–157 ms** (sequential scan) |
| Search "machine learning", `tsvector` + GIN index | — | **1.7–2.6 ms**, the same 1,558 rows |

- **`ILIKE '%term%'` cannot use a B-tree index**, because of the leading
  wildcard, so it reads every row. At 2,434 rows that is 3 ms and nobody
  notices; at 100,000 it is 50 to 90 times slower than the full-text index.
- **Word matching is also more correct.** `ILIKE '%rust%'` finds *Zero Trust
  Networks*; the full-text index matches the word, and its forms
  ("programs" finds "Programming").
- **The GIN index costs 6.4 MB** at 99,794 rows, a tenth of the table.
- **Browse is an index scan at both sizes.** An earlier measurement showed a
  sequential scan at the small size; that was the planner working from stale
  statistics before `ANALYZE`, not a missing index.

Further steps, in order, each only when the previous one is not enough:

1. **Trigram index** (`pg_trgm`) for typo tolerance.
2. **Semantic search** with embeddings, for "books like this".
3. **Hybrid ranking** with reciprocal rank fusion of the full-text and
   semantic lists.

None of these changes the ranker until there is a labelled evaluation set to
measure it against. The assistant already has one (`tools/evaluate_ai.py`).

### 3.4 Caching

An in-process cache with a short expiry for the topic list and the
recommendation shelf, the two reads that are identical for every visitor.
Redis is not worth a second service at this size: one process holds the
whole working set, and the cost of a stale topic count is nothing.

### 3.5 Background jobs

The importer and the planned link checker run as scheduled jobs from the same
image as the API, with a different command. There is no queue. A job that
fails leaves an `import_runs` row saying so, and the next run retries.

**Link checker (planned).** A `HEAD` request per resource, falling back to
`GET` when a host refuses `HEAD`; a polite per-host rate; `dead` set only
after several consecutive failures, so one bad night does not hide a book.

### 3.6 Rate limiting

Not built yet; listed as a known limitation in the README. The design is a
token bucket per IP stored in PostgreSQL (one row, one `UPDATE ... RETURNING`
per request), plus a global daily ceiling on LLM calls, so that a single
visitor cannot spend the whole budget.

### 3.7 Failure modes

| Failure | Effect | Handling |
|---|---|---|
| Upstream list unreachable | Import cannot run | Run recorded as failed; catalogue unchanged |
| Two imports overlap | Contention on the same rows | Upsert: no duplicates, no crash (measured) |
| A book's host goes away | Dead link | Link checker marks it; browse hides it |
| Database cold start (scale-to-zero) | First request slow | Accepted on the free tier |
| LLM down | Assistant loses free-form answers | Answers from the local knowledge base only |

---

## 4. Capacity

### Storage, measured

| Rows | Table, indexes and full-text index | Bytes per row |
|---|---|---|
| 2,434 | 2.2 MB | about 950 |
| 99,794 | 64 MB | about 670 |

The per-row cost falls as fixed overhead is shared, so a straight line from
the small table overstates the large one by about 40%.

**Embeddings dominate everything else.** At 99,794 rows, 768-dimension
float32 vectors take about 307 MB and 1,536 dimensions about 613 MB, most of
the free tier's 1 GB. That is the argument for the smaller model.

### Traffic

A 128 ms search ties up a worker for 128 ms, so one process serves about
eight searches a second. Full-text search at about 2 ms raises that ceiling
roughly fiftyfold, which is why it comes before any horizontal scaling.

### Scaling order

1. Indexes (full-text first)
2. Caching the identical reads
3. More worker processes
4. A read replica
5. Only then, anything distributed

### Observability

Request logs with latency, the `import_runs` table, and a health endpoint that
reports what the process is serving.

---

## 5. Trade-offs

| Decision | Chosen | Given up |
|---|---|---|
| Link out vs mirror | Link out | Availability when a host is down |
| Upsert vs check-then-insert | Upsert | Portability to databases without `ON CONFLICT` |
| Full-text vs `ILIKE` | Full-text | Substring matches inside words |
| No queue | Cron plus the same image | Retries with backoff |
| No Redis | In-process cache | A cache shared across processes |
| Never delete vanished entries | Keep them | A catalogue that shrinks when upstream does |

---

## 6. Build order

1. ~~Resource schema and idempotent importer~~
2. ~~Browse, search and open API~~
3. ~~Remove commerce~~
4. ~~Full-text search migration~~
5. Link checker
6. Rate limiting
7. Deploy (API plus database on free tiers)
8. Server-rendered pages for search engines
9. Semantic search, measured against the existing evaluation set
   (keyword retrieval keeps 80% of unrelated questions out; the goal is 90%)
10. A second source

## 7. What to revisit

- **Hosting**, if the free tier's cold starts hurt real visitors.
- **Embeddings dimension**, against storage, once semantic search is measured.
- **The no-queue rule**, once there is more than one kind of job.
