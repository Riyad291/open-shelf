"""The SQL projection must agree with the rules engine, and never outrank it.

Two kinds of test live here, and the first is the important one.

**Agreement.** The reports in ``analytics.py`` recompute, in SQL, numbers that
``rules.py`` and ``finance.py`` already compute in Python: the weighted rating
average, the order lists, total revenue. Two implementations of the same
arithmetic is exactly the setup where they quietly drift apart and a report
starts contradicting the application. These tests pin them together, so a
change to either side that breaks the other fails here.

**Containment.** Specification note 13 keeps the text files as the system of
record. The projection must stay a projection: no writes, nothing secret
inside it, and deleting it must lose nothing.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bookstore import models                        # noqa: E402
from bookstore.analytics import Analytics           # noqa: E402
from bookstore.catalog import Catalog               # noqa: E402
from bookstore.config import Config                 # noqa: E402
from bookstore.finance import FinanceService        # noqa: E402
from bookstore.rules import RulesEngine             # noqa: E402
from bookstore.storage import Storage               # noqa: E402


class AnalyticsTestCase(unittest.TestCase):
    """A small shop with enough history for every report to have rows."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="analytics-")
        cls.db = Storage(os.path.join(cls.tmp, "data"))
        cls.cfg = Config(os.path.join(cls.tmp, "settings.json"))
        cls.cfg.set("taboo_words", ["idiot", "garbage", "trash", "stupid"])
        cls.rules = RulesEngine(cls.db, cls.cfg)
        cls.catalog = Catalog(cls.db)
        cls.finance = FinanceService(cls.db)
        cls._seed()
        cls.sql = Analytics(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.sql.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _seed(cls):
        r, cat = cls.rules, cls.catalog

        cls.loved = cat.add_book("A Loved Book", "Ann", "literature",
                                 1950, 10.00, stock=20)
        cls.hated = cat.add_book("A Hated Book", "Bo", "science",
                                 1960, 20.00, stock=20)
        cls.quiet = cat.add_book("An Ignored Book", "Cy", "history",
                                 1970, 30.00, stock=5)

        cls.buyers = [r.create_account("buyer%d" % i, "pw12345", balance=500.0)
                      for i in range(5)]

        # Purchases, so revenue and best-sellers have something to total.
        for buyer in cls.buyers[:4]:
            r.purchase(buyer, cls.db.get_book(cls.loved["book_id"]))
        for buyer in cls.buyers[:2]:
            r.purchase(buyer, cls.db.get_book(cls.hated["book_id"]))

        # Four ratings each way, which is "more than 3" -- the threshold the
        # specification sets for a book to reach an order list.
        for buyer in cls.buyers[:4]:
            r.submit_review(buyer, cls.db.get_book(cls.loved["book_id"]),
                            5, "Wonderful.")
            r.submit_review(buyer, cls.db.get_book(cls.hated["book_id"]),
                            1, "Not for me.")

        # One comment with taboo words, so moderation has a row.
        r.submit_review(cls.buyers[4], cls.db.get_book(cls.loved["book_id"]),
                        2, "What an idiot, total garbage.")

        # Someone browses the ignored book but never buys it.
        r.record_browse(cls.buyers[0], cls.db.get_book(cls.quiet["book_id"]))


class TestAgreesWithTheRulesEngine(AnalyticsTestCase):
    """The reports must not contradict the application."""

    def test_weighted_average_matches_rules_engine(self):
        by_id = {row["title"]: row for row in self.sql.order_lists()}
        for book in (self.loved, self.hated):
            with self.subTest(book=book["title"]):
                truth = self.rules.rating_summary(book["book_id"])
                row = by_id[book["title"]]
                self.assertEqual(row["rating_count"], truth.count)
                self.assertAlmostEqual(row["weighted_average"],
                                       truth.weighted_average, places=2)
                self.assertAlmostEqual(row["plain_average"],
                                       truth.plain_average, places=2)

    def test_order_list_classification_matches_rules_engine(self):
        rows = {row["title"]: row["list_status"]
                for row in self.sql.order_lists()}
        for book in (self.loved, self.hated, self.quiet):
            with self.subTest(book=book["title"]):
                expected = self.rules.list_status(book["book_id"])
                self.assertEqual(rows.get(book["title"], "normal"), expected)

    def test_the_order_lists_are_not_empty(self):
        """A test that passes because both sides agree on nothing would be
        worthless, so check the fixture actually produced both lists."""
        statuses = {row["list_status"] for row in self.sql.order_lists()}
        self.assertIn("to-order", statuses)
        self.assertIn("no-order", statuses)

    def test_total_revenue_matches_the_finance_service(self):
        """The running total at the last month is the shop's whole revenue."""
        months = self.sql.revenue_by_month()
        self.assertTrue(months)
        self.assertAlmostEqual(months[-1]["running_total"],
                               self.finance.report().total_revenue,
                               places=2)

    def test_monthly_lines_match_the_finance_service(self):
        report = self.finance.report()
        months = self.sql.revenue_by_month()
        self.assertAlmostEqual(sum(m["sales"] for m in months),
                               round(report.revenue_sales, 2), places=2)
        self.assertAlmostEqual(sum(m["fines"] for m in months),
                               round(report.revenue_fines, 2), places=2)

    def test_units_sold_match_each_book_s_own_counter(self):
        for row in self.sql.best_sellers():
            with self.subTest(title=row["title"]):
                book = next(b for b in self.db.list_books()
                            if b["title"] == row["title"])
                self.assertEqual(row["units"], book["purchase_count"])

    def test_category_revenue_sums_to_total_revenue(self):
        categories = self.sql.category_performance()
        total = round(sum(row["revenue"] for row in categories), 2)
        sales = round(sum(t["amount"] for t in self.db.transactions
                          if t.get("kind") == "purchase"), 2)
        self.assertAlmostEqual(total, sales, places=2)

    def test_percentages_add_up_to_a_hundred(self):
        categories = self.sql.category_performance()
        self.assertAlmostEqual(
            sum(row["pct_of_revenue"] or 0 for row in categories), 100.0,
            delta=0.5)

    def test_customer_tiers_match_the_stored_roles(self):
        rows = {row["username"]: row for row in self.sql.customer_value()}
        for user in self.db.users.values():
            if user["role"] == models.SUPER_USER:
                self.assertNotIn(user["username"], rows,
                                 "the super-user is staff, not a customer")
                continue
            self.assertEqual(rows[user["username"]]["role"], user["role"])


class TestReportContent(AnalyticsTestCase):

    def test_dead_stock_finds_the_browsed_but_unbought_book(self):
        titles = [row["title"] for row in self.sql.dead_stock()]
        self.assertIn(self.quiet["title"], titles)
        self.assertNotIn(self.loved["title"], titles)

    def test_moderation_reports_the_taboo_comment(self):
        rows = self.sql.moderation()
        self.assertTrue(rows)
        self.assertTrue(any(row["taboo_words"] > 0 for row in rows))

    def test_a_category_with_no_sales_still_appears(self):
        """A LEFT JOIN, not an INNER one: an empty category is the finding."""
        categories = [row["category"] for row in self.sql.category_performance()]
        self.assertIn("history", categories)

    def test_best_sellers_rank_within_their_category(self):
        for row in self.sql.best_sellers():
            self.assertGreaterEqual(row["rank_in_category"], 1)

    def test_every_report_returns_plain_dictionaries(self):
        for name, rows in self.sql.report().items():
            with self.subTest(report=name):
                self.assertIsInstance(rows, list)
                for row in rows:
                    self.assertIsInstance(row, dict)


class TestStaysAProjection(AnalyticsTestCase):
    """Specification note 13: the text files remain the system of record."""

    def test_writes_are_refused(self):
        for statement in ("DELETE FROM books",
                          "UPDATE users SET balance = 999999",
                          "DROP TABLE reviews",
                          "INSERT INTO books VALUES ('x','t','a','c',1,1,1,0,0,'')"):
            with self.subTest(statement=statement.split()[0]):
                with self.assertRaises(ValueError):
                    self.sql.query(statement)

    def test_no_credential_is_projected(self):
        columns = {row["name"] for row in
                   self.sql.query("SELECT name FROM pragma_table_info('users')")}
        for secret in ("salt", "password_hash"):
            self.assertNotIn(secret, columns)

    def test_rebuilding_is_idempotent(self):
        before = self.sql.report()
        self.sql.build()
        self.sql.build()
        self.assertEqual(self.sql.report(), before)

    def test_the_projection_follows_the_text_files(self):
        """Change the shop, rebuild, and the report moves with it."""
        before = len(self.sql.query("SELECT book_id FROM books"))
        self.catalog.add_book("Added Later", "Dee", "poetry", 1999, 5.0,
                              stock=1)
        self.sql.build()
        after = len(self.sql.query("SELECT book_id FROM books"))
        self.assertEqual(after, before + 1)

    def test_deleting_the_projection_loses_nothing(self):
        path = os.path.join(self.tmp, "throwaway.db")
        first = Analytics(self.db, path=path)
        expected = first.report()
        first.close()
        os.remove(path)

        rebuilt = Analytics(self.db, path=path)
        self.assertEqual(rebuilt.report(), expected)
        rebuilt.close()
        os.remove(path)

    def test_the_shop_runs_with_no_projection_present(self):
        """Nothing in the application may import this module to work."""
        import subprocess
        source_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        hits = subprocess.run(
            ["grep", "-rn", "analytics", "--include=*.py",
             os.path.join(source_root, "bookstore", "ui"),
             os.path.join(source_root, "bookstore", "rules.py"),
             os.path.join(source_root, "bookstore", "storage.py")],
            capture_output=True, text=True).stdout
        self.assertEqual(hits.strip(), "",
                         "the rules and the GUI must not depend on reporting")

    def test_foreign_keys_are_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql.conn.execute(
                "INSERT INTO reviews VALUES "
                "('rX','no-such-book','no-such-user','RC',3,1.0,0,0,0,'')")


if __name__ == "__main__":
    unittest.main(verbosity=2)
