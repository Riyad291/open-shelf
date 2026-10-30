"""Browsing, facets and keyword search, over the relational catalogue.

Filtering and counting are SQL -- that is what a database is for. Ranking is
not: the TF-IDF scoring is the same code the file-backed version used, and it
reads from the ORM rows instead of dictionaries. Pushing relevance ranking
into SQL would mean full-text search configuration that differs between
PostgreSQL, MySQL and SQLite, and this project supports all three.

The index is built once per process and refreshed when the catalogue changes,
rather than on every query.
"""

from __future__ import annotations

import math
import os
import threading
from collections import Counter
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..catalog import TEXT_SAMPLE_CHARS, tokenize   # unchanged tokenizer
from ..db import models as m


class SearchHit:
    __slots__ = ("book", "score", "matched", "where")

    def __init__(self, book: m.Book, score: float, matched: List[str],
                 where: str):
        self.book = book
        self.score = score
        self.matched = matched
        self.where = where


# ------------------------------------------------------------- browsing


def list_books(session: Session, *, category: Optional[str] = None,
               author: Optional[str] = None, decade: Optional[str] = None,
               limit: int = 60, offset: int = 0,
               sort: str = "title") -> Tuple[List[m.Book], int]:
    """A page of the catalogue, plus the total that matched.

    The total comes from a separate COUNT rather than len() of the rows,
    because the rows are a page and the pager needs the whole figure.
    """
    statement = select(m.Book)
    counter = select(func.count(m.Book.id))

    for clause in _filters(category, author, decade):
        statement = statement.where(clause)
        counter = counter.where(clause)

    order = {
        "title": m.Book.title,
        "author": m.Book.author,
        "year": m.Book.year,
        "newest": m.Book.id.desc(),
        "popular": m.Book.browse_count.desc(),
    }.get(sort, m.Book.title)

    books = list(session.execute(
        statement.order_by(order).limit(min(limit, 200)).offset(max(offset, 0))
    ).scalars())
    total = session.execute(counter).scalar_one()
    return books, int(total)


def _filters(category, author, decade):
    clauses = []
    if category:
        clauses.append(func.lower(m.Book.category) == category.strip().lower())
    if author:
        clauses.append(func.lower(m.Book.author) == author.strip().lower())
    if decade:
        digits = "".join(ch for ch in str(decade) if ch.isdigit())
        if digits:
            start = int(digits[:4])
            clauses.append(m.Book.year >= start)
            clauses.append(m.Book.year < start + 10)
    return clauses


