"""Ratings, comments, moderation and the two order lists.

Reviews and moderation, as a free platform reads it: any user may comment; a
rating from a trusted contributor counts double; taboo words are masked; more
than three of them blocks the comment and earns a warning; more than two
warnings suspends a member or demotes a trusted contributor.

Trust is earned here too. A member whose reviews pass moderation often enough
is promoted (``accounts.maybe_promote``), which is what the old paying-customer
tier became once every book was free.

The taboo screening itself is unchanged -- ``moderation.screen_comment`` is
pure text in, text out, so it did not care that the store underneath moved.
"""

from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import models as m
from ..moderation import screen_comment
from . import accounts


class ReviewError(Exception):
    pass


class ReviewOutcome:
    def __init__(self, review: Optional[m.Review], message: str, *,
                 ok: bool = True, blocked: bool = False, taboo_hits: int = 0,
                 warning_issued: bool = False, warnings_total: int = 0,
                 suspended: bool = False, demoted: bool = False):
        self.review = review
        self.message = message
        self.ok = ok
        self.blocked = blocked
        self.taboo_hits = taboo_hits
        self.warning_issued = warning_issued
        self.warnings_total = warnings_total
        self.suspended = suspended
        self.demoted = demoted


def accepted_review_count(session: Session, user: m.User) -> int:
    """How many of this user's reviews passed moderation. One aggregate."""
    return int(session.execute(
        select(func.count(m.Review.id)).where(
            m.Review.user_id == user.id, m.Review.blocked.is_(False))
    ).scalar_one())


def is_trusted(user: Optional[m.User]) -> bool:
    """Trusted contributors and administrators have ratings that count double."""
    return user is not None and user.role in (m.VIP, m.SUPER_USER)


def already_reviewed(session: Session, user: m.User, book_id: int) -> bool:
    return bool(session.execute(
        select(func.count(m.Review.id)).where(
            m.Review.user_id == user.id, m.Review.book_id == book_id)
    ).scalar_one())


class Screening:
    """The result of screening a comment, including what it cost the author.

    Posting a comment and editing one are the same act as far as moderation
    is concerned, so they go through here rather than each calling
    ``screen_comment`` and deciding for themselves what to do about it.
    """

    def __init__(self, screened, *, warning_issued: bool = False,
                 suspended: bool = False, demoted: bool = False,
                 warnings_total: int = 0):
        self.screened = screened
        self.warning_issued = warning_issued
        self.suspended = suspended
        self.demoted = demoted
        self.warnings_total = warnings_total

    @property
    def blocked(self) -> bool:
        return self.screened.blocked

    @property
    def hits(self) -> int:
        return self.screened.hits


def screen_or_warn(session: Session, user: Optional[m.User], comment: str,
                   config, context: str) -> Screening:
    """Screen a comment and apply the warning ladder if it is blocked.

    ``context`` is the human-readable thing being commented on, used in the
    warning's recorded reason.

    This exists because the edit endpoint used to screen a comment itself and
    simply refuse a blocked one. That made the ladder in Reviews and moderation:
    avoidable: post one clean review, then edit it with taboo words as often
    as you like -- blocked every time, warned never. Measured before the fix:
    five blocked edits produced zero warnings, where a single blocked post
    produced one.
    """
    screened = screen_comment(comment, config.taboo_words,
                              config.max_taboo_before_block)

    if not screened.blocked:
        return Screening(screened)

    # A visitor has no account to carry a warning. The refusal is logged
    # instead, so the moderation report still sees it.
    if user is None:
        session.add(m.Event(
            kind="blocked_visitor_comment", user_id=None,
            detail="%d taboo words on %s" % (screened.hits, context)))
        session.flush()
        return Screening(screened)

    suspended, demoted = accounts.apply_warning(
        session, user,
        "blocked comment on '%s' (%d taboo words)" % (context, screened.hits),
        config)

    return Screening(screened, warning_issued=True, suspended=suspended,
                     demoted=demoted, warnings_total=user.warnings)


