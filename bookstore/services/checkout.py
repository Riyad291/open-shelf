"""Checkout: the one place in this project where correctness is hard.

Everything else can be retried. A checkout cannot: it moves money, decrements
stock, writes a ledger line and may promote the customer to VIP, and either
all of that happens or none of it does.

Three things make it correct, and they are worth reading in order.

**One transaction.** The caller owns it (``db.get_session`` commits once per
request). Nothing here commits, so a failure anywhere unwinds the whole
checkout rather than leaving an order with no payment, or stock removed for a
sale that never completed.

**Rows are locked, in a fixed order.** Each book is re-read with
``SELECT ... FOR UPDATE`` *inside* the transaction, so between the stock check
and the decrement no other session can touch that row. This is the fix for the
defect recorded in ADR-001: without it, sixteen concurrent sessions buying the
last copy produced nine successful purchases. Locks are taken in ascending
book id, because two customers buying the same two books in opposite orders
is a deadlock if they are not.

**The price is re-derived under the lock and captured on the line.** The cart
shows a price, but the shop's price may have changed since. The customer is
charged what the catalogue says at the moment of sale, and the order line
stores that figure so the receipt stays true forever.

The fine rule from specification item 6 is preserved exactly: attempting a
purchase you cannot afford is refused *and* costs a $1 fine, which can push
the balance negative and suspend the account.
"""

from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import models as m
from ..db.base import supports_row_locks
from . import pricing


class CheckoutError(Exception):
    """Checkout refused, with a reason fit to show a customer."""

    def __init__(self, message: str, *, fine_applied: Decimal = Decimal("0.00"),
                 suspended: bool = False, balance: Optional[Decimal] = None):
        super().__init__(message)
        self.message = message
        self.fine_applied = fine_applied
        self.suspended = suspended
        self.balance = balance


class CheckoutResult:
    def __init__(self, order: m.Order, payment: m.Payment, *,
                 promoted: bool = False):
        self.order = order
        self.payment = payment
        self.promoted = promoted


def _lock_books(session: Session, book_ids: List[int]) -> dict:
    """Re-read every book in the cart, locked, in a deadlock-free order."""
    statement = (
        select(m.Book)
        .where(m.Book.id.in_(book_ids))
        .order_by(m.Book.id)            # fixed order: see the module docstring
    )
    if supports_row_locks(session):
        statement = statement.with_for_update()

    return {book.id: book for book in session.execute(statement).scalars()}


