"""Facades so the assistant can read the SQL catalogue unchanged.

``chat.py`` is the intent routing, privilege tiering and RAG/LLM dispatch
behind specification items 3, 9, 12 and 13. It was written against
dictionaries from the file-backed store, and it uses a small set of methods
across five collaborators:

    db          get_book  get_user  list_users  reviews
    rules       order_lists  rating_summary
    catalog     all_books  categories  search
    recommender top_for
    insights    report_text  user_statement

Rewriting it for a storage change it is indifferent to would risk behaviour
that is already verified against the specification. These facades present
the same names over the relational schema instead (``bookstore/ports.py``
writes the contract down).
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import models as m
from . import catalog as catalog_service
from . import recommend as recommend_service
from . import reviews as review_service
from .image_catalogue import book_as_dict


def user_as_dict(user: Optional[m.User], session: Session) -> Optional[dict]:
    """The account shape the assistant expects."""
    if user is None:
        return None
    return {
        "user_id": str(user.id),
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "warnings": user.warnings,
        "suspended": user.suspended,
        "promotion_blocked": user.promotion_blocked,
        "review_count": review_service.accepted_review_count(session, user),
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
            "trusted_reviewer": r.trusted_reviewer,
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


class InsightsFacade:
    """The two texts the assistant prints for the administrator and a member.

    Computed by ``services/insights.py``, the same SQL as the administrator's
    screen, so the chat box and the screen cannot disagree.
    """

    def __init__(self, session: Session, config):
        self._session = session
        self._config = config

    def report_text(self) -> str:
        from . import insights
        data = insights.summary(self._session)

        lines = [
            "CATALOGUE",
            "  Live resources       %8d" % data["resources"],
            "  Dead links           %8d" % data["dead_links"],
            "  Times opened         %8d" % data["total_opens"],
            "  Sources              %8d" % data["sources"],
            "  Topics               %8d" % data["topics"],
            "",
            "COMMUNITY",
            "  Accounts             %8d" % data["accounts"],
            "  Trusted contributors %8d" % data["trusted_contributors"],
            "  Suspended            %8d" % data["suspended_accounts"],
            "  Reviews              %8d" % data["reviews"],
            "  Blocked reviews      %8d" % data["blocked_reviews"],
        ]

        last = data["last_import"]
        lines += ["", "LAST IMPORT"]
        if last is None:
            lines.append("  No import has run yet.")
        else:
            lines.append("  %s, %s: %d inserted, %d updated, %d unchanged"
                         % (last["status"], last["started_at"][:16],
                            last["inserted"], last["updated"],
                            last["unchanged"]))

        top = insights.most_opened(self._session, limit=5)
        if top:
            lines += ["", "MOST OPENED BY TOPIC"]
            for row in top:
                lines.append("  %-22s %-34s %4d"
                             % (row["topic"][:22], row["title"][:34],
                                row["opens"]))
        return "\n".join(lines)

    def user_statement(self, user: dict) -> str:
        try:
            account = self._session.get(m.User, int(user.get("user_id", 0)))
        except (TypeError, ValueError):
            account = None
        if account is None:
            return "No such account."

        accepted = review_service.accepted_review_count(self._session,
                                                        account)
        lines = [
            "ACCOUNT: %s" % (account.display_name or account.username),
            "  Standing          %s" % m.role_name(account.role),
            "  Reviews accepted  %d" % accepted,
            "  Warnings          %d of %d allowed"
            % (account.warnings, self._config.warning_limit),
            "  Status            %s"
            % ("suspended" if account.suspended else "active"),
        ]
        if account.role == m.REGISTERED and not account.promotion_blocked:
            left = self._config.trusted_review_threshold - accepted
            if left > 0:
                lines += ["", "%d more accepted review(s) and you become a "
                          "trusted contributor." % left]
        return "\n".join(lines)


def build_chat_service(session: Session, config, files, retriever, llm):
    """The assistant, wired to SQL through the facades above."""
    from ..chat import ChatService
    service = ChatService(
        StoreFacade(session), config, RulesFacade(session, config),
        CatalogFacade(session, files), RecommenderFacade(session, config),
        InsightsFacade(session, config), retriever, llm,
    )
    return service
