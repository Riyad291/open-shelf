"""Turning ORM rows into the HTTP contract.

One place, because the same book appears in the catalogue, in search results,
in the cart, in an order and in a recommendation -- and a price that differs
between those screens is a bug the customer notices before anybody else does.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import models as m
from ..services import pricing, reviews as review_service
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
        price=pricing.money(book.price),
        your_price=pricing.price_for(user, book, config.vip_discount),
        stock=book.stock,
        description=book.description or "",
        cover_url=cover_url(book),
        browse_count=book.browse_count,
        purchase_count=int(book.purchase_count or 0),
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
        verified_purchase=review.verified_purchase,
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
        balance=pricing.money(user.balance),
        warnings=user.warnings,
        suspended=user.suspended,
        purchase_count=int(user.purchase_count or 0),
        total_spent=pricing.money(user.total_spent),
        vip_discount=config.vip_discount if user.role == m.VIP else 0.0,
        face_enrolled=face_samples > 0,
    )


def order_out(order: m.Order) -> S.OrderOut:
    return S.OrderOut(
        id=order.id,
        status=order.status,
        subtotal=order.subtotal,
        discount=order.discount,
        total=order.total,
        discount_rate=order.discount_rate,
        placed_at=order.placed_at,
        items=[S.OrderLineOut(
            book_id=item.book_id,
            title=item.title_at_purchase or (item.book.title if item.book else ""),
            quantity=item.quantity,
            unit_price=item.unit_price,
            line_total=item.line_total,
            cover_url="/api/books/%d/cover" % item.book_id,
        ) for item in order.items],
        payments=[S.PaymentOut(
            id=p.id, method=p.method, amount=p.amount, status=p.status,
            reference=p.reference, created_at=p.created_at,
        ) for p in order.payments],
    )


def account_out(user: m.User, face_samples: int = 0) -> S.AccountOut:
    return S.AccountOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        role_name=ROLE_NAMES.get(user.role, user.role),
        balance=pricing.money(user.balance),
        total_spent=pricing.money(user.total_spent),
        purchase_count=int(user.purchase_count or 0),
        total_fines=pricing.money(user.total_fines),
        warnings=user.warnings,
        suspended=user.suspended,
        suspend_reason=user.suspend_reason or "",
        face_samples=face_samples,
        created_at=user.created_at,
    )
