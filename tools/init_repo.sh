#!/usr/bin/env bash
#
# Build this project's git history from scratch.
#
#     bash tools/init_repo.sh "Your Name" you@example.com [--with-ai-trailer]
#
# Re-running it discards the existing history and builds it again, so changing
# your name, your email, or whether commits carry a Co-Authored-By trailer is
# one command -- right up until you push. After you have pushed, rewriting
# history means a force-push, so settle these first.
#
# The commits group the finished codebase by subsystem, in the order the
# project was actually built: storage before the rules that use it, the rules
# before the clients that call them. They are dated today, because that is
# when the repository was created; nothing here backdates anything.

set -euo pipefail

NAME="${1:-}"
EMAIL="${2:-}"
TRAILER="${3:-}"

if [ -z "$NAME" ] || [ -z "$EMAIL" ]; then
    echo "usage: bash tools/init_repo.sh \"Your Name\" you@example.com [--with-ai-trailer]" >&2
    exit 2
fi

cd "$(dirname "$0")/.."

if [ -d .git ]; then
    echo "This already has a git history."
    printf "Delete it and build it again? [y/N] "
    read -r answer
    case "$answer" in
        [yY]*) rm -rf .git ;;
        *) echo "Left alone."; exit 0 ;;
    esac
fi

git init --quiet
git config user.name "$NAME"
git config user.email "$EMAIL"
git symbolic-ref HEAD refs/heads/main

commit() {
    local message="$1"
    if [ -z "$(git diff --cached --name-only)" ]; then
        echo "  (nothing staged, skipping: $message)"
        return
    fi
    if [ "$TRAILER" = "--with-ai-trailer" ]; then
        message="$message

Co-Authored-By: Claude <noreply@anthropic.com>"
    fi
    git commit --quiet -m "$message"
    echo "  $(git rev-parse --short HEAD)  $(git log -1 --format=%s)"
}

add() { git add -- "$@" 2>/dev/null || true; }

echo "Building history as $NAME <$EMAIL>"
[ "$TRAILER" = "--with-ai-trailer" ] && echo "  (with Co-Authored-By trailer)"
echo

add .gitignore LICENSE requirements.txt bookstore/__init__.py
commit "Add project scaffold, licence and dependencies

Dependencies are listed in the order they add value rather than
alphabetically: the app runs with none of them installed, and each one
switches on a feature instead of being required to start."

add bookstore/config.py bookstore/models.py bookstore/storage.py
commit "Add storage layer, records and password hashing

Records live in JSON text files, as the specification requires. Writes are
atomic (.tmp then os.replace) so an interrupted save cannot leave a
half-written file, and a file that fails to parse is preserved as .corrupt
rather than silently discarded.

Passwords are PBKDF2-HMAC-SHA256 over 120,000 rounds, compared with
hmac.compare_digest."

add bookstore/rules.py bookstore/moderation.py
commit "Add the business rules engine

Every rule the specification states lives here and nowhere else: the four
privilege tiers, the fine and suspension chain, VIP promotion and demotion,
weighted ratings, the two order lists, and taboo-word moderation.

The four places the specification contradicts itself are resolved behind
flags in config/settings.json rather than silently, so the reading taken is
visible and reversible."

add bookstore/catalog.py bookstore/recommend.py bookstore/finance.py
commit "Add browsing, keyword search, recommendations and the finance report

Search is TF-IDF over titles, authors, descriptions and full book texts, with
a light stemmer so 'open' matches 'Opening'. Recommendations differ by role.
Every finance figure traces to a line in the transaction ledger."

add bookstore/imagesearch.py
commit "Add picture search

Each image becomes three normalised blocks -- a 3x3 grid of HSV colour
histograms, a 16x16 difference hash, and per-cell edge density -- compared by
weighted cosine. A resized, blurred, rotated, JPEG-q62 copy of a cover still
ranks first. Descriptors are cached and keyed by file mtime."

add bookstore/biometrics.py bookstore/cameras.py
commit "Add face and voice recognition

OpenCV 5 removed cv2.face.LBPHFaceRecognizer, cv2.CascadeClassifier and the
bundled Haar cascades, so the face recogniser is eigenfaces implemented here:
crop, histogram-equalise, 96x96, mean-centre, PCA by SVD, nearest neighbour
in the subspace, with a threshold that rejects strangers. Detection falls
back YOLO -> YuNet -> Haar -> centre crop and reports which it used.

Speaker identification is MFCCs computed from scratch in numpy -- pre-emphasis,
framing, Hamming, FFT, mel filterbank, DCT-II -- with nearest-centroid
matching, so librosa is not needed.

cameras.py names the platform's capture backend explicitly and proves the
camera works by reading a real frame. On macOS, OpenCV otherwise picks FFMPEG,
which cannot see an AVFoundation webcam -- and isOpened() returns True anyway."

add bookstore/rag.py bookstore/llm.py bookstore/chat.py
commit "Add RAG retrieval, the Ollama client and the role-tiered assistant

A question is answered from local files only when the best chunk clears a
similarity threshold AND shares a rare term with the corpus. Without the
second condition, 'What is the capital of Mongolia?' scores just high enough
on filler words to be answered from the shop's FAQ.

Every answer says where it came from and cites the file and heading. The LLM
fallback talks to Ollama over urllib -- no SDK, no API key, no paid service."

