"""One long realistic session, then audited the way you audit a shop's books.

The per-rule tests check each rule in isolation. This checks that the rules
still agree with each other after a sequence of real actions: that the finance
report reconciles with the ledger, that every paid order has a payment, that
stock is conserved, and that no account is left in a state the rules say
cannot exist.

Per-rule tests cannot catch two individually correct rules that disagree about
the same number. These can.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from bookstore.db import models as m
from bookstore.services import accounts, cart, checkout, finance, reviews


@pytest.fixture()
def busy_shop(session, config, make_book, make_user):
    """A shop that has been used: purchases, refusals, ratings, moderation."""
    books = [
        make_book(title="Book %d" % i, author="Author %d" % (i % 3),
                  category=["literature", "science", "history"][i % 3],
                  year=1900 + i * 7, price=str(5 + i * 3), stock=6)
        for i in range(6)
    ]
    customers = [make_user(balance="120.00") for _ in range(5)]
    vip = make_user(role=m.VIP, balance="300.00")
    pauper = make_user(balance="2.00")
    session.commit()

    # Ordinary trade.
    for index, person in enumerate(customers):
        cart.add(session, person, books[index % len(books)].id, 1)
        cart.add(session, person, books[(index + 1) % len(books)].id, 2)
        checkout.place_order(session, person, config)

    # A VIP, who pays less for the same goods.
    cart.add(session, vip, books[0].id, 1)
    checkout.place_order(session, vip, config)

    # Somebody who cannot pay: refused, fined, and pushed into suspension.
    cart.add(session, pauper, books[5].id, 1)
    with pytest.raises(checkout.CheckoutError):
        checkout.place_order(session, pauper, config)

    # Opinions, including one that earns a warning.
    for index, person in enumerate(customers):
        reviews.submit(session, person, books[0], 5 - (index % 2),
                       "Worth reading.", config)
    reviews.submit(session, customers[0], books[1], 1,
                   "stupid garbage trash idiot", config)

    # A deposit and a reopening.
    accounts.deposit(session, pauper, Decimal("20.00"), "top-up")
    accounts.reopen(session, pauper)
    session.commit()

    return {"books": books, "customers": customers, "vip": vip,
            "pauper": pauper}


def test_the_finance_report_reconciles_with_the_ledger(session, busy_shop):
    report = finance.report(session)

    sales = session.execute(
        select(func.coalesce(func.sum(m.Transaction.amount), 0))
        .where(m.Transaction.kind == "purchase")).scalar_one()
    fines = session.execute(
        select(func.coalesce(func.sum(m.Transaction.amount), 0))
        .where(m.Transaction.kind == "fine")).scalar_one()

    assert report["revenue_sales"] == Decimal(sales).quantize(Decimal("0.01"))
    assert report["revenue_fines"] == Decimal(fines).quantize(Decimal("0.01"))
    assert report["total_revenue"] == (report["revenue_sales"]
                                       + report["revenue_fines"]
                                       - report["refunds"])


def test_every_paid_order_has_exactly_one_payment_for_its_total(
        session, busy_shop):
    orders = session.execute(
        select(m.Order).where(m.Order.status == "paid")).scalars().all()
    assert orders

    for order in orders:
        assert len(order.payments) == 1, "order %d" % order.id
        assert order.payments[0].amount == order.total


def test_every_order_total_equals_the_sum_of_its_lines(session, busy_shop):
    for order in session.execute(select(m.Order)).scalars():
        lines = sum((item.line_total for item in order.items), Decimal("0.00"))
        assert lines == order.subtotal, "order %d" % order.id
        assert order.total == order.subtotal


def test_every_purchase_line_has_a_ledger_entry(session, busy_shop):
    paid = session.execute(
        select(func.count(m.Order.id)).where(m.Order.status == "paid")
    ).scalar_one()
    ledger = session.execute(
        select(func.count(m.Transaction.id))
        .where(m.Transaction.kind == "purchase")).scalar_one()
    assert paid == ledger


def test_stock_is_conserved(session, busy_shop):
    """Opening stock minus everything sold must equal what is on the shelf."""
    for book in busy_shop["books"]:
        sold = session.execute(
            select(func.coalesce(func.sum(m.OrderItem.quantity), 0))
            .select_from(m.OrderItem)
            .join(m.Order, (m.Order.id == m.OrderItem.order_id)
                  & (m.Order.status == "paid"))
            .where(m.OrderItem.book_id == book.id)).scalar_one()
        assert book.stock == 6 - int(sold), book.title


def test_no_account_holds_an_impossible_state(session, busy_shop):
    for person in session.execute(select(m.User)).scalars():
        if person.balance < 0:
            assert person.suspended, (
                "%s is in debt but not suspended" % person.username)
        assert person.warnings >= 0
        if person.role == m.VIP:
            assert not person.vip_ineligible


def test_derived_figures_match_the_orders_they_are_derived_from(
        session, busy_shop):
    for person in session.execute(select(m.User)).scalars():
        paid = [o for o in person.orders if o.status == "paid"]
        expected_count = sum(item.quantity for o in paid for item in o.items)
        expected_spend = sum((o.total for o in paid), Decimal("0.00"))

        assert int(person.purchase_count or 0) == expected_count
        assert Decimal(person.total_spent or 0) == expected_spend


def test_a_vip_paid_less_than_list_for_the_same_book(session, busy_shop):
    vip_orders = [o for o in busy_shop["vip"].orders if o.status == "paid"]
    assert vip_orders
    for order in vip_orders:
        assert order.discount > 0
        for line in order.items:
            book = session.get(m.Book, line.book_id)
            assert line.unit_price < book.price


def test_the_customer_liability_equals_what_customers_hold(session, busy_shop):
    report = finance.report(session)
    held = session.execute(
        select(func.coalesce(func.sum(m.User.balance), 0))
        .where(m.User.role != m.SUPER_USER)).scalar_one()
    assert report["customer_liability"] == Decimal(held).quantize(Decimal("0.01"))


def test_a_blocked_comment_is_hidden_but_kept(session, busy_shop):
    blocked = session.execute(
        select(m.Review).where(m.Review.blocked.is_(True))).scalars().all()
    assert blocked, "the fixture should have produced one"

    for review in blocked:
        public = reviews.for_book(session, review.book_id)
        assert review.id not in [r.id for r in public]
        assert review.raw_comment, "the original is kept for the super-user"


def test_the_order_lists_agree_with_the_ratings_behind_them(
        session, config, busy_shop):
    lists = reviews.order_lists(session, config)
    for entry in lists["to_order"] + lists["no_order"]:
        summary = reviews.rating_summary(session, entry["book_id"])
        assert summary["count"] == entry["count"]
        assert summary["weighted_average"] == pytest.approx(
            entry["weighted_average"], abs=0.001)
        assert summary["count"] > config.min_ratings_for_list


def test_the_whole_shop_survives_being_read_back(session, busy_shop):
    """Everything above ran in one session. Read it fresh and it must agree."""
    from bookstore.db.base import session_factory

    before = finance.report(session)
    session.commit()

    with session_factory()() as fresh:
        after = finance.report(fresh)

    for key in ("revenue_sales", "revenue_fines", "total_revenue",
                "customer_liability", "inventory_value", "order_count"):
        assert before[key] == after[key], key
