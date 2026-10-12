"""Text-file persistence layer.

The spec forbids a database: "local text files are adequate".  This module is
the only place in the codebase that touches the filesystem for domain data, so
every other module stays pure and testable.

Design notes
------------
*   Each collection is one JSON document (JSON *is* text, and it gives us
    structure without a DB engine).
*   Writes are atomic: we write a ``.tmp`` sibling and ``os.replace`` it, so a
    crash mid-save can never leave a half-written catalogue.
*   An in-process ``RLock`` serialises access, which is enough for a single
    desktop app.
*   IDs are zero-padded sequential strings (``u001``, ``b014``) so they sort
    naturally and read well in a demo.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from . import config as cfg


def utc_now() -> str:
    """ISO-8601 UTC timestamp, used for every stored date."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_json(path: str, fallback: Any) -> Any:
    if not os.path.exists(path):
        return fallback
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read().strip()
        if not text:
            return fallback
        return json.loads(text)
    except (OSError, ValueError):
        # Corrupt file: preserve it for inspection rather than silently
        # overwriting the user's data, and start from the fallback.
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        return fallback


def _write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


class Storage:
    """Repository over the JSON text files in ``data/``."""

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = data_dir or cfg.DATA_DIR
        self._lock = threading.RLock()
        self._ensure_layout()

        self.users: Dict[str, dict] = {}
        self.books: Dict[str, dict] = {}
        self.reviews: List[dict] = []
        self.transactions: List[dict] = []
        self.events: List[dict] = []
        self.load()

    # -- paths --------------------------------------------------------------

    def _path(self, name: str) -> str:
        return os.path.join(self.data_dir, name)

    @property
    def kb_dir(self) -> str:
        return os.path.join(self.data_dir, "kb")

    @property
    def covers_dir(self) -> str:
        return os.path.join(self.data_dir, "covers")

    @property
    def figures_dir(self) -> str:
        return os.path.join(self.data_dir, "figures")

    @property
    def book_text_dir(self) -> str:
        return os.path.join(self.data_dir, "books")

    @property
    def faces_dir(self) -> str:
        return os.path.join(self.data_dir, "biometrics", "faces")

    @property
    def voices_dir(self) -> str:
        return os.path.join(self.data_dir, "biometrics", "voices")

    def _ensure_layout(self) -> None:
        for path in [
            self.data_dir,
            self.kb_dir,
            self.covers_dir,
            self.figures_dir,
            self.book_text_dir,
            self.faces_dir,
            self.voices_dir,
        ]:
            os.makedirs(path, exist_ok=True)

    # -- load / save --------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            self.users = _read_json(self._path("users.json"), {}) or {}
            self.books = _read_json(self._path("books.json"), {}) or {}
            self.reviews = _read_json(self._path("reviews.json"), []) or []
            self.transactions = _read_json(self._path("transactions.json"), []) or []
            self.events = _read_json(self._path("events.json"), []) or []

    def save_users(self) -> None:
        with self._lock:
            _write_json(self._path("users.json"), self.users)

    def save_books(self) -> None:
        with self._lock:
            _write_json(self._path("books.json"), self.books)

    def save_reviews(self) -> None:
        with self._lock:
            _write_json(self._path("reviews.json"), self.reviews)

    def save_transactions(self) -> None:
        with self._lock:
            _write_json(self._path("transactions.json"), self.transactions)

    def save_events(self) -> None:
        with self._lock:
            _write_json(self._path("events.json"), self.events)

    def save_all(self) -> None:
        self.save_users()
        self.save_books()
        self.save_reviews()
        self.save_transactions()
        self.save_events()

    # -- id generation ------------------------------------------------------

    @staticmethod
    def _next_id(prefix: str, existing: Callable[[], List[str]]) -> str:
        highest = 0
        for key in existing():
            if key.startswith(prefix):
                try:
                    highest = max(highest, int(key[len(prefix):]))
                except ValueError:
                    continue
        return "%s%03d" % (prefix, highest + 1)

    def next_user_id(self) -> str:
        with self._lock:
            return self._next_id("u", lambda: list(self.users.keys()))

    def next_book_id(self) -> str:
        with self._lock:
            return self._next_id("b", lambda: list(self.books.keys()))

    def next_review_id(self) -> str:
        with self._lock:
            return self._next_id("r", lambda: [r.get("review_id", "") for r in self.reviews])

    def next_transaction_id(self) -> str:
        with self._lock:
            return self._next_id("t", lambda: [t.get("txn_id", "") for t in self.transactions])

    # -- users --------------------------------------------------------------

    def get_user(self, user_id: str) -> Optional[dict]:
        return self.users.get(user_id)

    def find_user_by_username(self, username: str) -> Optional[dict]:
        target = (username or "").strip().lower()
        for user in self.users.values():
            if str(user.get("username", "")).strip().lower() == target:
                return user
        return None

    def put_user(self, user: dict, persist: bool = True) -> dict:
        with self._lock:
            self.users[user["user_id"]] = user
            if persist:
                self.save_users()
        return user

    def list_users(self, role: Optional[str] = None) -> List[dict]:
        users = sorted(self.users.values(), key=lambda u: u.get("user_id", ""))
        if role:
            users = [u for u in users if u.get("role") == role]
        return users

    # -- books --------------------------------------------------------------

    def get_book(self, book_id: str) -> Optional[dict]:
        return self.books.get(book_id)

    def put_book(self, book: dict, persist: bool = True) -> dict:
        with self._lock:
            self.books[book["book_id"]] = book
            if persist:
                self.save_books()
        return book

    def list_books(self) -> List[dict]:
        return sorted(self.books.values(), key=lambda b: b.get("book_id", ""))

    # -- reviews ------------------------------------------------------------

    def add_review(self, review: dict, persist: bool = True) -> dict:
        with self._lock:
            self.reviews.append(review)
            if persist:
                self.save_reviews()
        return review

    def reviews_for_book(self, book_id: str, include_blocked: bool = False) -> List[dict]:
        out = [r for r in self.reviews if r.get("book_id") == book_id]
        if not include_blocked:
            out = [r for r in out if not r.get("blocked")]
        return out

    def reviews_by_user(self, user_id: str, include_blocked: bool = True) -> List[dict]:
        out = [r for r in self.reviews if r.get("user_id") == user_id]
        if not include_blocked:
            out = [r for r in out if not r.get("blocked")]
        return out

    def user_has_reviewed(self, user_id: str, book_id: str) -> bool:
        return any(
            r.get("user_id") == user_id and r.get("book_id") == book_id
            for r in self.reviews
        )

    # -- transactions (the finance ledger behind the SU report) -------------

    def add_transaction(
        self,
        kind: str,
        user_id: str,
        amount: float,
        book_id: Optional[str] = None,
        note: str = "",
        persist: bool = True,
    ) -> dict:
        """Append one ledger line.

        ``kind`` is one of: purchase, fine, deposit, refund.  ``amount`` is the
        signed change to the *store's* revenue, so a purchase and a fine are
        positive and a refund is negative.
        """
        with self._lock:
            txn = {
                "txn_id": self.next_transaction_id(),
                "kind": kind,
                "user_id": user_id,
                "book_id": book_id,
                "amount": round(float(amount), 2),
                "note": note,
                "created_at": utc_now(),
            }
            self.transactions.append(txn)
            if persist:
                self.save_transactions()
        return txn

    def transactions_for_user(self, user_id: str) -> List[dict]:
        return [t for t in self.transactions if t.get("user_id") == user_id]

    # -- events (audit trail: warnings, suspensions, promotions) ------------

    def add_event(self, kind: str, user_id: str, detail: str, persist: bool = True) -> dict:
        with self._lock:
            event = {
                "kind": kind,
                "user_id": user_id,
                "detail": detail,
                "created_at": utc_now(),
            }
            self.events.append(event)
            if persist:
                self.save_events()
        return event

    def events_for_user(self, user_id: str) -> List[dict]:
        return [e for e in self.events if e.get("user_id") == user_id]

    # -- knowledge base files (the RAG corpus) ------------------------------

    def kb_files(self) -> List[str]:
        if not os.path.isdir(self.kb_dir):
            return []
        names = sorted(
            n for n in os.listdir(self.kb_dir)
            if n.lower().endswith((".txt", ".md")) and not n.startswith(".")
        )
        return [os.path.join(self.kb_dir, n) for n in names]
