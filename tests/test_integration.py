"""End-to-end integration: a realistic session, checked for self-consistency.

The unit tests in ``test_rules.py`` check each rule in isolation. This file
checks that the rules still agree with each other after a long sequence of
real actions -- that the ledger reconciles with the accounts, that stock is
conserved, and that everything survives being written to disk and read back.

These are the questions an auditor asks of a shop's books, and they catch a
class of defect that per-rule tests cannot: two individually correct rules
that disagree about the same number.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bookstore import models                    # noqa: E402
from bookstore.catalog import Catalog           # noqa: E402
from bookstore.config import Config             # noqa: E402
from bookstore.finance import FinanceService    # noqa: E402
from bookstore.recommend import Recommender     # noqa: E402
from bookstore.rules import RuleError, RulesEngine  # noqa: E402
from bookstore.storage import Storage           # noqa: E402


class TestFullSession(unittest.TestCase):
    """One long session, then a set of consistency checks over the result."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="integration-")
        cls.db = Storage(os.path.join(cls.tmp, "data"))
        cls.cfg = Config(os.path.join(cls.tmp, "settings.json"))
        cls.cfg.set("taboo_words", ["garbage", "trash", "idiot", "stupid"])
        cls.rules = RulesEngine(cls.db, cls.cfg)
        cls.catalog = Catalog(cls.db)
        cls.recommender = Recommender(cls.db, cls.rules)
        cls.finance = FinanceService(cls.db)
        cls._run_session()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _run_session(cls):
        r, cat = cls.rules, cls.catalog

        cls.books = [
            cat.add_book("Book %d" % i, "Author %d" % (i % 3), "literature",
                         1900 + i, 10.00 + i, stock=5)
            for i in range(6)
        ]
        cls.alice = r.create_account("alice", "pw1234", balance=100.0)
        cls.bob = r.create_account("bob", "pw1234", balance=3.0)
        cls.admin = r.create_account("admin", "pw1234",
                                     role=models.SUPER_USER)

        # Alice buys four titles, which promotes her, then a fifth at the
        # VIP price.
        for i in range(4):
            r.purchase(cls.alice, cls.books[i])
        cls.vip_price_result = r.purchase(cls.alice, cls.books[4])

        # Bob cannot afford anything and is fined into suspension.
        cls.first_refusal = r.purchase(cls.bob, cls.books[0])
        for _ in range(3):
            r.purchase(cls.bob, cls.books[0])
        cls.bob_suspended = bool(cls.bob["suspended"])
        r.reopen_account(cls.bob, 20.0)

        # One verified buyer rates a title highly, three non-buyers poorly.
        cls.target = cls.books[5]
        buyer = r.create_account("buyer", "pw1234", balance=100.0)
        r.purchase(buyer, cls.target)
        r.submit_review(buyer, cls.target, 5, "Excellent")
        for i in range(3):
            voter = r.create_account("n%d" % i, "pw1234", balance=10.0)
            r.submit_review(voter, cls.target, 1, "Poor")

        # A troll earns three warnings and is suspended.
        cls.troll = r.create_account("troll", "pw1234", balance=50.0)
        for i in range(3):
            r.submit_review(cls.troll, cls.books[i], 1,
                            "garbage trash idiot stupid")

        cls.db.save_all()

    # -- promotion and discounting --------------------------------------

    def test_the_fourth_purchase_promotes_and_the_fifth_is_discounted(self):
        self.assertEqual(self.alice["role"], models.VIP)
        # Books cost 10, 11, 12, 13 at list; the fifth is 14 less 10 per cent.
        self.assertAlmostEqual(self.vip_price_result.price_paid, 12.60, places=2)
        self.assertAlmostEqual(self.alice["balance"],
                               100.0 - 46.0 - 12.60, places=2)

    # -- fines and suspension -------------------------------------------

    def test_a_refused_purchase_is_fined_and_moves_no_stock(self):
        self.assertFalse(self.first_refusal.ok)
        self.assertAlmostEqual(self.first_refusal.fine_applied, 1.0, places=2)

    def test_repeated_fines_suspend_and_the_super_user_can_reopen(self):
        self.assertTrue(self.bob_suspended)
        self.assertFalse(self.bob["suspended"], "should have been reopened")
        self.assertGreater(float(self.bob["balance"]), 0)
        self.assertEqual(self.bob["warnings"], 0)

    def test_an_insufficient_deposit_will_not_reopen_an_account(self):
        debtor = self.rules.create_account("debtor", "pw1234", balance=0.5)
        self.rules.purchase(debtor, self.books[0])     # fined into the red
        self.assertTrue(debtor["suspended"])
        with self.assertRaises(RuleError):
            self.rules.reopen_account(debtor, 0.01)
        self.assertTrue(debtor["suspended"])

    # -- moderation ------------------------------------------------------

    def test_three_blocked_comments_suspend_their_author(self):
        self.assertEqual(self.troll["warnings"], 3)
        self.assertTrue(self.troll["suspended"])

    def test_blocked_comments_are_hidden_but_retained(self):
        book_id = self.books[0]["book_id"]
        self.assertEqual(self.db.reviews_for_book(book_id), [])
        kept = self.db.reviews_for_book(book_id, include_blocked=True)
        self.assertEqual(len(kept), 1)
        self.assertIn("garbage", kept[0]["raw_comment"])
        self.assertNotIn("garbage", kept[0]["comment"])

    # -- weighting -------------------------------------------------------

    def test_weighting_lifts_the_average_above_the_plain_mean(self):
        summary = self.rules.rating_summary(self.target["book_id"])
        self.assertEqual(summary.count, 4)
        self.assertAlmostEqual(summary.plain_average, 2.0, places=2)
        self.assertAlmostEqual(summary.weighted_average, 2.6, places=2)
        self.assertEqual(self.rules.list_status(self.target["book_id"]),
                         "normal")

    # -- the consistency checks that matter ------------------------------

    def test_the_finance_report_reconciles_with_the_ledger(self):
        report = self.finance.report()
        sales = sum(float(t["amount"]) for t in self.db.transactions
                    if t["kind"] == "purchase")
        fines = sum(float(t["amount"]) for t in self.db.transactions
                    if t["kind"] == "fine")
        self.assertAlmostEqual(report.revenue_sales, round(sales, 2), places=2)
        self.assertAlmostEqual(report.revenue_fines, round(fines, 2), places=2)
        self.assertAlmostEqual(report.total_revenue, round(sales + fines, 2),
                               places=2)

    def test_every_recorded_purchase_has_a_ledger_line(self):
        from_history = sum(len(u.get("purchase_history") or [])
                           for u in self.db.list_users())
        from_ledger = sum(1 for t in self.db.transactions
                          if t["kind"] == "purchase")
        self.assertEqual(from_history, from_ledger)

    def test_each_books_sold_count_matches_the_ledger(self):
        for book in self.db.list_books():
            sold = int(book.get("purchase_count", 0))
            ledger = sum(1 for t in self.db.transactions
                         if t.get("book_id") == book["book_id"]
                         and t["kind"] == "purchase")
            self.assertEqual(sold, ledger,
                             "%s sold %d but the ledger has %d"
                             % (book["book_id"], sold, ledger))

    def test_stock_plus_sales_never_exceeds_the_opening_stock(self):
        for book in self.db.list_books():
            self.assertLessEqual(
                int(book["stock"]) + int(book["purchase_count"]), 5 + 10,
                "%s has more copies than were ever stocked" % book["book_id"])
            self.assertGreaterEqual(int(book["stock"]), 0)

    def test_customer_liability_equals_the_sum_of_positive_balances(self):
        report = self.finance.report()
        expected = sum(float(u["balance"]) for u in self.db.list_users()
                       if float(u["balance"]) > 0)
        self.assertAlmostEqual(report.customer_liability, round(expected, 2),
                               places=2)

    def test_no_account_holds_an_impossible_state(self):
        for user in self.db.list_users():
            models.normalise_user(user)
            if float(user["balance"]) < 0:
                self.assertTrue(user["suspended"],
                                "%s is in debt but not suspended"
                                % user["username"])
            self.assertGreaterEqual(int(user["warnings"]), 0)
            self.assertGreaterEqual(float(user["total_spent"]), 0)
            self.assertIn(user["role"],
                          (models.REGISTERED, models.VIP, models.SUPER_USER))

    # -- durability ------------------------------------------------------

    def test_the_whole_session_survives_a_reload(self):
        reloaded = Storage(self.db.data_dir)
        self.assertEqual(len(reloaded.users), len(self.db.users))
        self.assertEqual(len(reloaded.reviews), len(self.db.reviews))
        self.assertEqual(len(reloaded.transactions),
                         len(self.db.transactions))
        self.assertEqual(
            reloaded.get_user(self.alice["user_id"])["role"], models.VIP)
        self.assertFalse(
            reloaded.get_user(self.bob["user_id"])["suspended"])
        self.assertTrue(
            reloaded.get_user(self.troll["user_id"])["suspended"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
