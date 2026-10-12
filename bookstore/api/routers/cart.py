"""The cart, checkout and order history.

CRUD on cart lines: POST adds, PATCH sets an exact quantity, DELETE removes
one line or empties the basket. Status codes carry meaning -- 404 for a book
that is not in the cart, 409 for a conflict with stock, 204 for a deletion
that returns nothing.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ...db import models as m
from ...services import cart as cart_service, checkout as checkout_service
from ...services import pricing
from .. import present, runtime
from .. import schemas as S
from ..deps import db_session, require_user


router = APIRouter(tags=["cart"])


def _cart_out(session: Session, user: m.User) -> S.CartOut:
    data = cart_service.summary(session, user, runtime.config().vip_discount)
    return S.CartOut(
        lines=[S.CartLineOut(cover_url="/api/books/%d/cover" % line["book_id"],
                             **line) for line in data["lines"]],
        item_count=data["item_count"],
        subtotal=data["subtotal"],
        discount=data["discount"],
        total=data["total"],
        affordable=data["affordable"],
        balance=data["balance"],
    )


@router.get("/api/cart", response_model=S.CartOut)
def view_cart(user: m.User = Depends(require_user),
              session: Session = Depends(db_session)):
    return _cart_out(session, user)


@router.post("/api/cart/items", response_model=S.CartOut,
             status_code=status.HTTP_201_CREATED)
def add_to_cart(body: S.CartItemIn, user: m.User = Depends(require_user),
                session: Session = Depends(db_session)):
    try:
        cart_service.add(session, user, body.book_id, body.quantity)
    except cart_service.CartError as exc:
        message = str(exc)
        if "No such book" in message:
            raise HTTPException(status.HTTP_404_NOT_FOUND, message)
        # Not enough stock is a conflict with the current state of the
        # resource, not a malformed request.
        raise HTTPException(status.HTTP_409_CONFLICT, message)
    return _cart_out(session, user)


@router.patch("/api/cart/items/{book_id}", response_model=S.CartOut)
def set_cart_quantity(book_id: int, body: S.QuantityIn,
                      user: m.User = Depends(require_user),
                      session: Session = Depends(db_session)):
    try:
        cart_service.set_quantity(session, user, book_id, body.quantity)
    except cart_service.CartError as exc:
        message = str(exc)
        if "not in your cart" in message:
            raise HTTPException(status.HTTP_404_NOT_FOUND, message)
        raise HTTPException(status.HTTP_409_CONFLICT, message)
    return _cart_out(session, user)


@router.delete("/api/cart/items/{book_id}", response_model=S.CartOut)
def remove_from_cart(book_id: int, user: m.User = Depends(require_user),
                     session: Session = Depends(db_session)):
    if not cart_service.remove(session, user, book_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "That book is not in your cart.")
    return _cart_out(session, user)


@router.delete("/api/cart", status_code=status.HTTP_204_NO_CONTENT)
def empty_cart(user: m.User = Depends(require_user),
               session: Session = Depends(db_session)):
    cart_service.clear(session, user)


# ------------------------------------------------------------- checkout


@router.post("/api/checkout", response_model=S.CheckoutOut)
def checkout(user: m.User = Depends(require_user),
             session: Session = Depends(db_session)):
    """Turn the cart into a paid order, in one transaction.

    A refusal is a 200 with ``ok: false`` rather than an error status,
    because a refusal is a normal outcome with a result the customer needs:
    insufficient funds still applies a fine and may suspend the account, and
    the client has to show the new balance.
    """
    try:
        result = checkout_service.place_order(session, user, runtime.config())
    except checkout_service.CheckoutError as exc:
        return S.CheckoutOut(
            ok=False, message=exc.message,
            new_balance=exc.balance if exc.balance is not None
            else pricing.money(user.balance),
            fine_applied=exc.fine_applied,
            suspended=exc.suspended,
        )

    message = "Order placed. Thank you."
    if result.promoted:
        message += " You are now a VIP customer -- 10% off from now on."

    return S.CheckoutOut(
        ok=True, message=message,
        order=present.order_out(result.order),
        promoted=result.promoted,
        new_balance=pricing.money(user.balance),
    )


# --------------------------------------------------------------- orders


@router.get("/api/orders", response_model=List[S.OrderOut])
def my_orders(user: m.User = Depends(require_user),
              session: Session = Depends(db_session),
              limit: int = 50, offset: int = 0):
    """This caller's orders, newest first.

    Scoped by the dependency, never by a query parameter: there is no way to
    ask this route for somebody else's history.
    """
    orders = session.execute(
        select(m.Order)
        .where(m.Order.user_id == user.id)
        .options(selectinload(m.Order.items).selectinload(m.OrderItem.book),
                 selectinload(m.Order.payments))
        .order_by(m.Order.id.desc())
        .limit(min(limit, 200)).offset(max(offset, 0))
    ).scalars().all()
    return [present.order_out(order) for order in orders]


@router.get("/api/orders/{order_id}", response_model=S.OrderOut)
def one_order(order_id: int, user: m.User = Depends(require_user),
              session: Session = Depends(db_session)):
    order = session.execute(
        select(m.Order)
        .where(m.Order.id == order_id)
        .options(selectinload(m.Order.items).selectinload(m.OrderItem.book),
                 selectinload(m.Order.payments))
    ).scalar_one_or_none()

    # 404 rather than 403 for somebody else's order: a 403 would confirm that
    # the order exists, which is information the caller has no right to.
    if order is None or (order.user_id != user.id
                         and user.role != m.SUPER_USER):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such order.")
    return present.order_out(order)
