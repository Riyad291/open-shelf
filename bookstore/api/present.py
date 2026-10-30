"""Turning ORM rows into the HTTP contract.

One place, because the same book appears in the catalogue, in search
results, on a recommendation and on the shelf -- and a rating that differs
between those screens is a bug a reader notices before anybody else does.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import models as m
from ..services import reviews as review_service
from . import schemas as S
from .deps import ROLE_NAMES


def cover_url(book: m.Book) -> Optional[str]:
    return "/api/books/%d/cover" % book.id if book.cover_path else None


def rating_map(session: Session, book_ids: Iterable[int]) -> dict:
    """Ratings for many books in one query.

    Asking per book is the N+1 that makes a catalogue page crawl; this is the
    reason the catalogue endpoint collects ids first and resolves once.
    """
    ids = list(book_ids)
    if not ids:
        return {}

    rows = session.execute(
        select(
            m.Review.book_id,
            func.count(m.Review.id),
            func.avg(m.Review.stars),
            func.sum(m.Review.stars * m.Review.weight)
            / func.nullif(func.sum(m.Review.weight), 0),
        )
        .where(m.Review.book_id.in_(ids), m.Review.blocked.is_(False))
        .group_by(m.Review.book_id)
    ).all()

    return {
        row[0]: {
            "count": int(row[1]),
            "plain_average": round(float(row[2]), 3) if row[2] is not None else None,
            "weighted_average": round(float(row[3]), 3) if row[3] is not None else None,
        }
        for row in rows
    }


def book_out(book: m.Book, user: Optional[m.User], config,
             rating: Optional[dict] = None) -> S.BookOut:
    rating = rating or {"count": 0, "weighted_average": None}
    status = (review_service.list_status(
        {"count": rating.get("count", 0),
         "plain_average": rating.get("plain_average"),
         "weighted_average": rating.get("weighted_average")}, config)
        if rating.get("count") else "normal")

    return S.BookOut(
        id=book.id,
        title=book.title,
        author=book.author,
        category=book.category,
        year=book.year,
        description=book.description or "",
        cover_url=cover_url(book),
        browse_count=book.browse_count,
        rating=rating.get("weighted_average"),
        rating_count=rating.get("count", 0),
        list_status=status,
    )


def books_out(session: Session, books: List[m.Book], user: Optional[m.User],
              config) -> List[S.BookOut]:
    ratings = rating_map(session, [b.id for b in books])
    return [book_out(b, user, config, ratings.get(b.id)) for b in books]


def review_out(review: m.Review) -> S.ReviewOut:
    if review.user is not None:
        author = review.user.display_name or review.user.username
    else:
        author = "visitor"
    return S.ReviewOut(
        id=review.id,
        book_id=review.book_id,
        stars=review.stars,
        comment=review.comment,
        author=author,
        trusted_reviewer=review.trusted_reviewer,
        weight=review.weight,
        created_at=review.created_at,
    )


def session_out(user: Optional[m.User], config,
                face_samples: int = 0) -> S.SessionOut:
    if user is None:
        return S.SessionOut(
            signed_in=False, role=m.VISITOR,
            role_name=ROLE_NAMES[m.VISITOR])

    return S.SessionOut(
        signed_in=True,
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        role_name=ROLE_NAMES.get(user.role, user.role),
        warnings=user.warnings,
        suspended=user.suspended,
        trusted=user.role in (m.VIP, m.SUPER_USER),
        # One lazy load of this user's reviews, for one user: not an N+1.
        review_count=sum(1 for r in user.reviews if not r.blocked),
        face_enrolled=face_samples > 0,
    )


def account_out(user: m.User, face_samples: int = 0) -> S.AccountOut:
    return S.AccountOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        role_name=ROLE_NAMES.get(user.role, user.role),
        warnings=user.warnings,
        suspended=user.suspended,
        suspend_reason=user.suspend_reason or "",
        face_samples=face_samples,
        created_at=user.created_at,
    )
