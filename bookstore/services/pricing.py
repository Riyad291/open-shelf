"""What a given customer pays for a given book.

Separated from checkout because three places need the answer and they must
not disagree: the catalogue (showing a price), the cart (showing a subtotal)
and checkout (charging it). A price shown and a price charged that differ by
a cent is the kind of defect customers notice and nobody tests for.

All arithmetic is ``Decimal`` with explicit rounding. ``ROUND_HALF_UP`` is
chosen over Python's banker's rounding because it is what a shopper expects
and what a till does: $0.125 becomes $0.13, not $0.12.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from ..db import models as m


CENT = Decimal("0.01")


def money(value) -> Decimal:
    """Round to cents the way a till does."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def discount_rate(user: m.User | None, vip_discount: float) -> Decimal:
    """Spec item 8: a VIP gets a percentage off every purchase.

    A visitor, a registered customer and the super-user all pay list price.
    """
    if user is not None and user.role == m.VIP:
        return Decimal(str(vip_discount))
    return Decimal("0")


def price_for(user: m.User | None, book: m.Book, vip_discount: float) -> Decimal:
    rate = discount_rate(user, vip_discount)
    if not rate:
        return money(book.price)
    return money(book.price * (Decimal("1") - rate))


def line_total(unit_price: Decimal, quantity: int) -> Decimal:
    """Quantity times unit price.

    Rounding happens once, on the unit price, then multiplies. Rounding the
    product instead lets a discount drift by a cent per line, and the order
    total stops matching the sum of its own lines.
    """
    return money(money(unit_price) * quantity)
