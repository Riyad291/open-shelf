"""The relational schema.

Two decisions run through the whole file and are worth stating once.

**Constraints are in the database, not only in Python.** Stars must be 1 to
5, a username is unique, an import cannot insert the same resource twice.
Application-level validation is a nicer error message on top of these, not
the thing standing between the data and a wrong row.

**Imported rows take their identity from upstream.** A resource is known by
``(source_id, external_id)``, not by our own id, so re-running an import
finds the row it wrote last time instead of writing another.

This schema had a shop in it -- carts, orders, payments, balances. It was
removed when the project became a free platform; ADR-002 records why, and
commit ``2e6cc23`` keeps the version with the checkout race in it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric,
    DDL, String, Text, UniqueConstraint, event, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


# Roles. Kept as short strings matching the original design's wording
# so that stored data, the API and the rules all speak the same language.
VISITOR = "VS"
REGISTERED = "RC"
VIP = "VC"
SUPER_USER = "SU"

ROLE_NAMES = {
    VISITOR: "Visitor",
    REGISTERED: "Member",
    VIP: "Trusted contributor",
    SUPER_USER: "Administrator",
}

#: Rank, lowest first. "At least this role" is a comparison of ranks.
ROLE_RANK = {VISITOR: 0, REGISTERED: 1, VIP: 2, SUPER_USER: 3}


def role_name(role: str) -> str:
    return ROLE_NAMES.get(role, role)


def role_at_least(role: str, minimum: str) -> bool:
    return ROLE_RANK.get(role, 0) >= ROLE_RANK.get(minimum, 0)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def as_utc(moment: Optional[dt.datetime]) -> Optional[dt.datetime]:
    """Make a datetime read back from the database safe to compare.

    PostgreSQL returns TIMESTAMP WITH TIME ZONE as an aware datetime; SQLite
    has no timezone type and hands back a naive one. Comparing a naive value
    with an aware one raises TypeError, so the same expression worked on
    PostgreSQL and crashed on SQLite -- and refresh, which compares an expiry
    against now, is on the path every signed-in person takes every fifteen
    minutes.

    Values are stored in UTC, so a naive one is UTC with the label missing.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(),
        nullable=False,
    )


# ---------------------------------------------------------------- accounts


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True,
                                          nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), default="")

    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    salt: Mapped[str] = mapped_column(String(64), nullable=False)

    role: Mapped[str] = mapped_column(String(2), default=REGISTERED,
                                      index=True, nullable=False)

    warnings: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    suspended: Mapped[bool] = mapped_column(Boolean, default=False,
                                            nullable=False)
    suspend_reason: Mapped[str] = mapped_column(String(255), default="")

    # A contributor demoted for abuse is not re-promoted automatically;
    # otherwise the next accepted review would undo the demotion.
    promotion_blocked: Mapped[bool] = mapped_column(Boolean, default=False,
                                                    nullable=False)

    reviews: Mapped[List["Review"]] = relationship(back_populates="user")
    face_samples: Mapped[List["BiometricSample"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("warnings >= 0", name="ck_users_warnings_non_negative"),
        CheckConstraint("role in ('VS','RC','VC','SU')", name="ck_users_role"),
    )


class RefreshToken(Base, TimestampMixin):
    """A refresh token, stored hashed so a database leak is not a key ring."""

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True,
                                            index=True, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True),
                                                    nullable=False)
    revoked_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str] = mapped_column(String(255), default="")

    user: Mapped["User"] = relationship()

    @property
    def active(self) -> bool:
        return (self.revoked_at is None
                and as_utc(self.expires_at) > utcnow())


class BiometricSample(Base, TimestampMixin):
    """One enrolled face or voice sample (face sign-in).

    The sample itself stays on disk; only its path and kind are relational.
    Biometric data in a row that a report might accidentally select is a
    liability, and the recogniser wants files anyway.
    """

    __tablename__ = "biometric_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)   # face|voice
    path: Mapped[str] = mapped_column(String(512), nullable=False)

    user: Mapped["User"] = relationship(back_populates="face_samples")

    __table_args__ = (
        CheckConstraint("kind in ('face','voice')", name="ck_biometric_kind"),
        Index("ix_biometric_user_kind", "user_id", "kind"),
    )


