"""The shopping cart.

A cart is not an order. Nothing here touches stock, money or the ledger --
putting a book in a basket reserves nothing, exactly as in a shop. All of
that happens at checkout, in one transaction.

Quantities are held in the database rather than recomputed, and the
``UNIQUE (cart_id, book_id)`` constraint means adding the same book twice
raises a quantity instead of creating a second line. The service relies on
that constraint rather than checking first, because checking first is the
same check-then-act race this project moved to a database to escape.
"""

from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import models as m
from . import pricing


class CartError(Exception):
    """Something the customer did that cannot be done."""


def get_or_create(session: Session, user: m.User) -> m.Cart:
    cart = session.execute(
        select(m.Cart)
        .where(m.Cart.user_id == user.id)
        .options(selectinload(m.Cart.items).selectinload(m.CartItem.book))
    ).scalar_one_or_none()

    if cart is None:
        cart = m.Cart(user_id=user.id)
        session.add(cart)
        session.flush()
    return cart


def add(session: Session, user: m.User, book_id: int,
        quantity: int = 1) -> m.CartItem:
    if quantity < 1:
        raise CartError("Quantity must be at least 1.")

    book = session.get(m.Book, book_id)
    if book is None:
        raise CartError("No such book.")

    cart = get_or_create(session, user)

    item = session.execute(
        select(m.CartItem).where(
            m.CartItem.cart_id == cart.id, m.CartItem.book_id == book_id)
    ).scalar_one_or_none()

    # Validate BEFORE mutating. The previous version raised after adding the
    # quantity, which is only safe while every caller rolls the transaction
    # back. A caller that catches CartError and commits anyway -- a perfectly
    # reasonable thing for a handler to do -- would have persisted a quantity
    # the shop cannot fill.
    #
    # Stock is not reserved by adding to a cart, exactly as in a shop.
    # Checkout re-checks under a row lock; this is courtesy, not the guard.
    wanted = (item.quantity if item else 0) + quantity
    if wanted > book.stock:
        raise CartError(
            "Only %d cop%s of '%s' %s in stock."
            % (book.stock, "y" if book.stock == 1 else "ies",
               book.title, "is" if book.stock == 1 else "are"))

    if item is None:
        # Appended to the relationship, not just session.add()ed, so that
        # cart.items reflects the new line within this same session.
        item = m.CartItem(book_id=book_id, quantity=quantity)
        cart.items.append(item)
    else:
        item.quantity = wanted

    session.flush()
    return item


def set_quantity(session: Session, user: m.User, book_id: int,
                 quantity: int) -> Optional[m.CartItem]:
    """Set an exact quantity. Zero removes the line, which is what a
    quantity selector dropping to 0 should mean."""
    cart = get_or_create(session, user)
    item = session.execute(
        select(m.CartItem).where(
            m.CartItem.cart_id == cart.id, m.CartItem.book_id == book_id)
    ).scalar_one_or_none()

    if item is None:
        raise CartError("That book is not in your cart.")

    if quantity <= 0:
        session.delete(item)
        session.flush()
        return None

    book = session.get(m.Book, book_id)
    if book is not None and quantity > book.stock:
        raise CartError(
            "Only %d in stock for '%s'." % (book.stock, book.title))

    item.quantity = quantity
    session.flush()
    return item


def remove(session: Session, user: m.User, book_id: int) -> bool:
    cart = get_or_create(session, user)
    item = session.execute(
        select(m.CartItem).where(
            m.CartItem.cart_id == cart.id, m.CartItem.book_id == book_id)
    ).scalar_one_or_none()
    if item is None:
        return False
    session.delete(item)
    session.flush()
    return True


def clear(session: Session, user: m.User) -> int:
    cart = get_or_create(session, user)
    count = len(cart.items)
    for item in list(cart.items):
        session.delete(item)
    session.flush()
    return count


def summary(session: Session, user: m.User, vip_discount: float) -> dict:
    """What the cart costs this customer, priced at their tier.

    The same function the cart screen and checkout both use, so the number
    the customer is shown is the number they are charged.
    """
    cart = get_or_create(session, user)

    lines: List[dict] = []
    subtotal = Decimal("0.00")
    full_price_total = Decimal("0.00")

    for item in cart.items:
        book = item.book
        unit = pricing.price_for(user, book, vip_discount)
        total = pricing.line_total(unit, item.quantity)
        subtotal += total
        full_price_total += pricing.line_total(book.price, item.quantity)
        lines.append({
            "book_id": book.id,
            "title": book.title,
            "author": book.author,
            "unit_price": unit,
            "list_price": pricing.money(book.price),
            "quantity": item.quantity,
            "line_total": total,
            "in_stock": book.stock,
            "enough_stock": item.quantity <= book.stock,
        })

    return {
        "cart_id": cart.id,
        "lines": lines,
        "item_count": sum(line["quantity"] for line in lines),
        "subtotal": pricing.money(subtotal),
        "discount": pricing.money(full_price_total - subtotal),
        "total": pricing.money(subtotal),
        "affordable": pricing.money(subtotal) <= user.balance,
        "balance": pricing.money(user.balance),
    }
