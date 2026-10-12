"""Tests for every business rule in the spec.

Run with:

    python -m unittest discover -s tests -v

Each test names the spec requirement it covers, so the suite doubles as a
traceability matrix: if a rule is reworded, the failing test says which one.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bookstore import models                      # noqa: E402
from bookstore.catalog import Catalog, stem, tokenize  # noqa: E402
from bookstore.config import Config               # noqa: E402
from bookstore.moderation import screen_comment   # noqa: E402
from bookstore.rag import Retriever, split_into_chunks  # noqa: E402
from bookstore.recommend import Recommender       # noqa: E402
from bookstore.rules import RuleError, RulesEngine  # noqa: E402
from bookstore.storage import Storage             # noqa: E402


class BookstoreTestCase(unittest.TestCase):
    """Builds a throwaway store in a temp directory for each test."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="bookstore-test-")
        self.config = Config(path=os.path.join(self.tmp, "settings.json"))
        self.config.set("taboo_words", ["damn", "garbage", "trash", "idiot",
                                        "stupid", "utter rubbish"])
        self.storage = Storage(os.path.join(self.tmp, "data"))
        self.rules = RulesEngine(self.storage, self.config)
        self.catalog = Catalog(self.storage)
        self.recommender = Recommender(self.storage, self.rules)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers --------------------------------------------------------

    def make_user(self, username="tester", role=models.REGISTERED, balance=100.0):
        return self.rules.create_account(username, "password", role=role,
                                         balance=balance)

    def make_book(self, title="A Book", author="An Author",
                  category="literature", year=2000, price=10.0, stock=5):
        return self.catalog.add_book(title, author, category, year, price,
                                     stock=stock)


# ===========================================================================
# R1 -- roles
# ===========================================================================


class TestRoles(BookstoreTestCase):
    def test_four_roles_exist_with_a_privilege_order(self):
        self.assertEqual(
            sorted(models.ROLE_RANK, key=lambda r: models.ROLE_RANK[r]),
            [models.VISITOR, models.REGISTERED, models.VIP, models.SUPER_USER],
        )

    def test_role_at_least_is_inclusive_upwards(self):
        self.assertTrue(models.role_at_least(models.SUPER_USER, models.VISITOR))
        self.assertTrue(models.role_at_least(models.VIP, models.REGISTERED))
        self.assertFalse(models.role_at_least(models.REGISTERED, models.VIP))

    def test_new_accounts_default_to_registered_customer(self):
        user = self.make_user()
        self.assertEqual(user["role"], models.REGISTERED)

    def test_duplicate_usernames_are_rejected(self):
        self.make_user("alice")
        with self.assertRaises(RuleError):
            self.make_user("alice")

    def test_passwords_are_hashed_not_stored(self):
        user = self.make_user("bob")
        self.assertNotIn("password", user)
        self.assertNotEqual(user["password_hash"], "password")
        self.assertTrue(models.verify_password("password", user["salt"],
                                               user["password_hash"]))
        self.assertFalse(models.verify_password("wrong", user["salt"],
                                                user["password_hash"]))

    def test_a_corrupt_stored_credential_refuses_rather_than_raising(self):
        """A hand-edited or truncated users.json must not crash the login."""
        for salt, digest in (("aabb", "not-hexadecimal"),
                             ("aabb", "abc"),          # odd-length hex
                             ("", ""),
                             ("zz", "aabb"),
                             ("aabb", "")):
            self.assertFalse(models.verify_password("anything", salt, digest))


# ===========================================================================
# R6 -- balance, fines, suspension
# ===========================================================================