def submit(session: Session, user: Optional[m.User], book: m.Book,
           stars: int, comment: str, config) -> ReviewOutcome:
    try:
        stars = int(stars)
    except (TypeError, ValueError):
        raise ReviewError("A rating must be a whole number of stars.")
    if not 1 <= stars <= 5:
        raise ReviewError("A rating must be between 1 and 5 stars.")

    # Eligibility is settled before the comment is screened, so that a
    # refused author is never also warned for the same attempt.
    if user is None:
        if not config.flag("visitors_may_review"):
            raise ReviewError(
                "Please sign in to post a rating -- reviews are tied to an "
                "account so that moderation warnings can be applied.")
    else:
        allowed, why = accounts.can_sign_in(user)
        if not allowed:
            raise ReviewError(why)
        if already_reviewed(session, user, book.id):
            raise ReviewError("You have already reviewed this book.")

    outcome = screen_or_warn(session, user, comment, config, book.title)
    screened = outcome.screened

    # -- a visitor, with no account to carry a warning --------------------
    if user is None:
        if screened.blocked:
            # screen_or_warn has already logged the refusal.
            return ReviewOutcome(
                None,
                "That comment was blocked: it contains %d taboo words."
                % screened.hits,
                ok=False, blocked=True, taboo_hits=screened.hits)

        review = m.Review(
            book_id=book.id, user_id=None, role_at_time=m.VISITOR,
            stars=stars, raw_comment=screened.original,
            comment=screened.masked, taboo_hits=screened.hits,
            blocked=False, trusted_reviewer=False,
            weight=Decimal(str(config.normal_weight)),
        )
        session.add(review)
        session.flush()
        message = "Rating posted."
        if screened.hits:
            message += " %d taboo word(s) were masked." % screened.hits
        return ReviewOutcome(review, message, taboo_hits=screened.hits)

    # -- a signed-in member -----------------------------------------------
    trusted = is_trusted(user)
    weight = Decimal(str(config.trusted_weight if trusted
                         else config.normal_weight))

    review = m.Review(
        book_id=book.id, user_id=user.id, role_at_time=user.role,
        stars=stars, raw_comment=screened.original, comment=screened.masked,
        taboo_hits=screened.hits, blocked=screened.blocked,
        trusted_reviewer=trusted, weight=weight,
    )
    session.add(review)
    session.flush()

    if screened.blocked:
        message = ("Your comment was blocked for containing %d taboo words. "
                   "You have received a warning (%d of %d allowed)."
                   % (screened.hits, user.warnings, config.warning_limit))
        if outcome.suspended:
            message += " Your account has been suspended."
        elif outcome.demoted:
            message += (" Your trusted-contributor status has been revoked; "
                        "you are now a member.")

        return ReviewOutcome(
            review, message, ok=False, blocked=True,
            taboo_hits=screened.hits, warning_issued=True,
            warnings_total=user.warnings, suspended=outcome.suspended,
            demoted=outcome.demoted)

    message = "Review posted."
    if screened.hits:
        message += " %d taboo word(s) were masked." % screened.hits
    if trusted:
        message += " Your rating counts double as a trusted contributor."
    if accounts.maybe_promote(session, user, config):
        message += (" You are now a trusted contributor: your ratings count "
                    "double from here.")
    return ReviewOutcome(review, message, taboo_hits=screened.hits)


# ------------------------------------------------------------- aggregates


def _rating_columns():
    """The weighted and plain averages, as SQL.

    Both are needed: the feature list classifies by the weighted average but
    counts ratings unweighted, and mixing the two is the kind of error that
    puts the wrong book on the no-order list.
    """
    return (
        func.count(m.Review.id).label("rating_count"),
        func.avg(m.Review.stars).label("plain_average"),
        (func.sum(m.Review.stars * m.Review.weight)
         / func.nullif(func.sum(m.Review.weight), 0)).label("weighted_average"),
    )


def rating_summary(session: Session, book_id: int) -> dict:
    count, plain, weighted = _rating_columns()
    row = session.execute(
        select(count, plain, weighted)
        .where(m.Review.book_id == book_id, m.Review.blocked.is_(False))
    ).one()

    return {
        "count": int(row.rating_count or 0),
        "plain_average": round(float(row.plain_average), 3)
        if row.plain_average is not None else None,
        "weighted_average": round(float(row.weighted_average), 3)
        if row.weighted_average is not None else None,
    }


def list_status(summary: dict, config) -> str:
    """Reviews and moderation: more than three ratings puts a book on a list."""
    if summary["count"] <= config.min_ratings_for_list:
        return "normal"

    average = (summary["weighted_average"]
               if config.flag("order_lists_use_weighted_average")
               else summary["plain_average"])
    if average is None:
        return "normal"
    if average < config.no_order_max_avg:
        return "no-order"
    if average > config.to_order_min_avg:
        return "to-order"
    return "normal"


def order_lists(session: Session, config) -> dict:
    """Both lists, in two queries rather than one per book.

    The naive version asks for every book's rating summary in a loop. With a
    thousand titles that is a thousand round trips to build one admin screen.
    """
    count, plain, weighted = _rating_columns()
    rows = session.execute(
        select(m.Book.id, m.Book.title, m.Book.author, m.Book.category,
               count, plain, weighted)
        .join(m.Review, (m.Review.book_id == m.Book.id)
              & (m.Review.blocked.is_(False)))
        .group_by(m.Book.id, m.Book.title, m.Book.author, m.Book.category)
        .having(func.count(m.Review.id) > config.min_ratings_for_list)
    ).all()

    no_order, to_order = [], []
    for row in rows:
        summary = {
            "count": int(row.rating_count),
            "plain_average": round(float(row.plain_average), 3),
            "weighted_average": round(float(row.weighted_average), 3),
        }
        entry = {
            "book_id": row.id, "title": row.title, "author": row.author,
            "category": row.category, **summary,
        }
        status = list_status(summary, config)
        if status == "no-order":
            no_order.append(entry)
        elif status == "to-order":
            to_order.append(entry)

    no_order.sort(key=lambda e: e["weighted_average"])
    to_order.sort(key=lambda e: -e["weighted_average"])
    return {"no_order": no_order, "to_order": to_order}


def for_book(session: Session, book_id: int,
             include_blocked: bool = False) -> List[m.Review]:
    statement = select(m.Review).where(m.Review.book_id == book_id)
    if not include_blocked:
        statement = statement.where(m.Review.blocked.is_(False))
    return list(session.execute(
        statement.order_by(m.Review.created_at.desc())).scalars())
