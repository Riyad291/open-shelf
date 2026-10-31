"""Recommendations: three recommendations, personalised by privilege.

A visitor and the administrator see what readers here rate most. A member or
trusted contributor sees books chosen from what they have browsed and
reviewed, with a sentence saying why -- a recommendation nobody can explain
is indistinguishable from an advert.

A book on the not-recommended list is never recommended. That would be a
straight contradiction between two of the feature list's own rules.
"""

from __future__ import annotations

from collections import Counter
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import models as m
from . import reviews as review_service


class Recommendation:
    __slots__ = ("book", "reason", "score")

    def __init__(self, book: m.Book, reason: str, score: float):
        self.book = book
        self.reason = reason
        self.score = score


def _excluded_ids(session: Session, config) -> set:
    """Books on the not-recommended list, which must not be recommended."""
    lists = review_service.order_lists(session, config)
    return {entry["book_id"] for entry in lists["no_order"]}


def _taste(session: Session, user: m.User) -> tuple:
    """What this reader leans towards, from browsing and reviewing.

    Two grouped queries rather than loading the history and counting in
    Python. Reviewing something is a stronger signal than glancing at it, so
    a review counts three times a view.
    """
    browsed = session.execute(
        select(m.Book.category, m.Book.author, func.count(m.BrowseHistory.id))
        .select_from(m.BrowseHistory)
        .join(m.Book, m.Book.id == m.BrowseHistory.book_id)
        .where(m.BrowseHistory.user_id == user.id)
        .group_by(m.Book.category, m.Book.author)
    ).all()

    reviewed = session.execute(
        select(m.Book.category, m.Book.author, func.count(m.Review.id))
        .select_from(m.Review)
        .join(m.Book, m.Book.id == m.Review.book_id)
        .where(m.Review.user_id == user.id, m.Review.blocked.is_(False))
        .group_by(m.Book.category, m.Book.author)
    ).all()

    categories: Counter = Counter()
    authors: Counter = Counter()
    for category, author, count in browsed:
        categories[category] += int(count)
        authors[author] += int(count)
    for category, author, count in reviewed:
        categories[category] += int(count or 0) * 3
        authors[author] += int(count or 0) * 3

    return categories, authors


def _already_seen(session: Session, user: m.User) -> set:
    """Books this reader has already reviewed: no point suggesting them."""
    return set(session.execute(
        select(m.Review.book_id).where(m.Review.user_id == user.id)
    ).scalars())


def top_for(session: Session, user: Optional[m.User], config,
            count: int = 3) -> List[Recommendation]:
    excluded = _excluded_ids(session, config)

    if user is None or user.role == m.SUPER_USER:
        return _store_wide(session, excluded, count)

    categories, authors = _taste(session, user)
    if not categories and not authors:
        return _store_wide(
            session, excluded, count,
            note="You have not browsed anything yet, so these are the "
                 "most reviewed here.")

    owned = _already_seen(session, user)
    books = session.execute(select(m.Book)).scalars().all()

    scored: List[Recommendation] = []
    for book in books:
        if book.id in excluded or book.id in owned:
            continue

        category_affinity = categories.get(book.category, 0)
        author_affinity = authors.get(book.author, 0)
        if not category_affinity and not author_affinity:
            continue

        score = category_affinity * 1.0 + author_affinity * 2.0
        score += min(float(book.browse_count or 0), 10.0) * 0.1

        reasons = []
        if author_affinity:
            reasons.append("you have read %s before" % book.author)
        if category_affinity:
            reasons.append("you read a lot of %s" % book.category)

        # Not .capitalize(): that lowercases the rest of the string, so
        # "you have read Charles Darwin before" came out as "charles darwin".
        sentence = " and ".join(reasons)
        sentence = sentence[:1].upper() + sentence[1:] + "."
        scored.append(Recommendation(book, sentence, score))

    scored.sort(key=lambda r: (-r.score, r.book.title))
    if len(scored) >= count:
        return scored[:count]

    # Not enough personal signal yet: top it up from the most reviewed
    # rather than returning one lonely suggestion.
    seen = {r.book.id for r in scored}
    for extra in _store_wide(session, excluded | seen | owned, count):
        scored.append(extra)
        if len(scored) >= count:
            break
    return scored[:count]


def _store_wide(session: Session, excluded: set, count: int,
                note: str = "") -> List[Recommendation]:
    """What readers here review and open most, for anyone with no history."""
    rated = func.coalesce(
        select(func.count(m.Review.id))
        .where(m.Review.book_id == m.Book.id, m.Review.blocked.is_(False))
        .correlate(m.Book)
        .scalar_subquery(), 0)

    statement = select(m.Book, rated.label("rated"))
    if excluded:
        statement = statement.where(m.Book.id.not_in(excluded))

    rows = session.execute(
        statement.order_by(rated.desc(), m.Book.browse_count.desc(),
                           m.Book.title).limit(count)
    ).all()

    out = []
    for book, rating_count in rows:
        if note:
            reason = note
        elif rating_count:
            reason = ("One of the most reviewed here (%d rating%s)."
                      % (rating_count, "" if rating_count == 1 else "s"))
        else:
            reason = "Popular with people browsing the library."
        out.append(Recommendation(book, reason, float(rating_count or 0)))
    return out


def similar_to(session: Session, book_ids: List[int], config,
               count: int = 3) -> List[Recommendation]:
    """Used by Shelf Scan: more like the books already on somebody's shelf."""
    if not book_ids:
        return []

    seeds = session.execute(
        select(m.Book).where(m.Book.id.in_(book_ids))
    ).scalars().all()

    categories = Counter(book.category for book in seeds)
    authors = Counter(book.author for book in seeds)
    excluded = _excluded_ids(session, config) | set(book_ids)

    candidates = session.execute(
        select(m.Book).where(m.Book.id.not_in(book_ids))
    ).scalars().all()

    scored = []
    for book in candidates:
        if book.id in excluded:
            continue
        score = categories.get(book.category, 0) * 1.0 \
            + authors.get(book.author, 0) * 2.5
        if not score:
            continue
        reason = ("You already own %s." % book.author
                  if authors.get(book.author) else
                  "Your shelf is heavy on %s." % book.category)
        scored.append(Recommendation(book, reason, score))

    scored.sort(key=lambda r: (-r.score, r.book.title))
    return scored[:count]