def place_order(session: Session, user: m.User, config) -> CheckoutResult:
    """Turn the customer's cart into a paid order.

    Raises ``CheckoutError`` if it cannot, having applied the fine the
    specification requires when the reason is insufficient funds.
    """
    if user.suspended:
        raise CheckoutError(
            "This account is suspended: %s" % (user.suspend_reason or
                                               "contact the shop."))

    cart = session.execute(
        select(m.Cart).where(m.Cart.user_id == user.id)
    ).scalar_one_or_none()

    items = list(cart.items) if cart else []
    if not items:
        raise CheckoutError("Your cart is empty.")

    wanted = {item.book_id: item.quantity for item in items}
    books = _lock_books(session, sorted(wanted))

    # -- availability, now that the rows are ours -------------------------
    missing = [book_id for book_id in wanted if book_id not in books]
    if missing:
        raise CheckoutError(
            "A book in your cart is no longer sold here. Remove it and try "
            "again.")

    short = [
        "'%s' (%d left, %d wanted)" % (books[bid].title, books[bid].stock, qty)
        for bid, qty in wanted.items() if books[bid].stock < qty
    ]
    if short:
        raise CheckoutError(
            "Not enough stock for %s. Somebody bought it while you were "
            "deciding." % ", ".join(short))

    # -- price it, under the same lock ------------------------------------
    rate = pricing.discount_rate(user, config.vip_discount)
    subtotal = Decimal("0.00")
    full_price = Decimal("0.00")
    lines = []

    for book_id in sorted(wanted):
        book = books[book_id]
        quantity = wanted[book_id]
        unit = pricing.price_for(user, book, config.vip_discount)
        total = pricing.line_total(unit, quantity)
        subtotal += total
        full_price += pricing.line_total(book.price, quantity)
        lines.append((book, quantity, unit, total))

    subtotal = pricing.money(subtotal)
    discount = pricing.money(full_price - subtotal)

    # -- can they pay? ----------------------------------------------------
    if subtotal > user.balance:
        balance_before = pricing.money(user.balance)
        fine = _apply_fine(session, user, config, subtotal)
        # Formatted from Decimal with str(), not %.2f: the latter converts to
        # float first, which is the one thing this schema went to Numeric to
        # avoid. It would be invisible here and wrong somewhere else later.
        raise CheckoutError(
            "Purchase refused: the cart costs $" + str(subtotal)
            + " but your balance is $" + str(balance_before)
            + ". A $" + str(fine) + " fine was applied; balance is now $"
            + str(pricing.money(user.balance)) + ".",
            fine_applied=fine,
            suspended=user.suspended,
            balance=pricing.money(user.balance),
        )

    # -- commit the sale ---------------------------------------------------
    order = m.Order(
        user_id=user.id, status="paid",
        subtotal=subtotal, discount=discount, total=subtotal,
        discount_rate=rate, placed_at=m.utcnow(),
    )
    session.add(order)
    session.flush()

    for book, quantity, unit, total in lines:
        session.add(m.OrderItem(
            order_id=order.id, book_id=book.id, quantity=quantity,
            unit_price=unit, line_total=total,
            title_at_purchase=book.title,
        ))
        book.stock -= quantity

    payment = m.Payment(
        order_id=order.id, user_id=user.id, method="balance",
        amount=subtotal, status="captured",
        reference="order-%d" % order.id,
    )
    session.add(payment)

    user.balance = pricing.money(user.balance - subtotal)

    session.add(m.Transaction(
        kind="purchase", user_id=user.id, order_id=order.id,
        amount=subtotal,
        note="order %d (%d item%s)" % (order.id, len(lines),
                                       "" if len(lines) == 1 else "s"),
    ))
    session.add(m.Event(
        kind="order_placed", user_id=user.id,
        detail="order %d, $%.2f, %d line(s)" % (order.id, subtotal, len(lines)),
    ))

    for item in items:
        session.delete(item)

    session.flush()
    promoted = _maybe_promote(session, user, config)

    return CheckoutResult(order, payment, promoted=promoted)


def _apply_fine(session: Session, user: m.User, config,
                attempted: Decimal) -> Decimal:
    """Specification item 6: a purchase you cannot afford still costs $1."""
    fine = pricing.money(Decimal(str(config.fine_amount)))
    user.balance = pricing.money(user.balance - fine)
    user.total_fines = pricing.money(user.total_fines + fine)

    session.add(m.Transaction(
        kind="fine", user_id=user.id, amount=fine,
        note="insufficient funds for a $%.2f cart" % attempted,
    ))
    session.add(m.Event(
        kind="fine", user_id=user.id,
        detail="$%.2f fine: cart of $%.2f exceeded the balance"
               % (fine, attempted),
    ))

    if user.balance < 0 and not user.suspended:
        user.suspended = True
        user.suspend_reason = "negative balance"
        session.add(m.Event(
            kind="account_suspended", user_id=user.id,
            detail="balance $%.2f" % user.balance,
        ))

    session.flush()
    return fine


def _maybe_promote(session: Session, user: m.User, config) -> bool:
    """Specification item 8: more than 3 purchases or more than $200 spent.

    Both figures are read from the orders table rather than from counters on
    the account, so they cannot drift out of step with the orders themselves.
    """
    if user.role != m.REGISTERED or user.vip_ineligible:
        return False

    # Two aggregates, not a walk over every order and every line. The earlier
    # version loaded the whole purchase history on each checkout, which gets
    # slower for exactly the customers who buy the most.
    purchases = session.execute(
        select(func.coalesce(func.sum(m.OrderItem.quantity), 0))
        .select_from(m.OrderItem)
        .join(m.Order, m.Order.id == m.OrderItem.order_id)
        .where(m.Order.user_id == user.id, m.Order.status == "paid")
    ).scalar_one()

    spent = session.execute(
        select(func.coalesce(func.sum(m.Order.total), 0))
        .where(m.Order.user_id == user.id, m.Order.status == "paid")
    ).scalar_one()

    if (purchases > config.vip_purchase_threshold
            or spent > Decimal(str(config.vip_spend_threshold))):
        user.role = m.VIP
        session.add(m.Event(
            kind="promoted", user_id=user.id,
            detail="RC -> VC (%s purchases, $%s spent)" % (purchases, spent),
        ))
        session.flush()
        return True
    return False