class TestPurchasesAndBalance(BookstoreTestCase):
    def test_affordable_purchase_succeeds_and_debits_the_balance(self):
        user = self.make_user(balance=50.0)
        book = self.make_book(price=12.50)
        result = self.rules.purchase(user, book)
        self.assertTrue(result.ok)
        self.assertAlmostEqual(user["balance"], 37.50, places=2)
        self.assertEqual(user["purchase_count"], 1)
        self.assertEqual(book["stock"], 4)

    def test_unaffordable_purchase_is_refused_and_fined(self):
        """R6, default reading: purchase refused, $1 fine applied."""
        user = self.make_user(balance=5.0)
        book = self.make_book(price=20.0)
        result = self.rules.purchase(user, book)
        self.assertFalse(result.ok)
        self.assertAlmostEqual(result.fine_applied, 1.0, places=2)
        self.assertAlmostEqual(user["balance"], 4.0, places=2)
        self.assertEqual(user["purchase_count"], 0, "no purchase should be recorded")
        self.assertEqual(book["stock"], 5, "stock must not move")

    def test_overdraft_flag_lets_the_purchase_through(self):
        """The alternative reading, switched on in settings.json."""
        self.config.set("spec_interpretation",
                        {"allow_overdraft_purchase": True,
                         "demotion_blocks_repromotion": True,
                         "order_lists_use_weighted_average": True,
                         "visitors_may_review": False})
        user = self.make_user(balance=5.0)
        book = self.make_book(price=20.0)
        result = self.rules.purchase(user, book)
        self.assertTrue(result.ok)
        # 5 - 1 fine - 20 price = -16
        self.assertAlmostEqual(user["balance"], -16.0, places=2)
        self.assertTrue(user["suspended"], "a negative balance must suspend")

    def test_negative_balance_suspends_the_account(self):
        user = self.make_user(balance=0.5)
        book = self.make_book(price=20.0)
        self.rules.purchase(user, book)      # 0.50 - 1.00 = -0.50
        self.assertLess(user["balance"], 0)
        self.assertTrue(user["suspended"])
        allowed, why = self.rules.can_log_in(user)
        self.assertFalse(allowed)
        self.assertIn("suspended", why.lower())

    def test_suspended_account_cannot_purchase(self):
        user = self.make_user(balance=0.5)
        book = self.make_book(price=20.0)
        self.rules.purchase(user, book)
        self.assertTrue(user["suspended"])
        cheap = self.make_book(title="Cheap", price=0.01)
        result = self.rules.purchase(user, cheap)
        self.assertFalse(result.ok)
        self.assertIn("suspended", result.message.lower())

    def test_reopening_requires_a_positive_balance(self):
        user = self.make_user(balance=0.5)
        book = self.make_book(price=20.0)
        self.rules.purchase(user, book)
        self.assertTrue(user["suspended"])

        # Too small a deposit leaves it negative and must be refused.
        with self.assertRaises(RuleError):
            self.rules.reopen_account(user, 0.10)
        self.assertTrue(user["suspended"])

        self.rules.reopen_account(user, 20.0)
        self.assertFalse(user["suspended"])
        self.assertGreater(user["balance"], 0)
        self.assertEqual(user["warnings"], 0)

    def test_out_of_stock_books_cannot_be_bought(self):
        user = self.make_user(balance=100.0)
        book = self.make_book(price=5.0, stock=0)
        result = self.rules.purchase(user, book)
        self.assertFalse(result.ok)
        self.assertIn("out of stock", result.message.lower())

    def test_deposit_must_be_positive(self):
        user = self.make_user()
        with self.assertRaises(RuleError):
            self.rules.deposit(user, -5.0)


# ===========================================================================
# R11 -- VIP promotion and discount
# ===========================================================================


class TestVipPromotion(BookstoreTestCase):
    def test_promotion_after_more_than_three_purchases(self):
        user = self.make_user(balance=500.0)
        books = [self.make_book(title="B%d" % i, price=5.0) for i in range(4)]
        for book in books[:3]:
            self.rules.purchase(user, book)
        self.assertEqual(user["role"], models.REGISTERED,
                         "three purchases is not 'more than three'")
        self.rules.purchase(user, books[3])
        self.assertEqual(user["role"], models.VIP)

    def test_promotion_after_spending_more_than_the_threshold(self):
        user = self.make_user(balance=500.0)
        book = self.make_book(price=250.0, stock=5)
        self.rules.purchase(user, book)
        self.assertEqual(user["role"], models.VIP)
        self.assertEqual(user["purchase_count"], 1)

    def test_vip_gets_ten_percent_off(self):
        user = self.make_user(role=models.VIP, balance=100.0)
        book = self.make_book(price=20.0)
        self.assertAlmostEqual(self.rules.price_for(user, book), 18.0, places=2)
        result = self.rules.purchase(user, book)
        self.assertTrue(result.ok)
        self.assertAlmostEqual(result.price_paid, 18.0, places=2)
        self.assertAlmostEqual(user["balance"], 82.0, places=2)

    def test_visitors_and_registered_customers_pay_list_price(self):
        book = self.make_book(price=20.0)
        registered = self.make_user("rc", role=models.REGISTERED)
        self.assertAlmostEqual(self.rules.price_for(None, book), 20.0, places=2)
        self.assertAlmostEqual(self.rules.price_for(registered, book), 20.0, places=2)

    def test_super_user_is_never_promoted(self):
        user = self.make_user(role=models.SUPER_USER, balance=500.0)
        for i in range(5):
            self.rules.purchase(user, self.make_book(title="B%d" % i, price=5.0))
        self.assertEqual(user["role"], models.SUPER_USER)


# ===========================================================================
# R9 -- taboo words
# ===========================================================================