# --------------------------------------------------------------- catalogue


class Book(Base, TimestampMixin):
    __tablename__ = "books"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    author: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    category: Mapped[str] = mapped_column(String(64), index=True,
                                          nullable=False)
    year: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    description: Mapped[str] = mapped_column(Text, default="")

    cover_path: Mapped[str] = mapped_column(String(512), default="")
    text_path: Mapped[str] = mapped_column(String(512), default="")
    gutenberg_id: Mapped[Optional[int]] = mapped_column(Integer)

    browse_count: Mapped[int] = mapped_column(Integer, default=0,
                                              nullable=False)

    figures: Mapped[List["BookFigure"]] = relationship(
        back_populates="book", cascade="all, delete-orphan",
        order_by="BookFigure.position")
    reviews: Mapped[List["Review"]] = relationship(
        back_populates="book", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_books_category_author", "category", "author"),
    )


class BookFigure(Base):
    """An internal illustration, indexed by picture search."""

    __tablename__ = "book_figures"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    book: Mapped["Book"] = relationship(back_populates="figures")

    __table_args__ = (
        UniqueConstraint("book_id", "position", name="uq_figure_position"),
    )


# ---------------------------------------------------------------- reviews


class Review(Base, TimestampMixin):
    """A rating and comment.

    ``user_id`` is nullable on purpose: Reviews and moderation: says *any* user
    can comment, including one who is not signed in.
    """

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)

    role_at_time: Mapped[str] = mapped_column(String(2), default=VISITOR)
    stars: Mapped[int] = mapped_column(Integer, nullable=False)

    raw_comment: Mapped[str] = mapped_column(Text, default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    taboo_hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False,
                                          nullable=False)

    trusted_reviewer: Mapped[bool] = mapped_column(Boolean, default=False,
                                                   nullable=False)
    # A trusted contributor's rating counts double.
    weight: Mapped[Decimal] = mapped_column(Numeric(4, 2),
                                            default=Decimal("1.00"),
                                            nullable=False)

    book: Mapped["Book"] = relationship(back_populates="reviews")
    user: Mapped[Optional["User"]] = relationship(back_populates="reviews")

    __table_args__ = (
        CheckConstraint("stars between 1 and 5", name="ck_reviews_stars_range"),
        Index("ix_reviews_book_blocked", "book_id", "blocked"),
    )


class TabooWord(Base, TimestampMixin):
    __tablename__ = "taboo_words"

    word: Mapped[str] = mapped_column(String(64), primary_key=True)


# ------------------------------------------------------- ledger and events


