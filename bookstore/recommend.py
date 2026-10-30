"""Top-3 recommendations (spec requirement 2).

Two code paths, exactly as the spec demands:

*   **RC / VC** -- personalised from that user's own browsing and purchasing
    history.  We build a taste profile over categories and authors, score every
    unpurchased book against it, and break ties with the book's weighted
    rating.
*   **SU / VS** -- the most purchased and most browsed books overall, with no
    personalisation (a visitor has no history, and the super-user wants the
    store-wide picture).

Books on the no-order list (requirement 8) are suppressed from personalised
recommendations: the store is not reordering them, so pushing them is wrong.
Books on the to-order list get a small boost.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional

from .db import models
from .ports import RatingRules
from .storage import Storage

# Scoring weights, kept here so they are easy to tune in one place.
W_CATEGORY = 3.0
W_AUTHOR = 2.5
W_RATING = 1.2
W_POPULARITY = 0.6
W_TO_ORDER_BOOST = 1.0


@dataclass
class Recommendation:
    book: dict
    score: float
    reason: str


class Recommender:
    def __init__(self, storage: Storage, rules: RatingRules):
        self.db = storage
        self.rules = rules

    # -- taste profile ------------------------------------------------------

    def _profile(self, user: dict) -> Dict[str, Counter]:
        """Weight categories and authors by how the user behaved.

        A purchase says much more about taste than a browse, so it counts
        three times as much.
        """
        categories: Counter = Counter()
        authors: Counter = Counter()

        for entry in user.get("browsing_history") or []:
            book = self.db.get_book(entry.get("book_id", ""))
            if book:
                categories[(book.get("category") or "").lower()] += 1
                authors[book.get("author") or ""] += 1

        for entry in user.get("purchase_history") or []:
            book = self.db.get_book(entry.get("book_id", ""))
            if book:
                categories[(book.get("category") or "").lower()] += 3
                authors[book.get("author") or ""] += 3

        return {"categories": categories, "authors": authors}

    # -- public API ---------------------------------------------------------

    def top_for(self, user: Optional[dict], count: int = 3) -> List[Recommendation]:
        """Dispatch on role, per requirement 2."""
        role = (user or {}).get("role", models.VISITOR)
        if user is not None and role in (models.REGISTERED, models.VIP):
            personal = self._personalised(user, count)
            if personal:
                return personal
            # A brand-new account has no history yet; fall back to store-wide
            # popularity rather than showing an empty shelf.
            return self._store_wide(count, note="popular while we learn your taste")
        return self._store_wide(count)

    def _personalised(self, user: dict, count: int) -> List[Recommendation]:
        profile = self._profile(user)
        if not profile["categories"] and not profile["authors"]:
            return []

        purchased = {
            e.get("book_id") for e in (user.get("purchase_history") or [])
        }
        cat_total = sum(profile["categories"].values()) or 1
        auth_total = sum(profile["authors"].values()) or 1

        scored: List[Recommendation] = []
        for book in self.db.list_books():
            book_id = book["book_id"]
            if book_id in purchased:
                continue
            status = self.rules.list_status(book_id)
            if status == "no-order":
                continue

            category = (book.get("category") or "").lower()
            author = book.get("author") or ""

            cat_affinity = profile["categories"].get(category, 0) / cat_total
            auth_affinity = profile["authors"].get(author, 0) / auth_total
            summary = self.rules.rating_summary(book_id)

            score = (
                W_CATEGORY * cat_affinity
                + W_AUTHOR * auth_affinity
                + W_RATING * (summary.weighted_average / 5.0 if summary.count else 0.0)
                + W_POPULARITY * self._popularity(book)
            )
            if status == "to-order":
                score += W_TO_ORDER_BOOST

            if score <= 0:
                continue

            scored.append(
                Recommendation(
                    book=book,
                    score=round(score, 4),
                    reason=self._reason(category, author, cat_affinity, auth_affinity,
                                        summary, status),
                )
            )

        scored.sort(key=lambda r: (-r.score, r.book.get("title", "")))
        return scored[:count]

    def _store_wide(self, count: int, note: str = "") -> List[Recommendation]:
        """Most purchased and most browsed, for SU and visitors."""
        books = self.db.list_books()
        if not books:
            return []

        max_purchases = max((int(b.get("purchase_count", 0)) for b in books), default=0) or 1
        max_browses = max((int(b.get("browse_count", 0)) for b in books), default=0) or 1

        scored: List[Recommendation] = []
        for book in books:
            purchases = int(book.get("purchase_count", 0))
            browses = int(book.get("browse_count", 0))
            # Purchases weigh more than browses but both count, since the spec
            # asks for "most purchased and browsed".
            score = 2.0 * (purchases / max_purchases) + 1.0 * (browses / max_browses)
            bits = []
            if purchases:
                bits.append("%d purchase%s" % (purchases, "" if purchases == 1 else "s"))
            if browses:
                bits.append("%d view%s" % (browses, "" if browses == 1 else "s"))
            reason = note or (", ".join(bits) if bits else "new in store")
            scored.append(Recommendation(book=book, score=round(score, 4), reason=reason))

        scored.sort(key=lambda r: (-r.score, r.book.get("title", "")))
        return scored[:count]

    def _popularity(self, book: dict) -> float:
        books = self.db.list_books()
        max_purchases = max((int(b.get("purchase_count", 0)) for b in books), default=0) or 1
        return int(book.get("purchase_count", 0)) / max_purchases

    @staticmethod
    def _reason(category, author, cat_affinity, auth_affinity, summary, status) -> str:
        if auth_affinity > 0 and auth_affinity >= cat_affinity:
            return "you have read %s before" % author
        if cat_affinity > 0:
            return "matches your interest in %s" % category
        if status == "to-order":
            return "highly rated by our customers"
        if summary.count:
            return "rated %.1f by %d reader%s" % (
                summary.weighted_average, summary.count,
                "" if summary.count == 1 else "s",
            )
        return "popular in store"

    # -- used by the chat box and Shelf Scan --------------------------------

    def similar_to(self, book_ids: List[str], count: int = 3,
                   exclude: Optional[set] = None) -> List[Recommendation]:
        """Recommend against an arbitrary set of books.

        Shelf Scan uses this: it detects the books on a photographed shelf and
        asks "what else?".
        """
        exclude = set(exclude or []) | set(book_ids)
        categories: Counter = Counter()
        authors: Counter = Counter()
        for book_id in book_ids:
            book = self.db.get_book(book_id)
            if book:
                categories[(book.get("category") or "").lower()] += 1
                authors[book.get("author") or ""] += 1

        cat_total = sum(categories.values()) or 1
        auth_total = sum(authors.values()) or 1

        scored: List[Recommendation] = []
        for book in self.db.list_books():
            if book["book_id"] in exclude:
                continue
            if self.rules.list_status(book["book_id"]) == "no-order":
                continue
            category = (book.get("category") or "").lower()
            author = book.get("author") or ""
            cat_affinity = categories.get(category, 0) / cat_total
            auth_affinity = authors.get(author, 0) / auth_total
            summary = self.rules.rating_summary(book["book_id"])
            score = (
                W_CATEGORY * cat_affinity
                + W_AUTHOR * auth_affinity
                + W_RATING * (summary.weighted_average / 5.0 if summary.count else 0.0)
            )
            if score <= 0:
                continue
            scored.append(
                Recommendation(
                    book=book, score=round(score, 4),
                    reason=self._reason(category, author, cat_affinity,
                                        auth_affinity, summary, "normal"),
                )
            )
        scored.sort(key=lambda r: (-r.score, r.book.get("title", "")))
        return scored[:count]