class TestModeration(BookstoreTestCase):
    WORDS = ["damn", "garbage", "trash", "idiot", "stupid", "utter rubbish"]

    def test_taboo_words_are_masked_character_for_character(self):
        result = screen_comment("This is garbage.", self.WORDS)
        self.assertEqual(result.masked, "This is *******.")
        self.assertEqual(result.hits, 1)
        self.assertFalse(result.blocked)

    def test_masking_is_case_insensitive(self):
        result = screen_comment("GARBAGE and Garbage", self.WORDS)
        self.assertEqual(result.hits, 2)
        self.assertNotIn("arbage", result.masked)

    def test_whole_word_matching_does_not_censor_substrings(self):
        result = screen_comment("The trashcan is classic.", self.WORDS)
        self.assertEqual(result.hits, 0, "'trash' inside 'trashcan' must not match")
        self.assertEqual(result.masked, "The trashcan is classic.")

    def test_more_than_three_taboo_words_blocks_the_comment(self):
        text = "garbage trash idiot stupid"       # four
        result = screen_comment(text, self.WORDS)
        self.assertEqual(result.hits, 4)
        self.assertTrue(result.blocked)

    def test_exactly_three_is_not_blocked(self):
        result = screen_comment("garbage trash idiot", self.WORDS)
        self.assertEqual(result.hits, 3)
        self.assertFalse(result.blocked, "'more than 3' means 4 or more")

    def test_occurrences_are_counted_not_distinct_words(self):
        result = screen_comment("damn damn damn damn", self.WORDS)
        self.assertEqual(result.hits, 4)
        self.assertTrue(result.blocked)

    def test_multi_word_taboo_phrases_match(self):
        result = screen_comment("This is utter rubbish.", self.WORDS)
        self.assertEqual(result.hits, 1)
        self.assertIn("*", result.masked)

    def test_clean_comment_is_untouched(self):
        result = screen_comment("A thoughtful and careful edition.", self.WORDS)
        self.assertTrue(result.clean)
        self.assertEqual(result.masked, "A thoughtful and careful edition.")


# ===========================================================================
# R7, R9, R10 -- reviews, warnings and their consequences
# ===========================================================================


