"""The relational schema.

Three decisions run through the whole file and are worth stating once.

**Money is ``Numeric``, never ``Float``.** A float cannot represent 0.10, so
a balance built from float arithmetic drifts: add 10 cents a hundred times
and you do not have ten dollars. Every amount here is ``Numeric(10, 2)`` and
every calculation goes through ``decimal.Decimal``. This is the single most
common defect in a hobby e-commerce schema.

**Prices are captured, not referenced.** An ``order_item`` stores the price
actually paid, not a foreign key to the book's current price. Otherwise
raising a price silently rewrites every past receipt, and the finance report
stops matching what customers were charged.

**Constraints are in the database, not only in Python.** Stars must be 1 to 5,
a cart cannot hold the same book twice, quantities are positive, stock cannot
go negative. Application-level validation is a nicer error message on top of
these, not the thing standing between the shop and a wrong number.

What a constraint cannot do is worth saying too, since it is easy to assume
otherwise: ``stock >= 0`` does **not** prevent concurrent overselling. In a
lost update every session reads the same stock and writes the same decrement,
so no row goes negative and the constraint stays silent while several
customers are charged for one copy. That is what the row lock in the checkout
path is for. See ``ADR-001`` for the measurements.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric,
    String, Text, UniqueConstraint, func, select,
)
from sqlalchemy.orm import (
    Mapped, column_property, mapped_column, relationship,
)

from .base import Base


# Roles. Kept as short strings matching the original specification's wording
# so that stored data, the API and the rules all speak the same language.
VISITOR = "VS"
REGISTERED = "RC"
VIP = "VC"
SUPER_USER = "SU"

MONEY = Numeric(10, 2)


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
    balance: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"),
                                             nullable=False)
    total_fines: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"),
                                                 nullable=False)

    warnings: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    suspended: Mapped[bool] = mapped_column(Boolean, default=False,
                                            nullable=False)
    suspend_reason: Mapped[str] = mapped_column(String(255), default="")

    # Spec items 7 and 8 contradict each other on whether a demoted VIP can be
    # promoted again. The resolution is recorded rather than implied.
    vip_ineligible: Mapped[bool] = mapped_column(Boolean, default=False,
                                                 nullable=False)

    orders: Mapped[List["Order"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")
    cart: Mapped[Optional["Cart"]] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan")
    reviews: Mapped[List["Review"]] = relationship(back_populates="user")
    face_samples: Mapped[List["BiometricSample"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("warnings >= 0", name="ck_users_warnings_non_negative"),
        CheckConstraint("role in ('VS','RC','VC','SU')", name="ck_users_role"),
    )

    # Derived figures are declared below the class, as column_property with
    # correlated subqueries. See the note after Book.


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
    """One enrolled face or voice sample (spec item 4).

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
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    stock: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
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
        # This catches a single transaction decrementing past zero -- taking 5
        # copies when 3 remain. It does NOT catch concurrent overselling, and
        # it is worth being precise about why, because it is tempting to
        # assume otherwise: in a lost update, every session reads stock = 1
        # and writes stock = 0. No row ever goes negative, so the constraint
        # is never violated and nine customers are charged for one copy.
        # Measured: 16 concurrent sessions, 9 succeeded, constraint silent.
        #
        # Only the row lock in the checkout path prevents that. The constraint
        # is a floor, not the fix.
        CheckConstraint("stock >= 0", name="ck_books_stock_non_negative"),
        CheckConstraint("price >= 0", name="ck_books_price_non_negative"),
        Index("ix_books_category_author", "category", "author"),
    )


class BookFigure(Base):
    """An internal illustration, indexed by picture search (spec item 5)."""

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


# ------------------------------------------------------------------- cart


class Cart(Base, TimestampMixin):
    """One open cart per customer.

    The unique constraint on ``user_id`` is what makes that true in the
    database rather than by convention -- two carts for one person is the
    sort of thing that only shows up when a customer complains.
    """

    __tablename__ = "carts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True,
        nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
        nullable=False)

    user: Mapped["User"] = relationship(back_populates="cart")
    items: Mapped[List["CartItem"]] = relationship(
        back_populates="cart", cascade="all, delete-orphan",
        order_by="CartItem.id")


class CartItem(Base, TimestampMixin):
    __tablename__ = "cart_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    cart_id: Mapped[int] = mapped_column(
        ForeignKey("carts.id", ondelete="CASCADE"), index=True, nullable=False)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    cart: Mapped["Cart"] = relationship(back_populates="items")
    book: Mapped["Book"] = relationship()

    __table_args__ = (
        # Adding the same book twice raises the quantity; it does not create
        # a second line. The database is what guarantees that.
        UniqueConstraint("cart_id", "book_id", name="uq_cart_item_book"),
        CheckConstraint("quantity > 0", name="ck_cart_item_quantity_positive"),
    )


# ----------------------------------------------------------------- orders


ORDER_STATUSES = ("pending", "paid", "cancelled", "refunded")


