"""The last copy, bought by several people at once.

This is the defect the move to a database was for, so it gets a test that
fails without the fix rather than one that merely passes with it.

``test_without_the_lock_the_shop_oversells`` deliberately reimplements
checkout's stock handling *without* ``SELECT ... FOR UPDATE`` and asserts that
it breaks. If that test ever stops failing to oversell, the real test below it
has stopped proving anything -- the race would have become untriggerable and
the passing result would be luck rather than correctness.

These need real row locking, so they skip on SQLite, which serialises writers
with a database-wide lock instead. A skipped test is not a passing one, and CI
runs the suite against PostgreSQL for exactly this reason.
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from sqlalchemy import select

from bookstore.db import models as m
from bookstore.db.base import session_factory
from bookstore.services import cart, checkout


pytestmark = pytest.mark.usefixtures("supports_row_locks")


@pytest.fixture()
def contested(session, make_book, make_user):
    """One copy, sixteen customers who can all afford it."""
    book = make_book(title="The Last Copy", price="10.00", stock=1)
    buyers = [make_user(balance="500.00") for _ in range(16)]
    for buyer in buyers:
        cart.add(session, buyer, book.id, 1)
    session.commit()
    return book.id, [buyer.id for buyer in buyers]


def _run_together(work, count):
    """Start `count` threads at the same instant and wait for them all."""
    barrier = threading.Barrier(count)
    results, errors = [], []
    lock = threading.Lock()

    def runner(index):
        barrier.wait()
        try:
            outcome = work(index)
            with lock:
                results.append(outcome)
        except Exception as exc:                      # noqa: BLE001
            with lock:
                errors.append("%s: %s" % (type(exc).__name__, exc))

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return results, errors


def test_without_the_lock_the_shop_oversells(supports_row_locks, contested,
                                             config):
    """The bug, reproduced. If this stops reproducing, the test below is
    no longer evidence of anything."""
    if not supports_row_locks:
        pytest.skip("needs a database with row-level locking")

    book_id, buyer_ids = contested
    Session = session_factory()

    def buy(index):
        with Session() as session:
            book = session.execute(
                select(m.Book).where(m.Book.id == book_id)   # no FOR UPDATE
            ).scalar_one()
            if book.stock <= 0:
                return False
            book.stock = book.stock - 1
            session.commit()
            return True

    results, errors = _run_together(buy, len(buyer_ids))
    assert not errors, errors
    assert sum(1 for r in results if r) > 1, (
        "the unlocked race did not reproduce, so the locked test below "
        "proves nothing on this machine")


def test_only_one_customer_gets_the_last_copy(supports_row_locks, contested,
                                              config):
    if not supports_row_locks:
        pytest.skip("needs a database with row-level locking")

    book_id, buyer_ids = contested
    Session = session_factory()

    def buy(index):
        with Session() as session:
            person = session.get(m.User, buyer_ids[index])
            try:
                checkout.place_order(session, person, config)
                session.commit()
                return True
            except checkout.CheckoutError:
                session.rollback()
                return False

    results, errors = _run_together(buy, len(buyer_ids))

    assert not errors, "a refusal must be a clean message, not an exception"
    assert sum(1 for r in results if r) == 1

    with session_factory()() as session:
        assert session.get(m.Book, book_id).stock == 0
        assert session.execute(
            select(m.Order).where(m.Order.status == "paid")).scalars().all().__len__() == 1
        assert session.execute(
            select(m.OrderItem)).scalars().all().__len__() == 1
        assert session.execute(
            select(m.Payment)).scalars().all().__len__() == 1


def test_nobody_is_fined_for_losing_the_race(supports_row_locks, contested,
                                             config):
    """Specification item 6 fines a purchase you cannot *afford*. Being
    second to the last copy is not that, and charging for it would punish
    people for someone else's speed."""
    if not supports_row_locks:
        pytest.skip("needs a database with row-level locking")

    book_id, buyer_ids = contested
    Session = session_factory()

    def buy(index):
        with Session() as session:
            person = session.get(m.User, buyer_ids[index])
            try:
                checkout.place_order(session, person, config)
                session.commit()
            except checkout.CheckoutError:
                session.rollback()
            return None

    _run_together(buy, len(buyer_ids))

    with session_factory()() as session:
        fines = session.execute(
            select(m.Transaction).where(m.Transaction.kind == "fine")
        ).scalars().all()
        assert fines == []


def test_two_customers_buying_the_same_pair_do_not_deadlock(
        supports_row_locks, session, config, make_book, make_user):
    """Locks are taken in ascending book id. Without that, two customers
    buying books A and B in opposite orders deadlock."""
    if not supports_row_locks:
        pytest.skip("needs a database with row-level locking")

    first = make_book(title="Book A", price="5.00", stock=20)
    second = make_book(title="Book B", price="5.00", stock=20)
    people = [make_user(balance="500.00") for _ in range(8)]

    for index, person in enumerate(people):
        # Half add A then B, half add B then A.
        order = [first, second] if index % 2 == 0 else [second, first]
        for book in order:
            cart.add(session, person, book.id, 1)
    session.commit()

    Session = session_factory()
    ids = [person.id for person in people]

    def buy(index):
        with Session() as db:
            person = db.get(m.User, ids[index])
            checkout.place_order(db, person, config)
            db.commit()
            return True

    results, errors = _run_together(buy, len(ids))
    assert not errors, "a deadlock or serialisation failure: %s" % errors
    assert all(results)

    with session_factory()() as db:
        assert db.get(m.Book, first.id).stock == 20 - len(ids)
        assert db.get(m.Book, second.id).stock == 20 - len(ids)
