"""The business rules, one requirement at a time.

Each test is named after the rule it checks, so a failure says which
requirement broke rather than which function did.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bookstore.db import models as m
from bookstore.services import accounts, cart, checkout, pricing, reviews


# ----------------------------------------------------- item 6: money


def test_a_purchase_moves_money_and_stock(session, config, make_book, make_user):
    book = make_book(price="10.00", stock=3)
    person = make_user(balance="50.00")

    cart.add(session, person, book.id, 1)
    result = checkout.place_order(session, person, config)

    assert result.order.total == Decimal("10.00")
    assert person.balance == Decimal("40.00")
    assert book.stock == 2


def test_a_purchase_you_cannot_afford_is_refused_and_fined(
        session, config, make_book, make_user):
    """Item 6: the attempt costs $1 even though it does not succeed."""
    book = make_book(price="40.00")
    person = make_user(balance="5.00")
    cart.add(session, person, book.id, 1)

    with pytest.raises(checkout.CheckoutError) as refused:
        checkout.place_order(session, person, config)

    assert refused.value.fine_applied == Decimal("1.00")
    assert person.balance == Decimal("4.00")
    assert book.stock == 5, "a refused purchase must not consume stock"


def test_a_negative_balance_suspends_the_account(
        session, config, make_book, make_user):
    book = make_book(price="40.00")
    person = make_user(balance="0.50")
    cart.add(session, person, book.id, 1)

    with pytest.raises(checkout.CheckoutError):
        checkout.place_order(session, person, config)

    assert person.balance < 0
    assert person.suspended


def test_a_suspended_account_cannot_buy(session, config, make_book, make_user):
    book = make_book()
    person = make_user(balance="100.00")
    cart.add(session, person, book.id, 1)
    accounts.suspend(session, person, "testing")

    with pytest.raises(checkout.CheckoutError, match="suspended"):
        checkout.place_order(session, person, config)


def test_only_a_deposit_that_clears_the_debt_allows_reopening(
        session, make_user):
    person = make_user(balance="-5.00")
    accounts.suspend(session, person, "negative balance")

    with pytest.raises(accounts.AccountError):
        accounts.reopen(session, person, deposit_amount=Decimal("1.00"))
    assert person.suspended

    accounts.reopen(session, person, deposit_amount=Decimal("10.00"))
    assert not person.suspended
    assert person.balance == Decimal("6.00")


# ------------------------------------------- items 8: VIP and discount


def test_a_vip_pays_ten_percent_less(session, config, make_book, make_user):
    book = make_book(price="20.00")
    vip = make_user(role=m.VIP, balance="100.00")
    ordinary = make_user(balance="100.00")

    assert pricing.price_for(vip, book, config.vip_discount) == Decimal("18.00")
    assert pricing.price_for(ordinary, book, config.vip_discount) == Decimal("20.00")
    assert pricing.price_for(None, book, config.vip_discount) == Decimal("20.00")


def test_more_than_three_purchases_promotes_to_vip(
        session, config, make_book, make_user):
    book = make_book(price="1.00", stock=20)
    person = make_user(balance="100.00")

    for _ in range(config.vip_purchase_threshold):
        cart.add(session, person, book.id, 1)
        checkout.place_order(session, person, config)
    assert person.role == m.REGISTERED, "exactly 3 is not 'more than 3'"

    cart.add(session, person, book.id, 1)
    result = checkout.place_order(session, person, config)
    assert result.promoted
    assert person.role == m.VIP


def test_spending_more_than_the_threshold_promotes_to_vip(
        session, config, make_book, make_user):
    book = make_book(price="250.00", stock=5)
    person = make_user(balance="400.00")

    cart.add(session, person, book.id, 1)
    result = checkout.place_order(session, person, config)

    assert result.promoted
    assert person.role == m.VIP


def test_the_super_user_is_never_promoted(session, config, make_book, make_user):
    book = make_book(price="250.00")
    boss = make_user(role=m.SUPER_USER, balance="900.00")

    cart.add(session, boss, book.id, 1)
    result = checkout.place_order(session, boss, config)

    assert not result.promoted
    assert boss.role == m.SUPER_USER


# --------------------------------------------- item 7: reviews and moderation


def test_a_purchaser_rating_counts_double(session, config, make_book, make_user):
    book = make_book(price="5.00", stock=10)
    buyer = make_user(balance="100.00")
    bystander = make_user(balance="100.00")

    cart.add(session, buyer, book.id, 1)
    checkout.place_order(session, buyer, config)

    reviews.submit(session, buyer, book, 5, "Loved it.", config)
    reviews.submit(session, bystander, book, 1, "Did not.", config)

    summary = reviews.rating_summary(session, book.id)
    assert summary["plain_average"] == pytest.approx(3.0)
    # (5*2 + 1*1) / 3 = 3.667
    assert summary["weighted_average"] == pytest.approx(3.667, abs=0.01)


def test_any_user_may_comment_including_a_visitor(
        session, config, make_book):
    """Item 7 says *any* user can comment on any book."""
    book = make_book()
    outcome = reviews.submit(session, None, book, 4, "Rather good.", config)

    assert outcome.ok
    assert outcome.review.user_id is None
    assert outcome.review.weight == Decimal("1.00")


def test_taboo_words_are_masked(session, config, make_book, make_user):
    book = make_book()
    person = make_user()

    outcome = reviews.submit(session, person, book, 3,
                             "What a garbage ending.", config)

    assert outcome.ok
    assert "garbage" not in outcome.review.comment
    assert "*" in outcome.review.comment
    assert outcome.review.raw_comment.count("garbage") == 1, \
        "the original is kept for the super-user"


def test_more_than_three_taboo_words_blocks_and_warns(
        session, config, make_book, make_user):
    book = make_book()
    person = make_user()

    outcome = reviews.submit(
        session, person, book, 1,
        "stupid garbage trash idiot moron", config)

    assert not outcome.ok
    assert outcome.blocked
    assert outcome.warning_issued
    assert person.warnings == 1


def test_exactly_three_taboo_words_is_masked_not_blocked(
        session, config, make_book, make_user):
    """The rule is *more than* three; the boundary is worth pinning."""
    book = make_book()
    person = make_user()

    outcome = reviews.submit(session, person, book, 2,
                             "stupid garbage trash", config)

    assert outcome.ok
    assert not outcome.blocked
    assert person.warnings == 0


def test_more_than_two_warnings_suspends_a_customer(
        session, config, make_book, make_user):
    person = make_user()
    for index in range(config.warning_limit):
        accounts.apply_warning(session, person, "test %d" % index, config)
    assert not person.suspended

    suspended, demoted = accounts.apply_warning(session, person, "final", config)
    assert suspended and not demoted
    assert person.suspended


def test_more_than_two_warnings_demotes_a_vip(session, config, make_user):
    vip = make_user(role=m.VIP)
    for index in range(config.warning_limit + 1):
        accounts.apply_warning(session, vip, "test %d" % index, config)

    assert vip.role == m.REGISTERED
    assert not vip.suspended, "a VIP is demoted, not suspended"
    assert vip.vip_ineligible, "and cannot simply be promoted straight back"


def test_a_demoted_vip_is_not_promoted_again(
        session, config, make_book, make_user):
    """Items 7 and 8 contradict each other here; this is the documented
    resolution, switchable in settings.json."""
    book = make_book(price="250.00", stock=5)
    person = make_user(role=m.VIP, balance="400.00")
    for index in range(config.warning_limit + 1):
        accounts.apply_warning(session, person, "test %d" % index, config)
    assert person.role == m.REGISTERED

    cart.add(session, person, book.id, 1)
    result = checkout.place_order(session, person, config)

    assert not result.promoted
    assert person.role == m.REGISTERED


# ------------------------------------------------- item 7: order lists


def _rate(session, config, book, make_user, stars_list):
    for stars in stars_list:
        reviews.submit(session, make_user(), book, stars, "A view.", config)


def test_more_than_three_low_ratings_make_a_no_order_book(
        session, config, make_book, make_user):
    book = make_book(title="Disliked")
    _rate(session, config, book, make_user, [1, 1, 2, 1])

    lists = reviews.order_lists(session, config)
    assert [entry["title"] for entry in lists["no_order"]] == ["Disliked"]


def test_more_than_three_high_ratings_make_a_to_order_book(
        session, config, make_book, make_user):
    book = make_book(title="Adored")
    _rate(session, config, book, make_user, [5, 5, 4, 5])

    lists = reviews.order_lists(session, config)
    assert [entry["title"] for entry in lists["to_order"]] == ["Adored"]


def test_three_ratings_are_not_enough_for_either_list(
        session, config, make_book, make_user):
    book = make_book(title="Barely Rated")
    _rate(session, config, book, make_user, [1, 1, 1])

    lists = reviews.order_lists(session, config)
    assert lists["no_order"] == []
    assert lists["to_order"] == []