class Event(Base, TimestampMixin):
    """An audit trail of anything a super-user might be asked to explain."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")


class BrowseHistory(Base, TimestampMixin):
    """What a customer looked at, which is what the recommender reads."""

    __tablename__ = "browse_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False)

    __table_args__ = (
        Index("ix_browse_user_created", "user_id", "created_at"),
    )


# ===========================================================================
# The resource catalogue
# ===========================================================================
# Everything below describes things that live somewhere else. The site is a
# signpost, not a host: a resource row holds enough to find, judge and open a
# free book, and a link to where it actually is. Imports run again and again,
# so a row's identity comes from upstream rather than from our own id.


class Source(Base, TimestampMixin):
    """A catalogue we import from.

    Attribution is a column, not a footnote. The licence a source's data is
    published under usually requires naming it, and keeping that text here
    means every page that shows a resource can show where it came from.
    """

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True,
                                      nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    homepage: Mapped[str] = mapped_column(String(512), default="")
    licence: Mapped[str] = mapped_column(String(64), default="")
    attribution: Mapped[str] = mapped_column(Text, default="")
    terms_url: Mapped[str] = mapped_column(String(512), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True,
                                          nullable=False)

    resources: Mapped[List["Resource"]] = relationship(
        back_populates="source", cascade="all, delete-orphan")


class Resource(Base, TimestampMixin):
    """One free book, course or paper, as a source describes it.

    ``external_id`` is the source's identity for the item, derived from its
    normalised URL, so the same entry maps to the same row on every import.
    ``work_key`` groups the same work listed more than once (a PDF and an
    HTML edition, say) without merging the rows. ``content_hash`` is how an
    import tells "unchanged" from "updated" without comparing every column.

    Link health (``last_checked_at``, ``last_status``, ``dead``) and
    ``click_count`` belong to us, not to the source, so an import never
    overwrites them.

    The UNIQUE constraint on ``(source_id, external_id)`` is the line that
    makes re-running an import safe: two imports racing on the same entry
    cannot both insert it.
    """

    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), index=True,
        nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(512), index=True,
                                       nullable=False)
    authors: Mapped[str] = mapped_column(String(512), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="book")
    media: Mapped[str] = mapped_column(String(64), default="")
    language: Mapped[str] = mapped_column(String(8), default="en", index=True)
    licence: Mapped[str] = mapped_column(String(64), default="")
    in_progress: Mapped[bool] = mapped_column(Boolean, default=False,
                                              nullable=False)
    work_key: Mapped[str] = mapped_column(String(64), index=True, default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)

    last_checked_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    last_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    dead: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False,
                                       index=True)
    click_count: Mapped[int] = mapped_column(Integer, default=0,
                                             nullable=False)

    source: Mapped["Source"] = relationship(back_populates="resources")
    topics: Mapped[List["ResourceTopic"]] = relationship(
        back_populates="resource", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("source_id", "external_id",
                         name="uq_resource_source_external"),
        CheckConstraint(
            "kind in ('book','course','paper','cheatsheet','video',"
            "'interactive')", name="ck_resource_kind"),
        Index("ix_resources_source_dead", "source_id", "dead"),
    )


# Full-text search, PostgreSQL only.
#
# A generated column holds each row's search document -- title weighted
# highest, then authors, then description -- and a GIN index makes matching
# against it an index lookup. Measured at 99,794 rows: ILIKE '%term%' read
# every row in 128-157 ms; this returned the same 1,558 rows in 1.7-2.6 ms
# (tools/bench_search.py reproduces it).
#
# It is not a mapped column because SQLite has no tsvector: declaring it on
# the model would break the SQLite schema. Instead the same DDL runs after
# the table is created on PostgreSQL (tests and create_all), and the
# migration adds it to existing databases. alembic/env.py is told to ignore
# it, so `alembic check` does not report it as drift.
SEARCH_VECTOR_COLUMN = (
    "ALTER TABLE resources ADD COLUMN search_vector tsvector "
    "GENERATED ALWAYS AS ("
    "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(authors, '')), 'B') || "
    "setweight(to_tsvector('english', coalesce(description, '')), 'C')"
    ") STORED")
SEARCH_VECTOR_INDEX = (
    "CREATE INDEX ix_resources_search ON resources USING gin (search_vector)")

for _statement in (SEARCH_VECTOR_COLUMN, SEARCH_VECTOR_INDEX):
    event.listen(Resource.__table__, "after_create",
                 DDL(_statement).execute_if(dialect="postgresql"))


class Topic(Base, TimestampMixin):
    """A subject (Machine Learning) or a programming language (Rust)."""

    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(96), unique=True, index=True,
                                      nullable=False)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="subject")

    __table_args__ = (
        CheckConstraint("kind in ('subject','language')",
                        name="ck_topic_kind"),
    )


class ResourceTopic(Base):
    __tablename__ = "resource_topics"

    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), primary_key=True)
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True)

    resource: Mapped["Resource"] = relationship(back_populates="topics")
    topic: Mapped["Topic"] = relationship()


class ImportRun(Base, TimestampMixin):
    """The audit trail: one row per import, successful or not.

    A failed import still leaves a row saying when it ran and why it failed,
    which is the first thing anyone asks when the catalogue looks stale.
    """

    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), index=True,
        nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running",
                                        nullable=False)
    inserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unchanged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="")

    source: Mapped["Source"] = relationship()

    __table_args__ = (
        CheckConstraint("status in ('running','ok','failed')",
                        name="ck_import_run_status"),
    )