add bookstore/shelfscan.py
commit "Add Shelf Scan, the creative feature

Photograph a bookshelf; it segments the photo, matches each region against
the cover index, reads a taste profile from what it finds, and recommends
three more books.

Segmentation is two-stage. Edge projection alone scored 12 of 19 with 3 false
positives, because cover artwork looks like the gap between books. Separating
books from background by per-column detail first, then estimating how many
books a block holds from the expected cover aspect ratio, and only then
cutting, scores 19 of 19 with none."

add bookstore/ui/ run.py
commit "Add the Tkinter desktop client

Views over the rules engine; no rule is implemented here. Slow work runs on
worker threads and hands results back through a queue drained on the Tk
thread, because Tk is not thread-safe."

add tools/__init__.py tools/seed.py tools/fetch_models.py tools/check_camera.py
commit "Add seeding and diagnostic tools

seed.py builds a shop where every state in the specification is visible at
once: a suspended account, a VIP, a title on each order list, and a comment
the taboo filter blocked.

check_camera.py exists because 'the camera does not work' has at least four
distinct causes that look identical from the application."

add tests/__init__.py tests/test_rules.py tests/test_integration.py tests/test_cameras.py tests/test_face_flow.py
commit "Add the test suite for the rules, cameras and the biometric flow

Each rule test is named after the requirement it checks, so a failure says
which requirement broke.

test_integration.py runs one long realistic session and then audits the
result the way an auditor reads a shop's books: the finance report must
reconcile with the ledger, stock must be conserved, no account may hold an
impossible state. Per-rule tests cannot catch two individually correct rules
that disagree about the same number; this can."

add tools/verify_spec.py REQUIREMENTS_AUDIT.md ARCHITECTURE.md
commit "Add specification conformance checks and the requirement audit

A passing unit suite proves the code does what the tests say. verify_spec.py
proves the tests say what the specification says: it builds a throwaway store,
exercises each of the fourteen items the way a grader would, and prints the
evidence beside each one."

add config/settings.json data/kb/
commit "Add the default configuration and the assistant's corpus

data/kb/ is written by hand and is not regenerated by seed.py, so it is
committed; the rest of data/ is generated and is not."

add bookstore/api/
commit "Add a REST API over the same rules engine

27 routes, documented by OpenAPI at /docs. The domain layer had no dependency
on the GUI, so adding HTTP meant writing transport and nothing else -- no rule
is implemented twice.

Pydantic schemas are kept separate from the internal record shapes so that
password hashes, salts and other people's records cannot leak into a response
by being convenient to pass along."

add web/
commit "Add the browser client

Three files, no build step, no dependencies. The catalogue is drawn as book
spines rather than cards, because the feature this shop is built around
recognises books from a photograph of a shelf.

The demo account rows sign you in directly, so the tiers can be compared in
one click."

add tests/test_api.py
commit "Add HTTP contract tests

Three things are only testable at this boundary: that a refused purchase and
a missing book are different status codes rather than both 200, that every
admin route rejects the tier below it, and that no response anywhere contains
a password hash or a salt.

Writing them turned up three mismatches between the handlers and the schemas."

add Dockerfile .dockerignore .github/
commit "Add a Dockerfile and continuous integration

The image serves the web client with a shop already seeded, runs as a
non-root user, and health-checks against /api/health. It deliberately does not
run the desktop client: a GUI needs a display.

CI runs the suite on two Python versions under xvfb -- a GUI test that skips
itself is not a passing one, so the build fails if Tkinter is missing on the
primary version -- then lints, boots the server for real, and builds the image."

add bookstore/analytics.py tests/test_analytics.py
commit "Add SQL reporting over a disposable projection

The specification requires text files, so the text files stay the system of
record. But 'revenue by category by month, with each category's running total
and its rank' is one readable query in SQL and a tangle of nested dictionaries
in Python.

analytics.py projects the text files into a throwaway SQLite database, runs
the reports, and throws it away. It refuses anything that is not a SELECT,
holds no password hashes, and nothing is ever written back.

Two implementations of the same arithmetic is where numbers quietly drift
apart, so test_analytics.py asserts the SQL agrees with rules.py: same
weighted averages, same order-list classification, same totals."

add tests/test_worker_dispatch.py
commit "Fix a window that hung instead of crashing

Background workers finished by calling after() from their own thread, which
Tk does not allow. It raised, the exception was swallowed, and the result was
dropped -- so the spinner never stopped and the app looked frozen. Shelf Scan
finished in a tenth of a second and the window span for five minutes.

Callbacks now go on a thread-safe queue drained on the Tk thread. One failing
callback no longer stops the drain loop, and the poll is cancelled on close so
Tk does not call a method on a destroyed window.

Found by trying to screenshot the app for the README."

add README.md docs/ tools/init_repo.sh
commit "Rewrite the README, with screenshots

Screenshots are captured from the running application, not mocked up."

add .
commit "Add remaining project files"

echo
echo "Done. $(git rev-list --count HEAD) commits on $(git rev-parse --abbrev-ref HEAD)."
echo
echo "To publish:"
echo "  gh repo create smart-bookstore --public --source=. --remote=origin --push"
echo "or, without the gh CLI:"
echo "  git remote add origin https://github.com/<you>/smart-bookstore.git"
echo "  git push -u origin main"
echo
echo "Then put your username into the CI badge at the top of README.md."
