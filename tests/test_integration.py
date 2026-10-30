"""One long realistic session, then audited.

The per-rule tests check each rule in isolation. This checks that the rules
still agree with each other after a sequence of real actions: that the
administrator's figures match the rows they summarise, that no account is
left in a state the rules say cannot exist, that standing was earned the way
the rules say, and that the lists agree with the ratings behind them.

Per-rule tests cannot catch two individually correct rules that disagree
about the same number. These can.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from bookstore.db import models as m
from bookstore.ingest.parsers import ParsedResource
from bookstore.ingest.pipeline import get_or_create_source, reconcile
from bookstore.services import accounts, insights, resources, reviews


@pytest.fixture()
def busy_library(session, config, make_book, make_user):
    """A library that has been used: reviews, promotion, moderation, imports."""
    threshold = config.trusted_review_threshold
    books = [
        make_book(title="Book %d" % i, author="Author %d" % (i % 3),
                  category=["literature", "science", "history"][i % 3],
                  year=1900 + i * 7)
        for i in range(max(6, threshold + 2))
    ]

    # Ada earns trust, then writes once more as a trusted contributor.
    ada = make_user("ada")
    for book in books[:threshold]:
        reviews.submit(session, ada, book, 4, "Worth reading.", config)
    assert ada.role == m.VIP
    reviews.submit(session, ada, books[threshold], 5, "Superb.", config)

    # Four ordinary members, who love one book and dislike another.
    members = [make_user(name) for name in ("ben", "cleo", "dan", "eve")]
    for person in members:
        reviews.submit(session, person, books[4], 5, "Loved it.", config)
        reviews.submit(session, person, books[5], 1, "Not for me.", config)

    # One masked comment and one blocked one.
    reviews.submit(session, members[0], books[0], 3,
                   "A garbage ending, otherwise fine.", config)
    reviews.submit(session, members[1], books[1], 1,
                   "stupid garbage trash idiot", config)

    # An import, some clicks, and a link that died.
    source = get_or_create_source(session, "probe", name="Probe Source",
                                  attribution="Probe attribution")
    reconcile(session, source, [
        ParsedResource(external_id="r%d" % i, title="Resource %d" % i,
                       url="https://example.org/r%d" % i,
                       topics=["Python" if i % 2 else "Rust"])
        for i in range(8)
    ])
    first = session.execute(
        select(m.Resource).order_by(m.Resource.id)).scalars().first()
    for _ in range(3):
        resources.record_click(session, first.id)
    last = session.execute(
        select(m.Resource).order_by(m.Resource.id.desc())).scalars().first()
    last.dead = True
    session.commit()

    return {"books": books, "ada": ada, "members": members}


class TestReportedFiguresMatchTheRows:

    def test_resource_counts(self, session, busy_library):
        summary = insights.summary(session)
        assert summary["resources"] == 7
        assert summary["dead_links"] == 1

    def test_opens_equal_the_clicks_recorded(self, session, busy_library):
        clicks = session.execute(
            select(func.sum(m.Resource.click_count))).scalar_one()
        assert insights.summary(session)["total_opens"] == clicks == 3

    def test_review_counts_split_blocked_from_accepted(self, session,
                                                       busy_library):
        summary = insights.summary(session)
        total = session.execute(select(func.count(m.Review.id))).scalar_one()
        assert summary["reviews"] + summary["blocked_reviews"] == total
        assert summary["blocked_reviews"] == 1

    def test_the_topic_report_includes_every_topic(self, session,
                                                   busy_library):
        reported = {row["topic"] for row in insights.topic_coverage(session)}
        stored = set(session.execute(select(m.Topic.name)).scalars())
        assert reported == stored


class TestModerationStateIsConsistent:

    def test_a_blocked_comment_is_hidden_but_kept(self, session,
                                                  busy_library):
        blocked = session.execute(
            select(m.Review).where(m.Review.blocked.is_(True))).scalars().all()
        assert len(blocked) == 1
        assert "garbage" in blocked[0].raw_comment, "kept for the record"

    def test_a_masked_comment_shows_no_taboo_word(self, session,
                                                  busy_library):
        masked = session.execute(
            select(m.Review).where(m.Review.blocked.is_(False),
                                   m.Review.taboo_hits > 0)).scalars().all()
        assert masked and all("garbage" not in r.comment for r in masked)

    def test_every_warning_has_an_event(self, session, busy_library):
        for user in session.execute(select(m.User)).scalars():
            events = session.execute(
                select(func.count(m.Event.id))
                .where(m.Event.user_id == user.id,
                       m.Event.kind == "warning")).scalar_one()
            assert events == user.warnings, user.username

    def test_no_account_holds_an_impossible_state(self, session,
                                                  busy_library, config):
        for user in session.execute(select(m.User)).scalars():
            assert user.warnings >= 0
            if user.warnings > config.warning_limit:
                assert user.suspended or user.role != m.VIP


class TestStandingIsEarned:

    def test_every_trusted_contributor_earned_it(self, session, busy_library,
                                                 config):
        for user in session.execute(
                select(m.User).where(m.User.role == m.VIP)).scalars():
            assert (reviews.accepted_review_count(session, user)
                    >= config.trusted_review_threshold)

    def test_promotion_was_recorded(self, session, busy_library):
        promoted = session.execute(
            select(func.count(m.Event.id))
            .where(m.Event.kind == "promoted",
                   m.Event.user_id == busy_library["ada"].id)).scalar_one()
        assert promoted == 1

    def test_only_reviews_written_after_promotion_weigh_double(
            self, session, busy_library):
        """Weight is fixed when a review is written, not recomputed later."""
        weights = session.execute(
            select(m.Review.trusted_reviewer, m.Review.weight)
            .where(m.Review.user_id == busy_library["ada"].id)).all()
        trusted = [w for t, w in weights if t]
        ordinary = [w for t, w in weights if not t]
        assert trusted, "the review written as a trusted contributor"
        assert ordinary and max(ordinary) < min(trusted)


class TestTheListsAgreeWithTheRatings:

    def test_the_loved_book_is_recommended(self, session, busy_library,
                                           config):
        lists = reviews.order_lists(session, config)
        assert "Book 4" in [e["title"] for e in lists["to_order"]]

    def test_the_disliked_book_is_not(self, session, busy_library, config):
        lists = reviews.order_lists(session, config)
        assert "Book 5" in [e["title"] for e in lists["no_order"]]


class TestTheCatalogueSurvivesBeingReadBack:

    def test_dead_links_are_not_browsable(self, session, busy_library):
        items, total = resources.list_resources(session)
        assert total == 7 and not any(r.dead for r in items)

    def test_every_resource_has_a_topic(self, session, busy_library):
        untagged = session.execute(
            select(func.count(m.Resource.id))
            .where(~m.Resource.id.in_(select(m.ResourceTopic.resource_id)))
        ).scalar_one()
        assert untagged == 0

    def test_the_analytics_run_end_to_end(self, session, busy_library):
        report = insights.analytics(session)
        assert report["most_opened"][0]["opens"] == 3
        assert report["catalogue_growth"][-1]["total"] == 8
        assert report["import_history"] == [], "reconcile alone is not a run"

    def test_a_suspended_account_cannot_review(self, session, busy_library,
                                               config):
        person = busy_library["members"][2]
        accounts.suspend(session, person, "testing")
        with pytest.raises(reviews.ReviewError):
            reviews.submit(session, person, busy_library["books"][2], 3,
                           "Again.", config)