class Order(Base, TimestampMixin):
    """A placed order. Immutable once paid, apart from its status."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending",
                                        index=True, nullable=False)

    subtotal: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"),
                                              nullable=False)
    discount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"),
                                              nullable=False)
    total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"),
                                           nullable=False)

    # The discount actually applied, recorded as a rate so a receipt can be
    # explained years later even if the VIP rate changes.
    discount_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), default=Decimal("0.0000"), nullable=False)

    placed_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="orders")
    items: Mapped[List["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan")
    payments: Mapped[List["Payment"]] = relationship(
        back_populates="order", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "status in ('pending','paid','cancelled','refunded')",
            name="ck_orders_status"),
        CheckConstraint("subtotal >= 0 and total >= 0 and discount >= 0",
                        name="ck_orders_amounts_non_negative"),
        Index("ix_orders_user_status", "user_id", "status"),
    )


class OrderItem(Base):
    """A line on an order, with the price that was actually charged."""

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True,
        nullable=False)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="RESTRICT"), index=True,
        nullable=False)

    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    line_total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    # Title and author as they were, so a receipt still reads correctly after
    # an admin corrects a typo in the catalogue.
    title_at_purchase: Mapped[str] = mapped_column(String(255), default="")

    order: Mapped["Order"] = relationship(back_populates="items")
    book: Mapped["Book"] = relationship(backref="order_items")

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_item_quantity_positive"),
        CheckConstraint("unit_price >= 0 and line_total >= 0",
                        name="ck_order_item_amounts_non_negative"),
    )


class Payment(Base, TimestampMixin):
    """What settled an order.

    This project takes payment from the customer's store balance, which the
    specification describes. The table is shaped so a card processor could be
    added without a migration of the order model: ``method`` and ``reference``
    are already where a gateway's identifier would go.
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True,
        nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)

    method: Mapped[str] = mapped_column(String(32), default="balance",
                                        nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="captured",
                                        nullable=False)
    reference: Mapped[str] = mapped_column(String(64), default="")

    order: Mapped["Order"] = relationship(back_populates="payments")

    __table_args__ = (
        CheckConstraint("status in ('captured','failed','refunded')",
                        name="ck_payments_status"),
    )


# ---------------------------------------------------------------- reviews


class Review(Base, TimestampMixin):
    """A rating and comment.

    ``user_id`` is nullable on purpose: specification item 7 says *any* user
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

    verified_purchase: Mapped[bool] = mapped_column(Boolean, default=False,
                                                    nullable=False)
    # A purchaser's rating counts double (spec item 7).
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


class Transaction(Base, TimestampMixin):
    """The money ledger. Append-only: nothing updates or deletes a row here.

    Every figure in the finance report is a sum over this table, which is why
    the report can never disagree with the accounts.
    """

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True)
    book_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("books.id", ondelete="SET NULL"), index=True)
    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), index=True)

    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    note: Mapped[str] = mapped_column(String(255), default="")

    __table_args__ = (
        CheckConstraint(
            "kind in ('purchase','fine','deposit','refund')",
            name="ck_transactions_kind"),
        Index("ix_transactions_kind_created", "kind", "created_at"),
    )


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


# ------------------------------------------------- derived figures (no N+1)
#
# `purchase_count` and `total_spent` are computed from the orders table, not
# stored on the account -- a counter that can disagree with the rows it counts
# is a bug waiting for a demo.
#
# The obvious way to write them is a Python property that walks
# `self.orders`. That was the first implementation and it was wrong: each
# access lazy-loads, so listing ten accounts cost **41 queries** and listing
# ten books cost **41** -- on the catalogue, the most requested page in the
# shop. At a thousand books that is four thousand round trips per page view.
#
# Declaring them as `column_property` with a correlated scalar subquery moves
# the work into the same SELECT that loads the row. Ten accounts and ten books
# each cost one query. The subqueries are covered by the indexes on
# `order_items.order_id`, `order_items.book_id` and `orders.user_id`.
#
# They are defined here, after every table exists, because each one references
# tables declared further down the file.

User.purchase_count = column_property(
    select(func.coalesce(func.sum(OrderItem.quantity), 0))
    .select_from(OrderItem)
    .join(Order, Order.id == OrderItem.order_id)
    .where(Order.user_id == User.id, Order.status == "paid")
    .correlate_except(OrderItem, Order)
    .scalar_subquery(),
    deferred=False,
)

User.total_spent = column_property(
    select(func.coalesce(func.sum(Order.total), 0))
    .where(Order.user_id == User.id, Order.status == "paid")
    .correlate_except(Order)
    .scalar_subquery(),
    deferred=False,
)

Book.purchase_count = column_property(
    select(func.coalesce(func.sum(OrderItem.quantity), 0))
    .select_from(OrderItem)
    .join(Order, Order.id == OrderItem.order_id)
    .where(OrderItem.book_id == Book.id, Order.status == "paid")
    .correlate_except(OrderItem, Order)
    .scalar_subquery(),
    deferred=False,
)
