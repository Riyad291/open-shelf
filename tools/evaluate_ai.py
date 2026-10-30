"""Measure the AI features instead of asserting that they work.

    python tools/evaluate_ai.py
    python tools/evaluate_ai.py --without-veto     # ablation

Three things are measured, each against a labelled set written down below:

1. **Retrieval routing.** The assistant answers a question from the local
   knowledge base (RAG) only when the files actually cover it. GROUNDED
   questions are covered and must be answered from the files, with a
   citation. ADVERSARIAL questions are not covered -- most are written to
   share words with the files ("a trusted editor on Wikipedia") -- and
   must not be.
2. **Tiering.** The same question gives different access to different
   roles (specification item 3).
3. **Picture search.** Each demo cover, uploaded, must find its own book.

The run fails (exit status 1) when any figure falls below its threshold, so
CI treats a regression in answer quality like a failing test.

Why not just unit tests: a unit test asserts one answer. A labelled set
gives a rate, and a rate is what changes when the ranking is tuned. A tweak
that fixes one question and breaks two others shows up here, not there.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("JWT_SECRET", "evaluate-ai-run")

# Covered by data/kb: each must be answered from the files and cited.
# Written the way a visitor would ask, not copied from the files' wording --
# a set phrased like the answers would measure nothing.
GROUNDED = [
    "Is Open Shelf free?",
    "Do I need an account to read the books?",
    "Who made this site?",
    "Where do the books come from?",
    "Do you host the PDFs yourselves?",
    "Are pirated books allowed here?",
    "How many books are in the catalogue?",
    "What should I do about a broken link?",
    "How do I become a trusted contributor?",
    "Can visitors leave reviews?",
    "Why was my comment blocked?",
    "What happens if I get too many warnings?",
    "How do I log in with my face?",
    "Is face login required?",
    "How do I get my suspended account back?",
    "What goes on the recommended list?",
    "How are my recommendations chosen?",
    "Can I search with a photo of a book cover?",
    "What data do you store about me?",
    "Is my password stored safely?",
]

# Not covered. Most share words with the files on purpose -- "trusted",
# "moderation", "licence", "face", "password" -- because a retriever that
# matches words rather than meaning answers these from the wrong file.
ADVERSARIAL = [
    "How do I become a trusted editor on Wikipedia?",
    "What are the moderation rules on Reddit?",
    "What licence does the Linux kernel use?",
    "Can I sign in to Gmail with my face?",
    "How do I reset my Netflix password?",
    "How much does a Coursera certificate cost?",
    "Who maintains the Python language?",
    "When was Python first released?",
    "Which programming language is the fastest?",
    "Where can I download free movies?",
    "How many books are in the Library of Congress?",
    "Who founded Creative Commons?",
    "How do I install PostgreSQL on Windows?",
    "How do I rate a restaurant on Yelp?",
    "What is the capital of Mongolia?",
    "Who won the 1998 World Cup?",
    "What is 17 times 23?",
    "What will the weather be tomorrow?",
    "What is the administrator's home address?",
    "How much money does Open Shelf make?",
]

# (question, who is asking, should they be refused?)
TIERING = [
    ("Show me the catalogue stats.", None, True),
    ("Show me the catalogue stats.", "member", True),
    ("Show me the catalogue stats.", "boss", False),
    ("What is on the recommended list?", None, True),
    ("What is on the recommended list?", "member", True),
    ("What is on the recommended list?", "trusted", False),
    ("What have I contributed?", None, True),
    ("What have I contributed?", "member", False),
]

# Regression gates, set just under what the current retriever measures, so
# CI fails if a change makes answers worse. They are not the goal. The goal
# for specificity and precision is 90%, and keyword retrieval does not reach
# it: the remaining leaks ("Who founded Creative Commons?") share real
# vocabulary with the files. Closing that gap is the case for semantic
# search, which will be measured against these same questions.
#
# Measured 2026-10-30 (20 grounded, 20 adversarial):
#                 recall  specificity  precision
#   with veto       85%       80%         81%
#   without veto    85%       55%         65%     (--without-veto)
THRESHOLDS = {
    "recall": 0.80,        # grounded questions answered from the files
    "specificity": 0.75,   # adversarial questions kept out of the files
    "precision": 0.75,     # of everything answered from the files, how much should have been
    "tiering": 1.00,       # access control has no acceptable error rate
    "image_top1": 0.80,    # a cover finds its own book first
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--without-veto", action="store_true",
        help="ablation: turn off the foreign-term check, to measure what it "
             "contributes (thresholds are not enforced)")
    args = parser.parse_args()

    if args.without_veto:
        from bookstore.rag import Retriever
        Retriever.foreign_terms = lambda self, question: []
        print("ABLATION: foreign-term veto disabled\n")

    tmp = tempfile.mkdtemp(prefix="evaluate-ai-")
    data_dir = os.path.join(tmp, "data")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shutil.copytree(os.path.join(root, "data", "kb"),
                    os.path.join(data_dir, "kb"))

    from fastapi.testclient import TestClient

    from bookstore.api import runtime
    from bookstore.api.app import create_app
    from bookstore.db import models as m
    from bookstore.db.base import Base, engine, session_factory
    from bookstore.services import accounts
    from demo_books import BOOKS, make_cover

    app = create_app(database="sqlite:///" + os.path.join(tmp, "eval.db"),
                     data_dir=data_dir, create_tables=True)
    # A server error is a result to count, not a reason to stop measuring.
    client = TestClient(app, raise_server_exceptions=False)

    session = session_factory()()
    for name, role in [("boss", m.SUPER_USER), ("member", m.REGISTERED),
                       ("trusted", m.VIP)]:
        accounts.create_account(session, name, "pw12345", role=role)
    session.commit()

    tokens = {}
    for name in ("boss", "member", "trusted"):
        reply = client.post("/api/auth/login",
                            json={"username": name, "password": "pw12345"})
        tokens[name] = {"Authorization": "Bearer "
                        + reply.json()["access_token"]}

    def ask(question, who=None):
        return client.post("/api/chat", headers=tokens.get(who, {}),
                           json={"question": question}).json()

    retriever = runtime.retriever()
    retriever.reload(force=True)
    print("Knowledge base: %d chunks from %d files\n"
          % (retriever.size, len(retriever.sources)))

    # ----------------------------------------------------------- routing
    grounded_hits, cited, misses = 0, 0, []
    for question in GROUNDED:
        answer = ask(question)
        if answer["source"] == "via RAG":
            grounded_hits += 1
            cited += bool(answer["citations"])
        else:
            misses.append("%s  -> %s" % (question, answer["source"]))

    leaks = []
    for question in ADVERSARIAL:
        answer = ask(question)
        if answer["source"] == "via RAG":
            leaks.append("%s  -> %s" % (question,
                                        (answer["citations"] or ["?"])[0]))

    answered_from_files = grounded_hits + len(leaks)
    figures = {
        "recall": grounded_hits / len(GROUNDED),
        "specificity": 1 - len(leaks) / len(ADVERSARIAL),
        "precision": (grounded_hits / answered_from_files
                      if answered_from_files else 0.0),
    }
    print("Routing")
    print("  grounded answered from the files   %2d of %d"
          % (grounded_hits, len(GROUNDED)))
    print("  ...of which carried a citation      %2d" % cited)
    print("  adversarial kept out of the files  %2d of %d"
          % (len(ADVERSARIAL) - len(leaks), len(ADVERSARIAL)))
    for question in misses:
        print("    missed: %s" % question)
    for line in leaks:
        print("    leaked: %s" % line)

    # ----------------------------------------------------------- tiering
    correct = 0
    for question, who, refused in TIERING:
        answer = ask(question, who)
        ok = bool(answer["denied_for_role"]) == refused
        correct += ok
        if not ok:
            print("    tiering wrong: %r as %s" % (question, who or "visitor"))
    figures["tiering"] = correct / len(TIERING)
    print("\nTiering\n  correct %d of %d" % (correct, len(TIERING)))

    # ----------------------------------------------------- picture search
    covers = os.path.join(data_dir, "covers")
    os.makedirs(covers, exist_ok=True)
    books = []
    for index, row in enumerate(BOOKS[:10]):
        title, author, category, year = row[:4]
        path = os.path.join(covers, "eval%02d.png" % index)
        if not make_cover(path, title, author, category, year, index + 1):
            continue
        book = m.Book(title=title, author=author, category=category,
                      year=year, cover_path=path)
        session.add(book)
        session.flush()
        books.append((book.id, path))
    session.commit()
    session.close()

    found = 0
    for book_id, path in books:
        with open(path, "rb") as handle:
            reply = client.post("/api/search/image",
                                files={"file": ("q.png", handle.read(),
                                                "image/png")})
        hits = reply.json() if reply.status_code == 200 else []
        if reply.status_code != 200:
            print("    picture search answered %d" % reply.status_code)
        if hits and hits[0]["book"]["id"] == book_id:
            found += 1
    figures["image_top1"] = found / len(books) if books else 0.0
    print("\nPicture search\n  own book ranked first %d of %d"
          % (found, len(books)))

    client.close()
    Base.metadata.drop_all(engine())
    shutil.rmtree(tmp, ignore_errors=True)

    # ----------------------------------------------------------- verdict
    print("\n%-12s %8s %10s" % ("metric", "value", "threshold"))
    failed = []
    for name, threshold in THRESHOLDS.items():
        value = figures[name]
        mark = "PASS" if value >= threshold else "FAIL"
        if mark == "FAIL":
            failed.append(name)
        print("%-12s %7.0f%% %9.0f%%  %s"
              % (name, value * 100, threshold * 100, mark))
    if args.without_veto:
        return 0
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
