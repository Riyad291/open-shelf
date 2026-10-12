# Architecture

## Contents

1. [Layering](#layering)
2. [Requirement traceability](#requirement-traceability)
3. [Resolved ambiguities](#resolved-ambiguities)
4. [Key design decisions](#key-design-decisions)
5. [Data model](#data-model)
6. [Known limitations](#known-limitations)

---

## Layering

```
          ┌──────────────────────────────────────────────┐
  UI      │  ui/app.py  login  shop  panels  admin        │   Tkinter only here
          └───────────────────────┬──────────────────────┘
                                  │
          ┌───────────────────────┴──────────────────────┐
  AI      │ chat  rag  llm  biometrics  imagesearch       │   optional deps,
          │ shelfscan  cameras                            │   all degrade gracefully
          └───────────────────────┬──────────────────────┘
                                  │
          ┌───────────────────────┴──────────────────────┐
  Domain  │ rules  catalog  recommend  moderation finance │   pure Python, no I/O
          └───────────────────────┬──────────────────────┘   → 79 unit tests
                                  │
          ┌───────────────────────┴──────────────────────┐
  Data    │ storage.py  ·  models.py  ·  config.py        │   the only filesystem code
          └───────────────────────┬──────────────────────┘
                                  │
                        data/*.json · data/kb/*.txt
```

Three properties this buys us:

**The domain layer has no imports from the UI or the AI layer.** Every business
rule in the spec can be tested without constructing a window or loading a model,
which is why the suite runs in under five seconds.

**`storage.py` is the only module that touches the filesystem for domain data.**
Swapping the text files for SQLite later would be one file's worth of work.

**Every AI adapter answers `available()` and a `dependency_message()`.** The app
launches with numpy, Pillow, OpenCV, sounddevice, ultralytics and Ollama all
absent; the affected tabs explain what to install instead of raising.

---

## Requirement traceability

| # | Requirement (abbreviated) | Implemented in | Tested by |
|---|---|---|---|
| 1 | Four user types with different privileges | `models.py` roles, `ui/app.py:rebuild_tabs` | `TestRoles` |
| 2 | Top-3: personalised for RC/VC, store-wide for SU/VS | `recommend.py:top_for` | `TestRecommendations` |
| 3 | Chat box, progressively richer by role; SU gets finances | `chat.py` intent table, `finance.py` | `TestChatTiering` |
| 4 | Face and/or voice login, YOLO-based face classification | `biometrics.py`, `cameras.py`, `ui/camera.py`, `ui/login.py` | `TestFaceEnrolmentAndMatching`, `TestPasswordGating`, `TestCameraDialog`, `TestOpenCamera` |
| 5 | SU book-entry GUI; browse by category/author/time; keyword + picture search | `ui/admin.py:AddBookPage`, `catalog.py`, `imagesearch.py` | `TestCatalogue` |
| 6 | Balance, $1 fine, negative → suspension, SU reopens | `rules.py:purchase/reopen_account` | `TestPurchasesAndBalance` |
| 7 | 1–5 stars + comments; purchasers weighted higher | `rules.py:submit_review/rating_summary` | `TestReviews` |
| 8 | >3 ratings avg <2 → no-order; avg >4 → to-order | `rules.py:list_status/order_lists` | `TestOrderLists` |
| 9 | Taboo list; mask with `*`; >3 → block + warning | `moderation.py` | `TestModeration` |
| 10 | RC >2 warnings → suspended; VC → demoted | `rules.py:_apply_warning` | `TestReviews` |
| 11 | >3 purchases or >$200 → VIP, 10% off | `rules.py:_check_promotion/price_for` | `TestVipPromotion` |
| 12 | Local store info as the RAG corpus | `data/kb/*.txt`, `rag.py` | `TestRag` |
| 13 | Route to RAG if local, else LLM; say which | `chat.py:ask` | `TestRag`, `TestChatTiering` |
| 14 | Creative feature | `shelfscan.py` | manual, 19/19 on 5 layouts |
| 15 | Local GUI, no web | Tkinter throughout | — |
| 16 | Free LLM via Ollama | `llm.py` (stdlib `urllib` only) | — |
| 17 | No database, text files only | `storage.py` | `TestStorage` |
| 18 | Gutenberg books | `tools/seed.py --gutenberg` | — |

---

## Resolved ambiguities

The spec left four questions open. Each is resolved with a documented default
and a **one-line switch** in `config/settings.json` under `spec_interpretation`,
so if the professor rules differently you flip a boolean rather than editing
code. All four are covered both ways by tests where it matters.

### 1. `allow_overdraft_purchase` — default `false`

> "When making purchases costing more money than the account balance, a 1-dollar
> fine will be applied."

Two readings: the purchase is **refused** and fined, or it **goes through**
(driving the balance negative) and is fined.

We refuse it. A fine reads as a penalty for the attempt, and a shop does not
hand over goods that cannot be paid for. The negative-balance suspension path is
still reachable — repeated failed attempts drive the balance below zero, which
is exactly what `dave` demonstrates in the demo script.

Set the flag to `true` for the other reading; `test_overdraft_flag_lets_the_purchase_through`
verifies it.

### 2. `demotion_blocks_repromotion` — default `true`

Requirements 10 and 11 contradict each other. A VIP demoted for warnings is now
a registered customer who still has more than three purchases on record — so
requirement 11 promotes them straight back, and the demotion is meaningless.

We set a `vip_ineligible` flag on demotion, which blocks re-promotion until the
super-user clears it (Accounts → *Clear warnings*, or reopening the account).
`test_demoted_vip_is_not_instantly_repromoted` covers the whole cycle.

### 3. `order_lists_use_weighted_average` — default `true`

Requirement 7 says purchaser comments carry more weight; requirement 8 sets
thresholds on "average rating" without saying which average.

We use the **weighted** average for the threshold and the **unweighted count**
for "more than 3 ratings" — weighting the count would let two buyers look like
four people. Both numbers are shown side by side on the book page and the order
lists so the difference is never hidden.
`test_weighting_can_change_which_list_a_book_lands_on` shows a case where it
matters: a verified buyer's 5 against three non-buyers' 1s is 2.0 plain (which
would be *not* no-order, exactly at the boundary) but 2.6 weighted.

### 4. `visitors_may_review` — default `false`

> "Any user can comment on any book…"

But requirement 9 warns the author of a blocked comment, and requirement 10
suspends or demotes them. A visitor has no account to carry a warning, so
"any user" cannot include visitors without making the moderation chain
unenforceable. Reviewing requires a login, and the UI says why.

### Boundary readings (not configurable)

The spec's comparisons are implemented strictly as written, and tested:

- "more than 3 ratings" → **4 or more** (`test_three_ratings_is_not_enough`)
- "average < 2" → 2.0 exactly is **not** no-order (`test_boundaries_are_strict`)
- "average > 4" → 4.0 exactly is **not** to-order (same test)
- "more than 3 taboo words" → 4 or more (`test_exactly_three_is_not_blocked`)
- "more than 2 warnings" → the **3rd** warning triggers the consequence
- "more than 3 purchases" → the **4th** purchase promotes
- Taboo words are counted as **occurrences**, not distinct words — saying the
  same slur four times is four hits.

---

## Key design decisions

### Why eigenfaces instead of OpenCV's LBPH recogniser

The obvious route is `cv2.face.LBPHFaceRecognizer_create()`. It lives in
`opencv-contrib-python`, and **OpenCV 5 removed the `face` module entirely** —
on a machine with OpenCV 5 installed, that call raises `AttributeError` and the
login is dead. OpenCV 5 also dropped `cv2.CascadeClassifier` and stopped
shipping the Haar cascade XMLs in `cv2.data`.

So detection and classification are separated:

- **Detection** uses whatever is available, in order: YOLO (`ultralytics`) →
  YuNet DNN (`cv2.FaceDetectorYN`) → Haar cascade (OpenCV 4.x only) → centre
  crop. The chosen backend is displayed on the login screen and the Biometrics
  page, so there's never any doubt which ran.
- **Classification** is ours: crop → histogram-equalise → 96×96 → mean-centre →
  PCA by SVD over the enrolled gallery → nearest neighbour by cosine in the
  eigenspace, with a 0.62 threshold so the model can answer "I don't know you".

This is the textbook eigenfaces method, needs only numpy, and is genuinely
*trained* on the enrolled samples — which is what the spec asks for. On
synthetic test identities it scores 3/3 with a stranger correctly rejected at
0.49 against the 0.62 threshold.

### Why the face is the login, not an optional extra

Requirement 4 says RC/VC/SU "must log in with a face and/or voice sample". The
first build treated biometrics as an alternative to the password, which quietly
made them optional — and since nobody had enrolled, the feature was invisible.

Now signing up *is* enrolment: the account is only created once five
photographs are in hand, and from then on that account signs in with the
camera. Three consequences had to be designed around rather than ignored:

**The bootstrap problem.** The first super-user cannot have enrolled a face,
because enrolment needs a running shop and somebody to operate it. So the
super-user keeps password access (`super_user_password_bootstrap`), and that is
the account that enrols everyone else. A system where the only way in is a face
that does not exist yet is a system nobody can open.

**Existing accounts.** The seeded demo accounts have no face. Requiring face
login for them would lock the shop's own demo out of itself, so the rule is
applied per account: *an account with an enrolled face must use it*. Enrol one
and it becomes face-only immediately.

**Order of operations at signup.** The account is created *after* the
photographs, never before. Creating it first and then enrolling would, if the
user closed the camera window, leave an account with no password route and no
face — unopenable. `test_abandoning_the_camera_creates_no_account` pins this.

### Why camera access goes through one module

`cv2.VideoCapture(0)` is the obvious way to open a webcam and it is not
reliable, for two reasons that only surfaced on a real machine.

**OpenCV picks the backend itself, and can pick one that cannot see the
camera.** On macOS it selected FFMPEG, which cannot enumerate an AVFoundation
device; the log read `open VIDEOIO/FFMPEG: Failed list devices for backend
avfoundation` followed by `camera failed to properly initialize!`. That looks
like a hardware fault and is really a backend mismatch. `cameras.py` names the
platform's native backend first — AVFoundation on macOS, DirectShow then Media
Foundation on Windows, V4L2 on Linux — and only then falls back to letting
OpenCV choose.

**`isOpened()` is not evidence.** On macOS a capture whose camera permission
has been denied routinely reports `isOpened() == True` and then returns
nothing from every `read()`. Code that trusts `isOpened()` reports success and
then shows a frozen black preview. `open_camera` therefore proves the device
by reading an actual frame, discarding the blank warm-up frames most webcams
emit, before returning it.

Two consequences fall out of doing this properly. The scan covers several
device indices, which matters because Continuity Camera often occupies index 0
with an iPhone that then goes to sleep. And when two devices open but deliver
nothing, the scan stops early and reports a permission problem rather than
working through every remaining combination — the cause is systemic, so
continuing only makes the user wait (16 seconds before, 3 after).

`tools/check_camera.py` exposes the same machinery as a diagnostic that prints
every backend and index it tried and what each one did, so a camera problem can
be identified without reading any source.

### Why the camera needs a preview

The original enrolment and login opened the webcam, grabbed frames and closed
it, with nothing on screen. That is unusable for a reason that only shows up in
someone else's hands: when it fails, every cause looks identical. A denied
camera permission, a face out of frame, bad lighting and an unenrolled account
all produced one silent failure.

Worse, the code collapsed them into one message. `identify_vector` returned
confidence `0.0` for an untrained model, and the loop's `result.confidence >
best.confidence` check is false for `0.0 > 0.0`, so the placeholder "Could not
see a face" survived — blaming the camera for a system where nobody had
enrolled. The fix was to seed `best` with `-1.0`, check `trained` before
touching the camera at all, and report the four failure modes separately.

`ui/camera.py` now shows the live feed with the detected face boxed, the
capture count, and the live match confidence. The capture runs on a worker
thread because `read()` blocks; Tk is not thread-safe, so the worker only ever
writes the newest frame into a lock-guarded slot and the UI pulls it on an
``after`` timer.

One subtlety: the detector returns a centre crop when it finds nothing, so a
box on screen is not evidence of a face. `FaceDetector.last_was_fallback` marks
that case, and the preview says "Looking for your face..." instead of boxing a
wall in green and enrolling it.

### Why TF-IDF instead of embeddings for RAG

The corpus is four text files. An embedding model would add a large dependency
and a model download to beat TF-IDF on a knowledge base this size — and TF-IDF
gives free, exact provenance, which is what lets each answer cite
`store_info.txt → Opening hours`.

Two refinements were necessary and both are load-bearing:

**Light stemming.** Without it, "when are you *open* on Sunday" did not match
the heading "*Opening* hours", and the staff roster (which mentions Sunday
shifts) outranked the actual opening hours. The stemmer runs the same way over
queries and documents, and repeats until the token stops changing — a single
pass is inconsistent ("ratings"→"rating" but "rating"→"rat"). Porter's step-1b
cleanup restores the silent *e* so "rate", "rated" and "rating" all agree.

**A rare-term requirement for routing.** TF-IDF cosine alone is not a usable
router. "What is the capital of Mongolia?" shares only filler words with the
corpus, yet scored 0.102 against a 0.08 threshold and was answered from the FAQ.
A question now counts as local only if the best chunk clears the threshold
**and** shares at least one term that appears in under a third of chunks. After
this, routing is 13/13 on a mixed set of shop and general questions.

### Why the chat tiering is a data table

`chat.py` declares each answerable topic as an `Intent` with a `min_role`. The
tiering is therefore a property of the data, not a thicket of `if role ==`
branches, and adding a topic is one list entry. It also makes the refusal
informative: the user is told which tier the topic needs, which is how a grader
*sees* requirement 3 working rather than inferring it from an empty answer.

Matching intents are sorted most-privileged-first, so "how much money have I
spent" resolves to the personal-account intent rather than the finance report.

### Why Shelf Scan segments in two stages

The first attempt cut the photo at every strong vertical edge. Cover artwork is
full of strong vertical edges, so it sliced *through* books and scored 12/19
with 3 false positives.

The fix was to stop looking for boundaries and start estimating the **count**.
Stage one separates books from shelf background by per-column detail (variance
plus gradient, split by an Otsu-style threshold) — gaps and backing are flat,
printed covers are not. Stage two takes each block, divides its width by the
expected cover width (0.66 × height), and takes only that many of the strongest
internal edges as cuts. However busy the artwork, we never take more cuts than
there are books. That took it to 19/19 with zero false positives across five
layouts (tight spacing, wide gaps, size jitter, two books, one book).

### Why passwords are hashed

Not a spec requirement, and the spec permits plain text files. But storing
plaintext passwords in `users.json` would be indefensible in a review, and
PBKDF2-HMAC-SHA256 is in the standard library. Biometrics are stored as sample
images and model centroids, never as raw identifying data beyond what the user
enrolled, and `data/kb/policies.txt` states that for customers.

### Why writes are atomic

`storage.py` writes to a `.tmp` sibling and `os.replace`s it. A crash during a
save can therefore never leave a half-written `books.json`, which with a plain
`open(path, "w")` is a real possibility. A file that *is* corrupt on load is
renamed `.corrupt` and preserved rather than silently overwritten — tested by
`test_a_corrupt_file_does_not_crash_loading`.

---

## Data model

All records are plain dicts so they serialise straight to JSON. Factory
functions in `models.py` are the single source of truth for each shape, and
`normalise_user` / `normalise_book` fill in fields added by later builds — so an
old `data/` folder keeps loading.

| File | Shape | Notes |
|---|---|---|
| `users.json` | `{user_id: record}` | balance, warnings, suspended, `vip_ineligible`, browsing and purchase history |
| `books.json` | `{book_id: record}` | price, stock, cover/figure/text paths, browse and purchase counters |
| `reviews.json` | `[record]` | keeps both `raw_comment` (SU only) and masked `comment`, plus `weight` and `blocked` |
| `transactions.json` | `[record]` | the ledger every finance figure derives from |
| `events.json` | `[record]` | audit trail: warnings, suspensions, promotions, demotions |
| `image_index.json` | descriptor cache | keyed by file mtime+size |
| `biometrics/face_model.json` | PCA mean, basis, gallery | retrained from samples at any time |
| `biometrics/voice_model.json` | per-user MFCC centroids | same |

IDs are zero-padded sequential strings (`u001`, `b014`) so they sort naturally
and read well in a demo.

---

## Known limitations

Stated plainly, because a project report that claims none is not credible.

**Biometric accuracy is only lightly validated on real people.** The face
pipeline has been run end to end on a real webcam and recognises its enrolled
user; beyond that, the evidence is synthetic identities, where it scores 3/3
with correct rejection of a stranger. The test that matters before a demo is
**false acceptance**: enrol yourself, then point the camera at someone who is
not enrolled and confirm they are refused. Real faces under varying lighting
and real voices in a noisy room are harder than anything tested here, and the
thresholds
(`FACE_MATCH_THRESHOLD = 0.62`, `VOICE_MATCH_THRESHOLD = 0.80` in
`biometrics.py`) will likely need tuning against your own enrolment data. The
voice threshold in particular is set against clean synthetic audio and is
probably too strict for a real microphone. Both are single constants, deliberately.

**Eigenfaces is not state of the art.** It is sensitive to pose and lighting and
will not match a modern embedding model. It was chosen because it trains on the
enrolled samples with no extra dependency and no model download, which the spec
explicitly favours. A FaceNet or ArcFace embedding would be the upgrade path,
and the `FaceRecogniser` interface would not change.

**Picture search is appearance-based, not semantic.** It matches *this image* to
*that cover*, robustly across resizing, blur, mild rotation and JPEG artefacts.
It cannot find "a book with a whale on the cover" — that needs CLIP or similar.

**Shelf Scan assumes one roughly straight-on shelf.** A photo taken at a steep
angle, a shelf with books lying flat, or a very dim image will segment poorly.
It also cannot read spine text, so a spine-out book only matches if its spine
resembles its indexed cover. Installing `ultralytics` plus `yolov8n.pt` switches
segmentation to YOLO's "book" class, which is more robust.

**The RAG corpus is small by design.** Four files, 35 chunks. TF-IDF is the
right tool at that size; at a few thousand pages it would need embeddings and a
proper vector index.

**Full-text search covers only the first 20 000 characters** of each book
(`TEXT_SAMPLE_CHARS` in `catalog.py`). Indexing complete Gutenberg texts is
several megabytes per title and makes index building slow for no demo benefit.

**Single-user, single-process.** An in-process lock serialises file access,
which is right for one desktop app. Two copies pointed at the same `data/`
folder would race.

**The GUI has no undo.** Deleting a book from the admin panel is immediate,
though reviews and purchase records survive it.