def facets(session: Session) -> dict:
    """Specification item 5: browse by category, author and time period.

    Three grouped queries, not a Python walk over every book.
    """
    categories = session.execute(
        select(m.Book.category, func.count(m.Book.id))
        .group_by(m.Book.category).order_by(m.Book.category)
    ).all()

    authors = session.execute(
        select(m.Book.author, func.count(m.Book.id))
        .group_by(m.Book.author).order_by(m.Book.author)
    ).all()

    # The decade a book belongs to. Floor division, not `/`: SQLAlchemy 2
    # renders `/` as true division, so 1859 / 10 is 185.9, and casting that to
    # an integer *rounds* to 186 -- putting an 1859 book in the 1860s. The
    # facet then disagreed with the filter, which uses a range: the sidebar
    # said "1860s (2)" and clicking "1850s" returned three books.
    decade_expr = (m.Book.year // 10) * 10
    decades = session.execute(
        select(decade_expr, func.count(m.Book.id))
        .where(m.Book.year.is_not(None))
        .group_by(decade_expr).order_by(decade_expr)
    ).all()

    return {
        "categories": [{"value": c, "count": int(n)} for c, n in categories],
        "authors": [{"value": a, "count": int(n)} for a, n in authors],
        "decades": [{"value": "%ds" % int(d), "count": int(n)}
                    for d, n in decades],
    }


def record_browse(session: Session, user: Optional[m.User],
                  book: m.Book) -> None:
    book.browse_count += 1
    if user is not None:
        session.add(m.BrowseHistory(user_id=user.id, book_id=book.id))
    session.flush()


# --------------------------------------------------------------- search


class SearchIndex:
    """An inverted index over the catalogue, rebuilt when it changes.

    Keyed by a cheap signature -- the number of books and the highest id --
    so adding or deleting a book invalidates it without a timestamp column or
    a cache-busting call from every write path.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._postings: Dict[int, Counter] = {}
        self._doc_freq: Counter = Counter()
        self._signature: Optional[Tuple[int, int]] = None

    def _current_signature(self, session: Session) -> Tuple[int, int]:
        row = session.execute(
            select(func.count(m.Book.id), func.coalesce(func.max(m.Book.id), 0))
        ).one()
        return int(row[0]), int(row[1])

    def _book_text(self, book: m.Book, data_dir: str) -> str:
        parts = [book.title, book.title,        # the title counts double
                 book.author, book.category, book.description or ""]

        path = book.text_path or ""
        if path:
            full = path if os.path.isabs(path) else os.path.join(
                os.path.dirname(data_dir), path)
            if not os.path.exists(full):
                full = os.path.join(data_dir, "books", os.path.basename(path))
            if os.path.exists(full):
                try:
                    with open(full, "r", encoding="utf-8",
                              errors="ignore") as handle:
                        parts.append(handle.read(TEXT_SAMPLE_CHARS))
                except OSError:
                    pass
        return " ".join(parts)

    def ensure(self, session: Session, data_dir: str) -> None:
        signature = self._current_signature(session)
        with self._lock:
            if signature == self._signature:
                return

            self._postings.clear()
            self._doc_freq.clear()
            for book in session.execute(select(m.Book)).scalars():
                counts = Counter(tokenize(self._book_text(book, data_dir)))
                self._postings[book.id] = counts
                for term in counts:
                    self._doc_freq[term] += 1
            self._signature = signature

    def search(self, session: Session, data_dir: str, query: str,
               limit: int = 20) -> List[SearchHit]:
        terms = tokenize(query)
        if not terms:
            return []

        self.ensure(session, data_dir)
        total_docs = max(1, len(self._postings))
        needle = query.strip().lower()

        candidate_ids = {
            book_id for book_id, counts in self._postings.items()
            if any(counts.get(term) for term in set(terms))
        }
        if not candidate_ids:
            return []

        books = session.execute(
            select(m.Book).where(m.Book.id.in_(candidate_ids))
        ).scalars().all()

        hits: List[SearchHit] = []
        for book in books:
            counts = self._postings.get(book.id)
            score = 0.0
            matched: List[str] = []
            for term in set(terms):
                tf = counts.get(term, 0)
                if not tf:
                    continue
                idf = math.log((total_docs + 1)
                               / (1 + self._doc_freq.get(term, 0))) + 1.0
                score += (1.0 + math.log(tf)) * idf
                matched.append(term)

            # An exact phrase in the title is a far stronger signal than any
            # amount of term overlap in the body text.
            if needle in book.title.lower():
                score += 8.0
            if needle in book.author.lower():
                score += 5.0

            if score > 0:
                haystack = (book.title + " " + book.author).lower()
                where = ("title/author"
                         if any(t in haystack for t in matched)
                         else "description/text")
                hits.append(SearchHit(book, round(score, 3), sorted(matched),
                                      where))

        hits.sort(key=lambda h: (-h.score, h.book.title))
        return hits[:limit]


_index = SearchIndex()


def search(session: Session, data_dir: str, query: str,
           limit: int = 20) -> List[SearchHit]:
    return _index.search(session, data_dir, query, limit)


def reset_index() -> None:
    """Used by tests, which build a different catalogue per case."""
    global _index
    _index = SearchIndex()