class TestReviews(BookstoreTestCase):
    def test_any_user_may_comment_including_a_visitor(self):
        """R7 opens with "Any user can comment on any book"."""
        book = self.make_book()
        result = self.rules.submit_review(None, book, 5, "Lovely")
        self.assertTrue(result.ok, result.message)

        reviews = self.storage.reviews_for_book(book["book_id"])
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["role_at_time"], models.VISITOR)
        self.assertFalse(reviews[0]["verified_purchase"])
        self.assertAlmostEqual(reviews[0]["weight"],
                               self.config.normal_weight, places=3)

    def test_a_visitor_rating_counts_towards_the_average(self):
        book = self.make_book()
        self.rules.submit_review(None, book, 4, "Good")
        self.rules.submit_review(None, book, 2, "Mixed")
        summary = self.rules.rating_summary(book["book_id"])
        self.assertEqual(summary.count, 2)
        self.assertAlmostEqual(summary.plain_average, 3.0, places=2)

    def test_a_visitor_comment_is_still_screened_and_blocked(self):
        """The filter applies to everyone; only the warning has nowhere to go."""
        book = self.make_book()
        masked = self.rules.submit_review(None, book, 3, "A damn shame")
        self.assertTrue(masked.ok)
        self.assertEqual(masked.taboo_hits, 1)
        shown = self.storage.reviews_for_book(book["book_id"])[0]["comment"]
        self.assertNotIn("damn", shown)

        blocked = self.rules.submit_review(
            None, self.make_book(title="Other"), 1,
            "garbage trash idiot stupid")
        self.assertFalse(blocked.ok)
        self.assertTrue(blocked.blocked)

    def test_a_blocked_visitor_comment_warns_nobody_but_is_recorded(self):
        book = self.make_book()
        self.rules.submit_review(None, book, 1, "garbage trash idiot stupid")
        # Nothing to warn: no account exists for a visitor.
        self.assertEqual([u for u in self.storage.list_users()
                          if int(u.get("warnings", 0)) > 0], [])
        # But the super-user can still see that it happened.
        kinds = [e["kind"] for e in self.storage.events]
        self.assertIn("blocked_visitor_comment", kinds)

    def test_visitor_reviewing_can_be_switched_off(self):
        self.config.set("spec_interpretation",
                        {"allow_overdraft_purchase": False,
                         "demotion_blocks_repromotion": True,
                         "order_lists_use_weighted_average": True,
                         "visitors_may_review": False})
        result = self.rules.submit_review(None, self.make_book(), 5, "Lovely")
        self.assertFalse(result.ok)
        self.assertIn("log in", result.message.lower())

    def test_a_signed_in_purchaser_still_outweighs_a_visitor(self):
        book = self.make_book(price=10.0)
        buyer = self.make_user("buyer", balance=100.0)
        self.rules.purchase(buyer, book)
        self.rules.submit_review(buyer, book, 5, "Loved it")
        self.rules.submit_review(None, book, 1, "Hated it")
        summary = self.rules.rating_summary(book["book_id"])
        # (5*2 + 1*1) / 3
        self.assertAlmostEqual(summary.weighted_average, 11 / 3.0, places=2)
        self.assertAlmostEqual(summary.plain_average, 3.0, places=2)

    def test_stars_must_be_between_one_and_five(self):
        user = self.make_user()
        book = self.make_book()
        for stars in (0, 6, -1):
            result = self.rules.submit_review(user, book, stars, "x")
            self.assertFalse(result.ok)

    def test_a_user_can_only_review_a_book_once(self):
        user = self.make_user()
        book = self.make_book()
        self.assertTrue(self.rules.submit_review(user, book, 4, "Good").ok)
        second = self.rules.submit_review(user, book, 5, "Even better")
        self.assertFalse(second.ok)
        self.assertIn("already", second.message.lower())

    def test_purchaser_reviews_carry_double_weight(self):
        buyer = self.make_user("buyer", balance=100.0)
        other = self.make_user("other", balance=100.0)
        book = self.make_book(price=10.0)
        self.rules.purchase(buyer, book)

        self.rules.submit_review(buyer, book, 5, "Wonderful")
        self.rules.submit_review(other, book, 1, "Awful")

        summary = self.rules.rating_summary(book["book_id"])
        self.assertEqual(summary.count, 2)
        self.assertAlmostEqual(summary.plain_average, 3.0, places=2)
        # (5*2 + 1*1) / 3 = 3.667
        self.assertAlmostEqual(summary.weighted_average, 11 / 3.0, places=2)
        self.assertEqual(summary.purchaser_count, 1)

    def test_blocked_comment_issues_a_warning_and_is_not_shown(self):
        user = self.make_user()
        book = self.make_book()
        result = self.rules.submit_review(
            user, book, 1, "garbage trash idiot stupid"
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertTrue(result.warning_issued)
        self.assertEqual(user["warnings"], 1)
        self.assertEqual(self.storage.reviews_for_book(book["book_id"]), [],
                         "a blocked review must not appear publicly")
        self.assertEqual(
            len(self.storage.reviews_for_book(book["book_id"], include_blocked=True)),
            1, "but it is kept for the super-user's moderation view",
        )

    def test_blocked_reviews_do_not_count_towards_the_average(self):
        book = self.make_book()
        good = self.make_user("good")
        bad = self.make_user("bad")
        self.rules.submit_review(good, book, 5, "Excellent")
        self.rules.submit_review(bad, book, 1, "garbage trash idiot stupid")
        summary = self.rules.rating_summary(book["book_id"])
        self.assertEqual(summary.count, 1)
        self.assertAlmostEqual(summary.weighted_average, 5.0, places=2)

    def test_masked_comment_is_still_posted(self):
        user = self.make_user()
        book = self.make_book()
        result = self.rules.submit_review(user, book, 3, "A damn shame really")
        self.assertTrue(result.ok)
        self.assertEqual(result.taboo_hits, 1)
        reviews = self.storage.reviews_for_book(book["book_id"])
        self.assertEqual(len(reviews), 1)
        self.assertIn("****", reviews[0]["comment"])
        self.assertNotIn("damn", reviews[0]["comment"])

    def test_registered_customer_suspended_after_more_than_two_warnings(self):
        """R10."""
        user = self.make_user()
        books = [self.make_book(title="B%d" % i) for i in range(3)]
        nasty = "garbage trash idiot stupid"

        self.rules.submit_review(user, books[0], 1, nasty)
        self.assertEqual(user["warnings"], 1)
        self.assertFalse(user["suspended"])

        self.rules.submit_review(user, books[1], 1, nasty)
        self.assertEqual(user["warnings"], 2)
        self.assertFalse(user["suspended"], "two warnings is not 'more than two'")

        result = self.rules.submit_review(user, books[2], 1, nasty)
        self.assertEqual(user["warnings"], 3)
        self.assertTrue(user["suspended"])
        self.assertTrue(result.suspended)

    def test_vip_demoted_instead_of_suspended(self):
        """R10: a VIP is demoted to RC rather than suspended."""
        user = self.make_user(role=models.VIP)
        books = [self.make_book(title="B%d" % i) for i in range(3)]
        nasty = "garbage trash idiot stupid"
        for book in books:
            self.rules.submit_review(user, book, 1, nasty)

        self.assertEqual(user["role"], models.REGISTERED)
        self.assertFalse(user["suspended"])
        self.assertTrue(user["vip_ineligible"])

    def test_demoted_vip_is_not_instantly_repromoted(self):
        """The spec conflict: a demoted VIP still has the purchase history that
        earned VIP in the first place."""
        user = self.make_user(role=models.VIP, balance=500.0)
        for i in range(5):
            self.rules.purchase(user, self.make_book(title="P%d" % i, price=5.0))
        self.assertGreater(user["purchase_count"], 3)

        for i in range(3):
            self.rules.submit_review(user, self.make_book(title="R%d" % i), 1,
                                     "garbage trash idiot stupid")
        self.assertEqual(user["role"], models.REGISTERED)

        # Another purchase must NOT promote them back.
        self.rules.purchase(user, self.make_book(title="After", price=5.0))
        self.assertEqual(user["role"], models.REGISTERED)

        # Until the super-user clears it.
        user["vip_ineligible"] = False
        self.rules.purchase(user, self.make_book(title="After2", price=5.0))
        self.assertEqual(user["role"], models.VIP)


# ===========================================================================
# R8 -- no-order and to-order lists
# ===========================================================================


class TestOrderLists(BookstoreTestCase):
    def _rate(self, book, scores):
        # Usernames are namespaced by book so two books can be rated in one test.
        for i, stars in enumerate(scores):
            user = self.make_user("rater_%s_%d" % (book["book_id"], i))
            self.rules.submit_review(user, book, stars, "A comment")

    def test_three_ratings_is_not_enough(self):
        book = self.make_book()
        self._rate(book, [1, 1, 1])
        self.assertEqual(self.rules.list_status(book["book_id"]), "normal",
                         "'more than 3 ratings' means 4 or more")

    def test_four_low_ratings_make_the_no_order_list(self):
        book = self.make_book()
        self._rate(book, [1, 1, 2, 2])          # average 1.5
        self.assertEqual(self.rules.list_status(book["book_id"]), "no-order")

    def test_four_high_ratings_make_the_to_order_list(self):
        book = self.make_book()
        self._rate(book, [5, 5, 4, 5])          # average 4.75
        self.assertEqual(self.rules.list_status(book["book_id"]), "to-order")

    def test_boundaries_are_strict(self):
        """Exactly 2.0 is not 'below 2'; exactly 4.0 is not 'above 4'."""
        low = self.make_book(title="Exactly two")
        self._rate(low, [2, 2, 2, 2])
        self.assertEqual(self.rules.list_status(low["book_id"]), "normal")

        high = self.make_book(title="Exactly four")
        self._rate(high, [4, 4, 4, 4])
        self.assertEqual(self.rules.list_status(high["book_id"]), "normal")

    def test_order_lists_collects_both(self):
        bad = self.make_book(title="Bad")
        good = self.make_book(title="Good")
        self._rate(bad, [1, 1, 1, 1])
        self._rate(good, [5, 5, 5, 5])
        lists = self.rules.order_lists()
        self.assertEqual([r["book"]["title"] for r in lists["no_order"]], ["Bad"])
        self.assertEqual([r["book"]["title"] for r in lists["to_order"]], ["Good"])

    def test_weighting_can_change_which_list_a_book_lands_on(self):
        """A verified buyer's 5 outweighs two non-buyers' 1s."""
        book = self.make_book(price=10.0)
        buyer = self.make_user("buyer", balance=100.0)
        self.rules.purchase(buyer, book)
        self.rules.submit_review(buyer, book, 5, "Loved it")
        for i in range(3):
            self.rules.submit_review(self.make_user("n%d" % i), book, 1, "Hated it")

        summary = self.rules.rating_summary(book["book_id"])
        self.assertAlmostEqual(summary.plain_average, 2.0, places=2)
        # (5*2 + 1+1+1) / 5 = 2.6
        self.assertAlmostEqual(summary.weighted_average, 2.6, places=2)
        self.assertEqual(self.rules.list_status(book["book_id"]), "normal")


# ===========================================================================
# R2 -- recommendations
# ===========================================================================


class TestRecommendations(BookstoreTestCase):
    def setUp(self):
        super().setUp()
        self.books = [
            self.make_book(title="Sci One", author="Sagan", category="science"),
            self.make_book(title="Sci Two", author="Sagan", category="science"),
            self.make_book(title="Sci Three", author="Hawking", category="science"),
            self.make_book(title="Poem One", author="Frost", category="poetry"),
            self.make_book(title="Poem Two", author="Frost", category="poetry"),
        ]

    def test_visitors_get_store_wide_popularity(self):
        buyer = self.make_user("buyer", balance=500.0)
        self.rules.purchase(buyer, self.books[3])
        self.rules.purchase(buyer, self.books[3])

        recs = self.recommender.top_for(None, 3)
        self.assertEqual(len(recs), 3)
        self.assertEqual(recs[0].book["title"], "Poem One",
                         "the most purchased title should lead")

    def test_super_user_also_gets_the_store_wide_view(self):
        admin = self.make_user("admin", role=models.SUPER_USER)
        buyer = self.make_user("buyer", balance=500.0)
        self.rules.purchase(buyer, self.books[2])
        recs = self.recommender.top_for(admin, 3)
        self.assertEqual(recs[0].book["title"], "Sci Three")

    def test_registered_customer_gets_personalised_results(self):
        user = self.make_user("reader", balance=500.0)
        # A clear taste for science, and for Sagan in particular.
        self.rules.purchase(user, self.books[0])
        self.rules.record_browse(user, self.books[1])

        recs = self.recommender.top_for(user, 3)
        titles = [r.book["title"] for r in recs]
        self.assertNotIn("Sci One", titles, "already purchased")
        self.assertEqual(titles[0], "Sci Two",
                         "same author and category should rank first")

    def test_no_order_books_are_never_recommended_personally(self):
        user = self.make_user("reader", balance=500.0)
        self.rules.purchase(user, self.books[0])

        # Tank Sci Two's rating so it lands on the no-order list.
        for i in range(4):
            self.rules.submit_review(self.make_user("r%d" % i), self.books[1], 1, "no")
        self.assertEqual(self.rules.list_status(self.books[1]["book_id"]), "no-order")

        titles = [r.book["title"] for r in self.recommender.top_for(user, 3)]
        self.assertNotIn("Sci Two", titles)

    def test_new_account_falls_back_to_popularity(self):
        user = self.make_user("fresh")
        recs = self.recommender.top_for(user, 3)
        self.assertEqual(len(recs), 3, "a brand-new account still sees a shelf")

    def test_recommendations_are_capped_at_three_by_default(self):
        user = self.make_user("reader", balance=500.0)
        self.rules.purchase(user, self.books[0])
        self.assertLessEqual(
            len(self.recommender.top_for(user, self.config.recommendation_count)), 3
        )


# ===========================================================================
# R5 -- browsing and keyword search
# ===========================================================================


class TestCatalogue(BookstoreTestCase):
    def setUp(self):
        super().setUp()
        self.make_book(title="Moby Dick", author="Herman Melville",
                       category="literature", year=1851)
        self.make_book(title="On the Origin of Species", author="Charles Darwin",
                       category="science", year=1859)
        self.make_book(title="Relativity", author="Albert Einstein",
                       category="science", year=1916)

    def test_browse_by_category(self):
        self.assertEqual(len(self.catalog.by_category("science")), 2)
        self.assertEqual(len(self.catalog.by_category("literature")), 1)
        self.assertEqual(dict(self.catalog.categories())["science"], 2)

    def test_browse_by_author(self):
        self.assertEqual(len(self.catalog.by_author("Charles Darwin")), 1)
        self.assertEqual(len(self.catalog.authors()), 3)

    def test_browse_by_decade(self):
        self.assertEqual(len(self.catalog.by_decade("1850s")), 2)
        self.assertEqual(len(self.catalog.by_decade("1910s")), 1)
        labels = [label for label, _ in self.catalog.decades()]
        self.assertIn("1850s", labels)

    def test_keyword_search_matches_title_and_author(self):
        hits = self.catalog.search("darwin")
        self.assertTrue(hits)
        self.assertEqual(hits[0].book["title"], "On the Origin of Species")

        hits = self.catalog.search("moby")
        self.assertEqual(hits[0].book["title"], "Moby Dick")

    def test_search_returns_nothing_for_an_absent_term(self):
        self.assertEqual(self.catalog.search("quantum chromodynamics"), [])

    def test_search_is_stemmed(self):
        self.assertEqual(stem("opening"), "open")
        self.assertEqual(stem("policies"), "policy")
        self.assertEqual(stem("shipping"), "ship")

    def test_stemming_is_consistent_across_inflections(self):
        """The property that actually matters: a query term and the document
        term it should match must stem to the same thing."""
        for group in (("rate", "rated", "rating", "ratings"),
                      ("open", "opens", "opened", "opening"),
                      ("book", "books"),
                      ("ship", "ships", "shipping", "shipped")):
            stems = {stem(word) for word in group}
            self.assertEqual(len(stems), 1,
                             "%r stemmed inconsistently to %r" % (group, stems))

    def test_stemming_is_idempotent(self):
        for word in ("ratings", "opening", "policies", "shipping", "readers"):
            self.assertEqual(stem(stem(word)), stem(word))

    def test_stopwords_are_dropped(self):
        self.assertNotIn("what", tokenize("What is the price"))
        self.assertIn("price", tokenize("What is the price"))

    def test_adding_a_book_validates_its_fields(self):
        with self.assertRaises(ValueError):
            self.catalog.add_book("", "Author", "science", 2000, 10.0)
        with self.assertRaises(ValueError):
            self.catalog.add_book("Title", "Author", "science", 2000, -1.0)
        with self.assertRaises(ValueError):
            self.catalog.add_book("Title", "Author", "science", 2000, "free")

    def test_new_books_are_searchable_immediately(self):
        self.catalog.search("darwin")                      # builds the index
        self.catalog.add_book("Cosmos", "Carl Sagan", "science", 1980, 15.0)
        hits = self.catalog.search("cosmos")
        self.assertTrue(hits)
        self.assertEqual(hits[0].book["title"], "Cosmos")


# ===========================================================================
# R12, R13 -- RAG retrieval and routing
# ===========================================================================


class TestRag(BookstoreTestCase):
    def setUp(self):
        super().setUp()
        os.makedirs(self.storage.kb_dir, exist_ok=True)
        with open(os.path.join(self.storage.kb_dir, "store.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write(
                "# Opening hours\n"
                "Monday to Friday: 9:00 to 19:00. Saturday: 10:00 to 18:00.\n"
                "Sunday: 12:00 to 17:00.\n\n"
                "# Returns\n"
                "Unopened books may be returned within 30 days for a full "
                "refund to the account balance.\n\n"
                "# The team\n"
                "Margaret Osei is the owner and buyer. Mielikki is the shop "
                "cat and sleeps on the philosophy shelf.\n"
            )
        self.retriever = Retriever(self.storage)

    def test_chunks_are_split_on_headings(self):
        chunks = split_into_chunks("# A\nbody a\n\n# B\nbody b\n")
        self.assertEqual([h for h, _ in chunks], ["A", "B"])

    def test_the_knowledge_base_is_indexed(self):
        self.assertGreaterEqual(self.retriever.size, 3)
        self.assertIn("store.txt", self.retriever.sources)

    def test_a_local_question_retrieves_the_right_chunk(self):
        hits = self.retriever.retrieve("what are your opening hours")
        self.assertTrue(hits)
        self.assertEqual(hits[0].chunk.heading, "Opening hours")

    def test_stemming_lets_open_match_opening(self):
        hits = self.retriever.retrieve("when are you open on Sunday")
        self.assertTrue(hits)
        self.assertEqual(hits[0].chunk.heading, "Opening hours")

    def test_a_local_question_is_routed_to_rag(self):
        local, hits = self.retriever.is_local_question("what is your returns policy",
                                                       0.15)
        self.assertTrue(local)
        self.assertEqual(hits[0].chunk.heading, "Returns")

    def test_an_unrelated_question_is_not_routed_to_rag(self):
        """R13: anything not in local files must go to the LLM instead."""
        for question in ("what is the capital of Mongolia",
                         "explain quantum entanglement",
                         "who won the world cup in 1998"):
            local, _hits = self.retriever.is_local_question(question, 0.15)
            self.assertFalse(local, "%r should not be answered from local files"
                                    % question)

    def test_rag_still_works_on_a_very_small_knowledge_base(self):
        """Regression: the rare-term budget is a ratio of corpus size, so on a
        two-passage corpus nothing qualified as rare and RAG never fired."""
        import tempfile
        small_dir = tempfile.mkdtemp(dir=self.tmp)
        storage = Storage(small_dir)
        os.makedirs(storage.kb_dir, exist_ok=True)
        with open(os.path.join(storage.kb_dir, "tiny.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write("# Opening hours\nMonday to Friday 9 to 19.\n\n"
                         "# The team\nMargaret owns the shop.\n")
        retriever = Retriever(storage)
        self.assertEqual(retriever.size, 2, "expected a two-passage corpus")

        local, hits = retriever.is_local_question("what are your opening hours",
                                                  0.15)
        self.assertTrue(local, "a local question must still reach RAG")
        self.assertEqual(hits[0].chunk.heading, "Opening hours")

        off_topic, _ = retriever.is_local_question(
            "what is the capital of Mongolia", 0.15)
        self.assertFalse(off_topic, "an off-topic question must still go to the LLM")

    def test_a_term_absent_from_the_corpus_is_never_rare(self):
        hits = self.retriever.retrieve("mongolia capital ulaanbaatar")
        for hit in hits:
            self.assertEqual(hit.rare_matches, [],
                             "absent terms must not count as evidence")

    def test_rare_term_support_is_required(self):
        local, _ = self.retriever.is_local_question("what what what", 0.0)
        self.assertFalse(local, "filler words alone must never trigger RAG")

    def test_the_index_notices_an_edited_file(self):
        before = self.retriever.size
        with open(os.path.join(self.storage.kb_dir, "extra.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write("# Parking\nThere is metered parking on Mercer Street "
                         "and a car park on Alder Lane.\n")
        self.retriever.reload()
        self.assertGreater(self.retriever.size, before)
        hits = self.retriever.retrieve("where can I park")
        self.assertTrue(hits)
        self.assertEqual(hits[0].chunk.heading, "Parking")


# ===========================================================================
# R3 -- role-tiered chat
# ===========================================================================


class TestChatTiering(BookstoreTestCase):
    def setUp(self):
        super().setUp()
        from bookstore.chat import ChatService, SOURCE_DENIED, SOURCE_STORE
        self.SOURCE_DENIED = SOURCE_DENIED
        self.SOURCE_STORE = SOURCE_STORE
        os.makedirs(self.storage.kb_dir, exist_ok=True)
        with open(os.path.join(self.storage.kb_dir, "store.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write("# Opening hours\nMonday to Friday: 9:00 to 19:00.\n")
        self.make_book(title="Moby Dick", author="Herman Melville")
        self.chat = ChatService(self.storage, self.config, self.rules,
                                self.catalog, self.recommender)

    def test_only_the_super_user_sees_the_finance_report(self):
        question = "show me the finance report"
        admin = self.make_user("admin", role=models.SUPER_USER)
        vip = self.make_user("vip", role=models.VIP)
        rc = self.make_user("rc", role=models.REGISTERED)

        self.assertEqual(self.chat.ask(admin, question).source, self.SOURCE_STORE)
        for user in (vip, rc, None):
            answer = self.chat.ask(user, question)
            self.assertTrue(answer.denied_for_role,
                            "%s must not see finances" % (user or {}).get("role", "VS"))

    def test_vip_sees_the_order_lists_but_a_registered_customer_does_not(self):
        question = "what is on the to-order list"
        vip = self.make_user("vip", role=models.VIP)
        rc = self.make_user("rc", role=models.REGISTERED)
        self.assertEqual(self.chat.ask(vip, question).source, self.SOURCE_STORE)
        self.assertTrue(self.chat.ask(rc, question).denied_for_role)

    def test_registered_customer_sees_their_own_balance_but_a_visitor_does_not(self):
        question = "what is my balance"
        rc = self.make_user("rc", role=models.REGISTERED, balance=42.0)
        answer = self.chat.ask(rc, question)
        self.assertEqual(answer.source, self.SOURCE_STORE)
        self.assertIn("42.00", answer.text)
        self.assertTrue(self.chat.ask(None, question).denied_for_role)

    def test_everyone_can_ask_about_the_catalogue(self):
        question = "do you have Moby Dick"
        for user in (None, self.make_user("rc"),
                     self.make_user("su", role=models.SUPER_USER)):
            answer = self.chat.ask(user, question)
            self.assertFalse(answer.denied_for_role)
            self.assertIn("Moby Dick", answer.text)

    def test_the_answer_always_declares_its_source(self):
        answer = self.chat.ask(None, "what are your opening hours")
        self.assertTrue(answer.formatted().startswith("["))

    def test_help_lists_more_for_a_higher_role(self):
        visitor_help = self.chat.ask(None, "help").text
        admin = self.make_user("admin", role=models.SUPER_USER)
        admin_help = self.chat.ask(admin, "help").text
        self.assertGreater(admin_help.count("*"), visitor_help.count("*"))


# ===========================================================================
# Storage
# ===========================================================================


class TestStorage(BookstoreTestCase):
    def test_data_survives_a_reload(self):
        user = self.make_user("alice", balance=30.0)
        book = self.make_book(title="Persisted")
        self.rules.purchase(user, book)
        self.storage.save_all()

        reloaded = Storage(self.storage.data_dir)
        self.assertIn(user["user_id"], reloaded.users)
        self.assertIn(book["book_id"], reloaded.books)
        self.assertEqual(reloaded.users[user["user_id"]]["purchase_count"], 1)
        self.assertEqual(len(reloaded.transactions), 2)   # deposit + purchase

    def test_ids_are_sequential(self):
        a = self.make_book(title="A")
        b = self.make_book(title="B")
        self.assertEqual(a["book_id"], "b001")
        self.assertEqual(b["book_id"], "b002")

    def test_a_corrupt_file_does_not_crash_loading(self):
        path = os.path.join(self.storage.data_dir, "books.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json at all")
        reloaded = Storage(self.storage.data_dir)
        self.assertEqual(reloaded.books, {})
        self.assertTrue(os.path.exists(path + ".corrupt"),
                        "the bad file should be preserved for inspection")

    def test_older_records_are_upgraded_with_missing_fields(self):
        legacy = {"user_id": "u900", "username": "legacy", "role": models.REGISTERED}
        models.normalise_user(legacy)
        self.assertIn("vip_ineligible", legacy)
        self.assertIn("purchase_history", legacy)
        self.assertEqual(legacy["warnings"], 0)


# ===========================================================================
# Finance (R3's data source)
# ===========================================================================


class TestFinance(BookstoreTestCase):
    def test_report_adds_up(self):
        from bookstore.finance import FinanceService
        user = self.make_user("alice", balance=100.0)
        book = self.make_book(price=20.0, stock=5)
        self.rules.purchase(user, book)
        self.rules.purchase(user, book)

        poor = self.make_user("poor", balance=1.0)
        self.rules.purchase(poor, book)          # refused, fined $1

        report = FinanceService(self.storage).report()
        self.assertAlmostEqual(report.revenue_sales, 40.0, places=2)
        self.assertAlmostEqual(report.revenue_fines, 1.0, places=2)
        self.assertEqual(report.purchase_count, 2)
        self.assertEqual(report.fine_count, 1)
        self.assertAlmostEqual(report.total_revenue, 41.0, places=2)
        self.assertAlmostEqual(report.average_order, 20.0, places=2)

    def test_customer_liability_counts_positive_balances(self):
        from bookstore.finance import FinanceService
        self.make_user("a", balance=50.0)
        self.make_user("b", balance=25.0)
        report = FinanceService(self.storage).report()
        self.assertAlmostEqual(report.customer_liability, 75.0, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
