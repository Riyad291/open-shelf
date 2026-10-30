"""Facades so the assistant can read the SQL catalogue unchanged.

``chat.py`` is 683 lines of intent routing, privilege tiering and RAG/LLM
dispatch, and it implements specification items 3, 9, 12 and 13. It was
written against dictionaries from the file-backed store, and it uses exactly
eleven methods across four collaborators:

    db          get_book  get_user  list_users  reviews
    rules       order_lists  price_for  rating_summary
    catalog     all_books  categories  search
    recommender top_for

Rewriting it for a storage change it is indifferent to would risk behaviour
that is already verified against the specification. These facades present the
same eleven names over the relational schema instead, so the assistant is
carried across the migration untouched.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import models as m
from . import catalog as catalog_service
from . import pricing
from . import recommend as recommend_service
from . import reviews as review_service
from .image_catalogue import book_as_dict


def user_as_dict(user: Optional[m.User], session: Session) -> Optional[dict]:
    """The account shape the assistant expects.

    ``purchase_history`` is materialised from the orders table because the
    assistant answers "what have I bought?" from it. It is a list of the same
    entries the old store kept.
    """
    if user is None:
        return None

    history = session.execute(
        select(m.OrderItem.book_id, m.OrderItem.unit_price,
               m.OrderItem.title_at_purchase, m.Order.placed_at)
        .select_from(m.OrderItem)
        .join(m.Order, (m.Order.id == m.OrderItem.order_id)
              & (m.Order.status == "paid"))
        .where(m.Order.user_id == user.id)
        .order_by(m.Order.id.desc())
    ).all()

    return {
        "user_id": str(user.id),
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "balance": float(user.balance),
        "warnings": user.warnings,
        "suspended": user.suspended,
        "vip_ineligible": user.vip_ineligible,
        "purchase_count": int(user.purchase_count or 0),
        "total_spent": float(user.total_spent or 0),
        "total_fines": float(user.total_fines),
        "purchase_history": [
            {"book_id": str(row.book_id), "price_paid": float(row.unit_price),
             "title": row.title_at_purchase,
             "at": row.placed_at.isoformat() if row.placed_at else ""}
            for row in history
        ],
    }


class StoreFacade:
    """``db`` as the assistant knows it."""

    def __init__(self, session: Session):
        self._session = session

    def get_book(self, book_id) -> Optional[dict]:
        try:
            key = int(str(book_id).lstrip("b") or 0)
        except (TypeError, ValueError):
            return None
        book = self._session.get(m.Book, key)
        return book_as_dict(book) if book else None

    def get_user(self, user_id) -> Optional[dict]:
        try:
            key = int(str(user_id).lstrip("u") or 0)
        except (TypeError, ValueError):
            return None
        user = self._session.get(m.User, key)
        return user_as_dict(user, self._session) if user else None

    def list_users(self, role: Optional[str] = None) -> List[dict]:
        statement = select(m.User)
        if role:
            statement = statement.where(m.User.role == role)
        return [user_as_dict(u, self._session)
                for u in self._session.execute(statement).scalars()]

    @property
    def reviews(self) -> List[dict]:
        rows = self._session.execute(
            select(m.Review).options(selectinload(m.Review.user))
        ).scalars().all()
        return [{
            "review_id": str(r.id),
            "book_id": str(r.book_id),
            "user_id": str(r.user_id) if r.user_id else "",
            "stars": r.stars,
            "comment": r.comment,
            "raw_comment": r.raw_comment,
            "taboo_hits": r.taboo_hits,
            "blocked": r.blocked,
            "verified_purchase": r.verified_purchase,
            "weight": float(r.weight),
        } for r in rows]


class _Summary:
    """``rating_summary`` returns an object with attributes, not a dict."""

    __slots__ = ("count", "weighted_average", "plain_average")

    def __init__(self, count, weighted, plain):
        self.count = count
        self.weighted_average = weighted if weighted is not None else 0.0
        self.plain_average = plain if plain is not None else 0.0


class RulesFacade:
    def __init__(self, session: Session, config):
        self._session = session
        self._config = config

    def rating_summary(self, book_id) -> _Summary:
        try:
            key = int(str(book_id).lstrip("b") or 0)
        except (TypeError, ValueError):
            return _Summary(0, 0.0, 0.0)
        data = review_service.rating_summary(self._session, key)
        return _Summary(data["count"], data["weighted_average"],
                        data["plain_average"])

    def price_for(self, user, book) -> float:
        """The assistant passes dicts, so the real book row is fetched to
        price it -- the discount must match what checkout charges."""
        try:
            key = int(str(book.get("book_id", "")).lstrip("b") or 0)
        except (TypeError, ValueError):
            return float(book.get("price", 0))

        row = self._session.get(m.Book, key)
        if row is None:
            return float(book.get("price", 0))

        account = None
        if user and user.get("user_id"):
            try:
                account = self._session.get(m.User, int(user["user_id"]))
            except (TypeError, ValueError):
                account = None
        return float(pricing.price_for(account, row, self._config.vip_discount))

    def order_lists(self) -> dict:
        lists = review_service.order_lists(self._session, self._config)

        def shape(entries):
            out = []
            for entry in entries:
                book = self._session.get(m.Book, entry["book_id"])
                out.append({
                    "book": book_as_dict(book) if book else
                    {"book_id": str(entry["book_id"]), "title": entry["title"]},
                    "count": entry["count"],
                    "average": entry["weighted_average"],
                    "plain_average": entry["plain_average"],
                })
            return out

        return {"no_order": shape(lists["no_order"]),
                "to_order": shape(lists["to_order"])}


class CatalogFacade:
    def __init__(self, session: Session, files):
        self._session = session
        self._files = files

    def all_books(self) -> List[dict]:
        books = self._session.execute(
            select(m.Book).options(selectinload(m.Book.figures))
            .order_by(m.Book.title)
        ).scalars().all()
        return [book_as_dict(b) for b in books]

    def categories(self) -> List[tuple]:
        from sqlalchemy import func
        rows = self._session.execute(
            select(m.Book.category, func.count(m.Book.id))
            .group_by(m.Book.category).order_by(m.Book.category)
        ).all()
        return [(row[0], int(row[1])) for row in rows]

    def search(self, query: str, limit: int = 20) -> List:
        hits = catalog_service.search(self._session, self._files.data_dir,
                                      query, limit)

        class _Hit:
            __slots__ = ("book", "score", "matched", "where")

            def __init__(self, hit):
                self.book = book_as_dict(hit.book)
                self.score = hit.score
                self.matched = hit.matched
                self.where = hit.where

        return [_Hit(h) for h in hits]


class RecommenderFacade:
    def __init__(self, session: Session, config):
        self._session = session
        self._config = config

    def top_for(self, user, count: int = 3) -> List:
        account = None
        if user and user.get("user_id"):
            try:
                account = self._session.get(m.User, int(user["user_id"]))
            except (TypeError, ValueError):
                account = None

        picks = recommend_service.top_for(self._session, account,
                                          self._config, count)

        class _Pick:
            __slots__ = ("book", "reason", "score")

            def __init__(self, rec):
                self.book = book_as_dict(rec.book)
                self.reason = rec.reason
                self.score = rec.score

        return [_Pick(p) for p in picks]


class FinanceFacade:
    """The two finance texts the assistant prints, computed in SQL.

    ``ChatService`` builds its own ``FinanceService(storage)`` in __init__,
    and that class iterates ``storage.transactions``, ``.users`` and
    ``.books`` -- attributes the file-backed store had and this facade does
    not. The result was not a crash the tests would catch but a polite
    failure *inside* an answer: a super-user asking for the finance report got
    "Something went wrong answering that: 'StoreFacade' object has no
    attribute 'transactions'", with the answer still labelled as successful.
    Specification item 3's headline capability was broken and looked fine.

    Replacing the attribute rather than widening the facade keeps the money
    figures coming from one place -- services/finance.py, the same SQL the
    super-user's Finance screen uses -- so the chat box and the screen cannot
    disagree.
    """

    def __init__(self, session: Session, config):
        self._session = session
        self._config = config

    def report_text(self) -> str:
        from . import finance as finance_service
        report = finance_service.report(self._session)

        lines = [
            "FINANCE REPORT",
            "",
            "Revenue from sales   $%10.2f" % report["revenue_sales"],
            "Revenue from fines   $%10.2f" % report["revenue_fines"],
            "Refunds              $%10.2f" % report["refunds"],
            "TOTAL REVENUE        $%10.2f" % report["total_revenue"],
            "",
            "Paid orders          %11d" % report["order_count"],
            "Average order        $%10.2f" % report["average_order"],
            "Fines issued         %11d" % report["fine_count"],
            "",
            "Customer balances    $%10.2f" % report["customer_liability"],
            "Stock at list price  $%10.2f" % report["inventory_value"],
            "Suspended accounts   %11d" % report["suspended_count"],
            "Accounts in debt     %11d" % report["negative_balance_count"],
        ]

        if report["top_books"]:
            lines += ["", "Best sellers:"]
            for book in report["top_books"]:
                lines.append("  %-34s %3d sold  $%8.2f"
                             % (book["title"][:34], book["units"],
                                book["revenue"]))

        if report["revenue_by_category"]:
            lines += ["", "Revenue by category:"]
            for row in report["revenue_by_category"]:
                if row["revenue"]:
                    lines.append("  %-20s $%8.2f  (%.1f%%)"
                                 % (row["category"], row["revenue"],
                                    row["pct_of_revenue"]))
        return "\n".join(lines)

    def user_statement(self, user: dict) -> str:
        from sqlalchemy import select as _select

        try:
            account = self._session.get(m.User, int(user.get("user_id", 0)))
        except (TypeError, ValueError):
            account = None
        if account is None:
            return "No such account."

        lines = [
            "Account statement for %s" % (account.display_name
                                          or account.username),
            "Balance            $%8.2f" % account.balance,
            "Lifetime spend     $%8.2f" % (account.total_spent or 0),
            "Fines paid         $%8.2f" % account.total_fines,
            "Purchases          %8d" % int(account.purchase_count or 0),
            "Warnings           %8d" % account.warnings,
        ]

        movements = self._session.execute(
            _select(m.Transaction)
            .where(m.Transaction.user_id == account.id)
            .order_by(m.Transaction.id.desc())
            .limit(8)
        ).scalars().all()

        if movements:
            lines += ["", "Recent activity:"]
            for txn in movements:
                book = (self._session.get(m.Book, txn.book_id)
                        if txn.book_id else None)
                label = book.title if book else (txn.note or "")
                lines.append("  %-10s %-26s $%7.2f  %s"
                             % (txn.kind, str(label)[:26], abs(txn.amount),
                                txn.created_at.date().isoformat()))
        return "\n".join(lines)


def build_chat_service(session: Session, config, files, retriever, llm):
    """The assistant, wired to SQL through the facades above."""
    from ..chat import ChatService
    service = ChatService(
        StoreFacade(session), config, RulesFacade(session, config),
        CatalogFacade(session, files), RecommenderFacade(session, config),
        FinanceFacade(session, config), retriever, llm,
    )
    return service
