"""Specification item 2: three recommendations, personalised by privilege.

A visitor and the super-user see what the shop sells most. A registered or
VIP customer sees books chosen from what they have browsed and bought, with
a sentence saying why -- a recommendation nobody can explain is indistinguishable
from an advert.

A book on the no-order list is never recommended. Recommending a title the
shop has decided to stop stocking would be a straight contradiction between
two of the specification's own rules.
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
    """Books on the no-order list, which must not be recommended."""
    lists = review_service.order_lists(session, config)
    return {entry["book_id"] for entry in lists["no_order"]}


def _taste(session: Session, user: m.User) -> tuple:
    """What this customer leans towards, from browsing and buying.

    Two grouped queries rather than loading the history and counting in
    Python. Purchases weigh more than views: buying a book says more than
    glancing at it.
    """
    browsed = session.execute(
        select(m.Book.category, m.Book.author, func.count(m.BrowseHistory.id))
        .select_from(m.BrowseHistory)
        .join(m.Book, m.Book.id == m.BrowseHistory.book_id)
        .where(m.BrowseHistory.user_id == user.id)
        .group_by(m.Book.category, m.Book.author)
    ).all()

    bought = session.execute(
        select(m.Book.category, m.Book.author, func.sum(m.OrderItem.quantity))
        .select_from(m.OrderItem)
        .join(m.Order, (m.Order.id == m.OrderItem.order_id)
              & (m.Order.status == "paid"))
        .join(m.Book, m.Book.id == m.OrderItem.book_id)
        .where(m.Order.user_id == user.id)
        .group_by(m.Book.category, m.Book.author)
    ).all()

    categories: Counter = Counter()
    authors: Counter = Counter()
    for category, author, count in browsed:
        categories[category] += int(count)
        authors[author] += int(count)
    for category, author, count in bought:
        categories[category] += int(count or 0) * 3
        authors[author] += int(count or 0) * 3

    return categories, authors


def _already_owned(session: Session, user: m.User) -> set:
    return set(session.execute(
        select(m.OrderItem.book_id)
        .join(m.Order, (m.Order.id == m.OrderItem.order_id)
              & (m.Order.status == "paid"))
        .where(m.Order.user_id == user.id)
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
                 "shop's most popular.")

    owned = _already_owned(session, user)
    books = session.execute(
        select(m.Book).where(m.Book.stock > 0)
    ).scalars().all()

    ratings = {}
    scored: List[Recommendation] = []
    for book in books:
        if book.id in excluded or book.id in owned:
            continue

        category_affinity = categories.get(book.category, 0)
        author_affinity = authors.get(book.author, 0)
        if not category_affinity and not author_affinity:
            continue

        score = category_affinity * 1.0 + author_affinity * 2.0
        score += min(float(book.purchase_count or 0), 10.0) * 0.2

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

    # Not enough personal signal yet: top it up from the shop's best sellers
    # rather than returning one lonely suggestion.
    seen = {r.book.id for r in scored}
    for extra in _store_wide(session, excluded | seen | owned, count):
        scored.append(extra)
        if len(scored) >= count:
            break
    return scored[:count]


def _store_wide(session: Session, excluded: set, count: int,
                note: str = "") -> List[Recommendation]:
    """What the shop sells and shows most, for anyone with no history."""
    sold = func.coalesce(
        select(func.sum(m.OrderItem.quantity))
        .select_from(m.OrderItem)
        .join(m.Order, (m.Order.id == m.OrderItem.order_id)
              & (m.Order.status == "paid"))
        .where(m.OrderItem.book_id == m.Book.id)
        .correlate(m.Book)
        .scalar_subquery(), 0)

    statement = select(m.Book, sold.label("sold")).where(m.Book.stock > 0)
    if excluded:
        statement = statement.where(m.Book.id.not_in(excluded))

    rows = session.execute(
        statement.order_by(sold.desc(), m.Book.browse_count.desc(),
                           m.Book.title).limit(count)
    ).all()

    out = []
    for book, sold_count in rows:
        if note:
            reason = note
        elif sold_count:
            reason = "One of the shop's best sellers (%d sold)." % int(sold_count)
        else:
            reason = "Popular with people browsing the shop."
        out.append(Recommendation(book, reason, float(sold_count or 0)))
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
        select(m.Book).where(m.Book.stock > 0, m.Book.id.not_in(book_ids))
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
