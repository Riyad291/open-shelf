"""The business rules, one requirement at a time.

Each test is named after the rule it checks, so a failure says which
requirement broke rather than which function did.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bookstore.db import models as m
from bookstore.services import accounts, reviews


# ------------------------------------------- items 6 and 8: standing


def test_a_suspended_member_cannot_review(session, config, make_book,
                                          make_user):
    book = make_book()
    person = make_user()
    accounts.suspend(session, person, "testing")

    with pytest.raises(reviews.ReviewError, match="suspended"):
        reviews.submit(session, person, book, 4, "Fine.", config)


def test_reopening_keeps_warnings_by_default(session, config, make_user):
    person = make_user()
    accounts.apply_warning(session, person, "one", config)
    accounts.suspend(session, person, "testing")

    accounts.reopen(session, person)
    assert not person.suspended
    assert person.warnings == 1, "a reopened account is not a clean slate"


def test_an_administrator_can_clear_warnings_on_reopening(
        session, config, make_user):
    person = make_user()
    accounts.apply_warning(session, person, "one", config)
    accounts.suspend(session, person, "testing")

    accounts.reopen(session, person, clear_warnings=True)
    assert person.warnings == 0


def test_enough_accepted_reviews_promote_a_member(
        session, config, make_book, make_user):
    person = make_user()
    for index in range(config.trusted_review_threshold):
        reviews.submit(session, person, make_book(title="B%d" % index), 4,
                       "Good.", config)

    assert person.role == m.VIP


def test_one_review_short_is_not_enough(session, config, make_book,
                                        make_user):
    person = make_user()
    for index in range(config.trusted_review_threshold - 1):
        reviews.submit(session, person, make_book(title="B%d" % index), 4,
                       "Good.", config)

    assert person.role == m.REGISTERED


def test_blocked_reviews_do_not_count_towards_promotion(
        session, config, make_book, make_user):
    person = make_user()
    for index in range(config.trusted_review_threshold):
        reviews.submit(session, person, make_book(title="B%d" % index), 1,
                       "stupid garbage trash idiot moron", config)

    assert person.role != m.VIP


def test_an_administrator_is_never_promoted(session, config, make_book,
                                            make_user):
    boss = make_user(role=m.SUPER_USER)
    for index in range(config.trusted_review_threshold):
        reviews.submit(session, boss, make_book(title="B%d" % index), 4,
                       "Good.", config)

    assert boss.role == m.SUPER_USER


# ------------------------------------------------------ review weighting


def test_a_trusted_contributor_rating_counts_double(
        session, config, make_book, make_user):
    book = make_book()
    trusted = make_user(role=m.VIP)
    ordinary = make_user()

    reviews.submit(session, trusted, book, 5, "Loved it.", config)
    reviews.submit(session, ordinary, book, 1, "Did not.", config)

    summary = reviews.rating_summary(session, book.id)
    assert summary["plain_average"] == pytest.approx(3.0)
    # (5*2 + 1*1) / 3 = 3.667
    assert summary["weighted_average"] == pytest.approx(3.667, abs=0.01)


# ------------------------------------------------------------- moderation


def test_any_user_may_comment_including_a_visitor(
        session, config, make_book):
    """Reviews and moderation says *any* user can comment on any book."""
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
    assert not vip.suspended, "a trusted contributor is demoted, not suspended"
    assert vip.promotion_blocked, "and cannot simply be promoted straight back"


def test_a_demoted_contributor_is_not_promoted_again(
        session, config, make_book, make_user):
    """Without promotion_blocked, the next accepted review would undo the
    demotion."""
    person = make_user(role=m.VIP)
    for index in range(config.warning_limit + 1):
        accounts.apply_warning(session, person, "test %d" % index, config)
    assert person.role == m.REGISTERED

    for index in range(config.trusted_review_threshold + 1):
        reviews.submit(session, person, make_book(title="B%d" % index), 4,
                       "Good.", config)

    assert person.role == m.REGISTERED


# ------------------------------------------------------ recommendation lists


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
