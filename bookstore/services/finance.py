"""The super-user's finance report and the shop's analytics, in SQL.

This file replaces two things from the file-backed design: the Python loops
that summed the ledger in memory, and ``analytics.py``, which projected the
text files into a throwaway SQLite database so that reports could be written
in SQL. The projection existed only because the records were not relational.
They are now, so it is gone -- the reports query the real schema.

Every figure is a sum over the ``transactions`` table or the orders it
references, which is why the report can never disagree with the accounts.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List

from sqlalchemy import Numeric, case, cast, func, select
from sqlalchemy.orm import Session

from ..db import models as m
from . import pricing


ZERO = Decimal("0.00")


def _money(value) -> Decimal:
    return pricing.money(value or 0)


def _sum_by_kind(session: Session) -> Dict[str, Decimal]:
    rows = session.execute(
        select(m.Transaction.kind, func.coalesce(func.sum(m.Transaction.amount), 0),
               func.count(m.Transaction.id))
        .group_by(m.Transaction.kind)
    ).all()
    return {row[0]: (_money(row[1]), int(row[2])) for row in rows}


def report(session: Session) -> Dict[str, Any]:
    """Every figure the super-user is shown, in a handful of queries."""
    by_kind = _sum_by_kind(session)
    sales, sale_count = by_kind.get("purchase", (ZERO, 0))
    fines, fine_count = by_kind.get("fine", (ZERO, 0))
    refunds, _ = by_kind.get("refund", (ZERO, 0))
    deposits, _ = by_kind.get("deposit", (ZERO, 0))

    liability = _money(session.execute(
        select(func.coalesce(func.sum(m.User.balance), 0))
        .where(m.User.role != m.SUPER_USER)
    ).scalar_one())

    inventory = _money(session.execute(
        select(func.coalesce(
            func.sum(cast(m.Book.stock, Numeric(10, 2)) * m.Book.price), 0))
    ).scalar_one())

    counts = dict(session.execute(
        select(m.User.role, func.count(m.User.id)).group_by(m.User.role)
    ).all())

    suspended = session.execute(
        select(func.count(m.User.id)).where(m.User.suspended.is_(True))
    ).scalar_one()

    negative = session.execute(
        select(func.count(m.User.id)).where(m.User.balance < 0)
    ).scalar_one()

    orders = session.execute(
        select(func.count(m.Order.id)).where(m.Order.status == "paid")
    ).scalar_one()

    total_revenue = _money(sales + fines - refunds)
    average_order = _money(sales / orders) if orders else ZERO

    return {
        "revenue_sales": sales,
        "revenue_fines": fines,
        "refunds": refunds,
        "deposits_taken": deposits,
        "total_revenue": total_revenue,
        "purchase_count": sale_count,
        "fine_count": fine_count,
        "order_count": int(orders),
        "average_order": average_order,
        "customer_liability": liability,
        "inventory_value": inventory,
        "user_counts": {k: int(v) for k, v in counts.items()},
        "suspended_count": int(suspended),
        "negative_balance_count": int(negative),
        "top_books": best_sellers(session, limit=6),
        "revenue_by_category": revenue_by_category(session),
    }


# ------------------------------------------------------------- analytics
#
# The queries that used to live in analytics.py, now against the real schema.


def revenue_by_month(session: Session) -> List[Dict[str, Any]]:
    """Money in per month with a running total -- a window over a CTE."""
    month = func.to_char(m.Transaction.created_at, "YYYY-MM")
    if session.get_bind().dialect.name == "sqlite":
        month = func.strftime("%Y-%m", m.Transaction.created_at)

    monthly = (
        select(
            month.label("month"),
            func.coalesce(func.sum(case(
                (m.Transaction.kind == "purchase", m.Transaction.amount),
                else_=0)), 0).label("sales"),
            func.coalesce(func.sum(case(
                (m.Transaction.kind == "fine", m.Transaction.amount),
                else_=0)), 0).label("fines"),
            func.coalesce(func.sum(case(
                (m.Transaction.kind == "refund", m.Transaction.amount),
                else_=0)), 0).label("refunds"),
            func.count(m.Transaction.id).label("movements"),
        )
        .group_by(month)
        .cte("monthly")
    )

    revenue = (monthly.c.sales + monthly.c.fines - monthly.c.refunds)
    rows = session.execute(
        select(
            monthly.c.month, monthly.c.sales, monthly.c.fines,
            monthly.c.refunds, revenue.label("revenue"), monthly.c.movements,
            func.sum(revenue).over(order_by=monthly.c.month)
            .label("running_total"),
        ).order_by(monthly.c.month)
    ).all()

    return [{
        "month": row.month,
        "sales": _money(row.sales),
        "fines": _money(row.fines),
        "refunds": _money(row.refunds),
        "revenue": _money(row.revenue),
        "movements": int(row.movements),
        "running_total": _money(row.running_total),
    } for row in rows]


def best_sellers(session: Session, limit: int = 10) -> List[Dict[str, Any]]:
    """Units and revenue per title, ranked within each category."""
    rank = func.rank().over(
        partition_by=m.Book.category,
        order_by=func.sum(m.OrderItem.quantity).desc(),
    ).label("rank_in_category")

    rows = session.execute(
        select(
            m.Book.id, m.Book.title, m.Book.author, m.Book.category,
            func.sum(m.OrderItem.quantity).label("units"),
            func.sum(m.OrderItem.line_total).label("revenue"),
            rank,
        )
        .select_from(m.OrderItem)
        .join(m.Order, (m.Order.id == m.OrderItem.order_id)
              & (m.Order.status == "paid"))
        .join(m.Book, m.Book.id == m.OrderItem.book_id)
        .group_by(m.Book.id, m.Book.title, m.Book.author, m.Book.category)
        .order_by(func.sum(m.OrderItem.quantity).desc())
        .limit(limit)
    ).all()

    return [{
        "book_id": row.id, "title": row.title, "author": row.author,
        "category": row.category, "units": int(row.units),
        "revenue": _money(row.revenue),
        "rank_in_category": int(row.rank_in_category),
    } for row in rows]


def revenue_by_category(session: Session) -> List[Dict[str, Any]]:
    """A LEFT JOIN on purpose: a category that has sold nothing is a finding,
    and an inner join would hide exactly the rows worth looking at."""
    total = session.execute(
        select(func.coalesce(func.sum(m.OrderItem.line_total), 0))
        .select_from(m.OrderItem)
        .join(m.Order, (m.Order.id == m.OrderItem.order_id)
              & (m.Order.status == "paid"))
    ).scalar_one() or 0

    rows = session.execute(
        select(
            m.Book.category,
            func.count(func.distinct(m.Book.id)).label("titles"),
            func.coalesce(func.sum(m.Book.stock), 0).label("copies_in_stock"),
            func.coalesce(func.sum(m.OrderItem.quantity), 0).label("units_sold"),
            func.coalesce(func.sum(m.OrderItem.line_total), 0).label("revenue"),
        )
        .select_from(m.Book)
        .outerjoin(m.OrderItem, m.OrderItem.book_id == m.Book.id)
        .outerjoin(m.Order, (m.Order.id == m.OrderItem.order_id)
                   & (m.Order.status == "paid"))
        .group_by(m.Book.category)
        .order_by(func.coalesce(func.sum(m.OrderItem.line_total), 0).desc())
    ).all()

    out = []
    for row in rows:
        revenue = _money(row.revenue)
        out.append({
            "category": row.category,
            "titles": int(row.titles),
            "copies_in_stock": int(row.copies_in_stock or 0),
            "units_sold": int(row.units_sold or 0),
            "revenue": revenue,
            "pct_of_revenue": round(float(revenue) / float(total) * 100, 1)
            if total else 0.0,
        })
    return out


def customer_value(session: Session, limit: int = 10) -> List[Dict[str, Any]]:
    rows = session.execute(
        select(m.User)
        .where(m.User.role != m.SUPER_USER)
        .order_by(m.User.total_spent.desc())
        .limit(limit)
    ).scalars().all()

    return [{
        "username": u.username,
        "display_name": u.display_name,
        "role": u.role,
        "balance": _money(u.balance),
        "purchase_count": int(u.purchase_count or 0),
        "total_spent": _money(u.total_spent),
        "total_fines": _money(u.total_fines),
        "warnings": u.warnings,
        "suspended": "yes" if u.suspended else "no",
    } for u in rows]


def dead_stock(session: Session) -> List[Dict[str, Any]]:
    """Browsed but never bought: the shelf's quiet failures."""
    rows = session.execute(
        select(m.Book)
        .where(m.Book.purchase_count == 0)
        .order_by(m.Book.browse_count.desc(), m.Book.stock.desc())
    ).scalars().all()

    return [{
        "book_id": b.id, "title": b.title, "author": b.author,
        "category": b.category, "price": _money(b.price),
        "stock": b.stock, "browse_count": b.browse_count,
    } for b in rows]


def moderation(session: Session) -> List[Dict[str, Any]]:
    rows = session.execute(
        select(
            m.User.username, m.User.role, m.User.warnings, m.User.suspended,
            func.count(m.Review.id).label("comments"),
            func.coalesce(func.sum(case((m.Review.blocked.is_(True), 1),
                                        else_=0)), 0).label("blocked"),
            func.coalesce(func.sum(m.Review.taboo_hits), 0).label("taboo_words"),
        )
        .select_from(m.User)
        .join(m.Review, m.Review.user_id == m.User.id)
        .group_by(m.User.id, m.User.username, m.User.role, m.User.warnings,
                  m.User.suspended)
        .having(func.coalesce(func.sum(m.Review.taboo_hits), 0) > 0)
        .order_by(func.coalesce(func.sum(m.Review.taboo_hits), 0).desc())
    ).all()

    return [{
        "username": row.username, "role": row.role, "warnings": row.warnings,
        "suspended": "yes" if row.suspended else "no",
        "comments": int(row.comments), "blocked": int(row.blocked),
        "taboo_words": int(row.taboo_words),
        "pct_blocked": round(int(row.blocked) / int(row.comments) * 100, 1)
        if row.comments else 0.0,
    } for row in rows]


def analytics(session: Session) -> Dict[str, List[Dict[str, Any]]]:
    return {
        "revenue_by_month": revenue_by_month(session),
        "category_performance": revenue_by_category(session),
        "best_sellers": best_sellers(session),
        "customer_value": customer_value(session),
        "moderation": moderation(session),
        "dead_stock": dead_stock(session),
    }
