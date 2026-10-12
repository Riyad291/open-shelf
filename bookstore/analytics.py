"""A SQL view over the shop, projected from the text files.

Why this exists
---------------
Specification note 13 says the shop must keep its records in text files, not
a database, and it does: ``data/*.json`` is the system of record and the only
thing any rule reads or writes. Nothing in this module is ever written back,
and deleting the database loses nothing -- ``build()`` recreates it from the
text files in under a second.

What it is for is the other half of the job: questions of shape

    "revenue by category by month, with each category's running total and its
     rank against the others"

are a single readable query in SQL and a tangle of nested dictionaries in
Python. The business *rules* stay in ``rules.py``, where the specification
wants them. The *reporting* happens here, over a throwaway projection.

The separation is strict and worth stating plainly, because it is the part a
reviewer should check:

    text files  ->  build()  ->  SQLite  ->  SELECT
         ^                                      |
         |______________ never ________________ |

``build()`` is the only writer, it only ever writes to the projection, and
every query below is a SELECT. There is no path from this module back to
``data/``.

Usage
-----
    from bookstore.analytics import Analytics

    with Analytics(storage) as sql:          # in memory by default
        rows = sql.revenue_by_month()

    python -m bookstore.analytics            # prints the whole report
    python -m bookstore.analytics --sql "SELECT * FROM books LIMIT 5"
    python -m bookstore.analytics --out shop.db   # keep the file to poke at
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

try:
    from .storage import Storage
except ImportError:                                  # pragma: no cover
    from bookstore.storage import Storage            # type: ignore


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
#
# Columns are chosen for querying, not for faithfulness to the JSON. Nothing
# secret is projected: no salt, no password hash. A reporting database that
# holds credentials is a reporting database that leaks them.

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE books (
    book_id        TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    author         TEXT NOT NULL,
    category       TEXT NOT NULL,
    year           INTEGER,
    price          REAL    NOT NULL,
    stock          INTEGER NOT NULL,
    browse_count   INTEGER NOT NULL DEFAULT 0,
    purchase_count INTEGER NOT NULL DEFAULT 0,
    added_at       TEXT
);

CREATE TABLE users (
    user_id        TEXT PRIMARY KEY,
    username       TEXT NOT NULL UNIQUE,
    display_name   TEXT,
    role           TEXT NOT NULL,
    balance        REAL NOT NULL DEFAULT 0,
    warnings       INTEGER NOT NULL DEFAULT 0,
    suspended      INTEGER NOT NULL DEFAULT 0,
    purchase_count INTEGER NOT NULL DEFAULT 0,
    total_spent    REAL NOT NULL DEFAULT 0,
    total_fines    REAL NOT NULL DEFAULT 0,
    face_enrolled  INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT
);

CREATE TABLE transactions (
    txn_id     TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    user_id    TEXT REFERENCES users(user_id),
    book_id    TEXT REFERENCES books(book_id),
    amount     REAL NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL,
    -- Denormalised once here so every report can group by month without
    -- repeating the same substr() in a dozen queries.
    month      TEXT
);

CREATE TABLE reviews (
    review_id         TEXT PRIMARY KEY,
    book_id           TEXT REFERENCES books(book_id),
    user_id           TEXT REFERENCES users(user_id),
    role_at_time      TEXT,
    stars             INTEGER NOT NULL,
    weight            REAL NOT NULL DEFAULT 1.0,
    verified_purchase INTEGER NOT NULL DEFAULT 0,
    taboo_hits        INTEGER NOT NULL DEFAULT 0,
    blocked           INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT
);

CREATE TABLE events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    user_id    TEXT,
    detail     TEXT,
    created_at TEXT
);

CREATE INDEX idx_txn_kind    ON transactions(kind);
CREATE INDEX idx_txn_month   ON transactions(month);
CREATE INDEX idx_txn_user    ON transactions(user_id);
CREATE INDEX idx_rev_book    ON reviews(book_id);
CREATE INDEX idx_books_cat   ON books(category);

-- The weighted average the specification asks for in item 7: a comment from
-- somebody who actually bought the book counts double. Expressing it as a
-- view means no report can accidentally use the unweighted mean instead.
CREATE VIEW book_ratings AS
SELECT
    b.book_id,
    b.title,
    b.author,
    b.category,
    COUNT(r.review_id)                                   AS rating_count,
    ROUND(AVG(r.stars), 3)                               AS plain_average,
    ROUND(SUM(r.stars * r.weight) / SUM(r.weight), 3)    AS weighted_average
FROM books b
JOIN reviews r ON r.book_id = b.book_id AND r.blocked = 0
GROUP BY b.book_id, b.title, b.author, b.category;
"""


def _month(timestamp: str) -> str:
    """'2026-10-11T23:59:14+00:00' -> '2026-10'. Empty string if unparseable."""
    return timestamp[:7] if timestamp and len(timestamp) >= 7 else ""


class Analytics:
    """A read-only SQL projection of the shop's text files."""

    def __init__(self, storage: Optional[Storage] = None,
                 path: str = ":memory:"):
        self.storage = storage or Storage()
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.build()

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "Analytics":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # -- the one writer ----------------------------------------------------

    def build(self) -> "Analytics":
        """(Re)create the projection from the text files. Idempotent."""
        self.storage.load()
        cur = self.conn.cursor()

        for table in ("book_ratings",):
            cur.execute("DROP VIEW IF EXISTS %s" % table)
        for table in ("events", "reviews", "transactions", "users", "books"):
            cur.execute("DROP TABLE IF EXISTS %s" % table)
        cur.executescript(SCHEMA)

        cur.executemany(
            "INSERT INTO books VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(b.get("book_id"), b.get("title", ""), b.get("author", ""),
              b.get("category", ""), b.get("year"), float(b.get("price", 0)),
              int(b.get("stock", 0)), int(b.get("browse_count", 0)),
              int(b.get("purchase_count", 0)), b.get("added_at", ""))
             for b in self.storage.books.values()])

        cur.executemany(
            "INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(u.get("user_id"), u.get("username", ""),
              u.get("display_name", ""), u.get("role", ""),
              float(u.get("balance", 0)), int(u.get("warnings", 0)),
              int(bool(u.get("suspended"))), int(u.get("purchase_count", 0)),
              float(u.get("total_spent", 0)), float(u.get("total_fines", 0)),
              int(bool(u.get("face_enrolled"))), u.get("created_at", ""))
             for u in self.storage.users.values()])

        known_books = {b.get("book_id") for b in self.storage.books.values()}
        known_users = {u.get("user_id") for u in self.storage.users.values()}

        cur.executemany(
            "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)",
            [(t.get("txn_id"), t.get("kind", ""),
              t.get("user_id") if t.get("user_id") in known_users else None,
              t.get("book_id") if t.get("book_id") in known_books else None,
              float(t.get("amount", 0)), t.get("note", ""),
              t.get("created_at", ""), _month(t.get("created_at", "")))
             for t in self.storage.transactions])

        cur.executemany(
            "INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(r.get("review_id"),
              r.get("book_id") if r.get("book_id") in known_books else None,
              r.get("user_id") if r.get("user_id") in known_users else None,
              r.get("role_at_time", ""), int(r.get("stars", 0)),
              float(r.get("weight", 1.0)),
              int(bool(r.get("verified_purchase"))),
              int(r.get("taboo_hits", 0)), int(bool(r.get("blocked"))),
              r.get("created_at", ""))
             for r in self.storage.reviews])

        cur.executemany(
            "INSERT INTO events (kind, user_id, detail, created_at) "
            "VALUES (?,?,?,?)",
            [(e.get("kind", ""), e.get("user_id"), e.get("detail", ""),
              e.get("created_at", "")) for e in self.storage.events])

        self.conn.commit()
        return self

    # -- querying ----------------------------------------------------------

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        """Run a SELECT and return plain dictionaries.

        Anything that is not a SELECT or a WITH is refused. The projection is
        disposable, so a stray UPDATE would not corrupt the shop -- but it
        would quietly make a report disagree with the text files, which is
        worse than an error.
        """
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if head not in ("SELECT", "WITH"):
            raise ValueError(
                "This is a read-only projection; only SELECT and WITH are "
                "allowed here. The shop's records are the text files in "
                "data/, and they are written through rules.py.")
        return [dict(row) for row in self.conn.execute(sql, tuple(params))]

    # -- the reports -------------------------------------------------------

    def revenue_by_month(self) -> List[Dict[str, Any]]:
        """Money in per month, split by why, with a running total.

        A window function over a CTE: the kind of question that is one query
        here and three passes over a list in Python.
        """
        return self.query("""
            WITH monthly AS (
                SELECT month,
                       SUM(CASE WHEN kind = 'purchase' THEN amount ELSE 0 END) AS sales,
                       SUM(CASE WHEN kind = 'fine'     THEN amount ELSE 0 END) AS fines,
                       SUM(CASE WHEN kind = 'refund'   THEN amount ELSE 0 END) AS refunds,
                       COUNT(*)                                                AS movements
                FROM transactions
                WHERE month <> ''
                GROUP BY month
            )
            SELECT month,
                   ROUND(sales, 2)                   AS sales,
                   ROUND(fines, 2)                   AS fines,
                   ROUND(refunds, 2)                 AS refunds,
                   ROUND(sales + fines - refunds, 2) AS revenue,
                   movements,
                   ROUND(SUM(sales + fines - refunds)
                         OVER (ORDER BY month
                               ROWS BETWEEN UNBOUNDED PRECEDING
                                        AND CURRENT ROW), 2) AS running_total
            FROM monthly
            ORDER BY month
        """)

    def best_sellers(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Units and revenue per title, ranked within each category."""
        return self.query("""
            SELECT b.title,
                   b.author,
                   b.category,
                   COUNT(t.txn_id)        AS units,
                   ROUND(SUM(t.amount),2) AS revenue,
                   RANK() OVER (PARTITION BY b.category
                                ORDER BY COUNT(t.txn_id) DESC) AS rank_in_category
            FROM books b
            JOIN transactions t
              ON t.book_id = b.book_id AND t.kind = 'purchase'
            GROUP BY b.book_id, b.title, b.author, b.category
            ORDER BY units DESC, revenue DESC
            LIMIT ?
        """, (limit,))

    def category_performance(self) -> List[Dict[str, Any]]:
        """Each category's share of revenue, and how its stock is doing.

        A LEFT JOIN on purpose: a category that has sold nothing is a finding,
        and an INNER JOIN would hide exactly the rows worth looking at.
        """
        return self.query("""
            SELECT b.category,
                   COUNT(DISTINCT b.book_id)                  AS titles,
                   SUM(b.stock)                               AS copies_in_stock,
                   ROUND(SUM(b.stock * b.price), 2)           AS stock_value,
                   COUNT(t.txn_id)                            AS units_sold,
                   ROUND(COALESCE(SUM(t.amount), 0), 2)       AS revenue,
                   ROUND(100.0 * COALESCE(SUM(t.amount), 0)
                         / NULLIF((SELECT SUM(amount) FROM transactions
                                   WHERE kind = 'purchase'), 0), 1) AS pct_of_revenue
            FROM books b
            LEFT JOIN transactions t
                   ON t.book_id = b.book_id AND t.kind = 'purchase'
            GROUP BY b.category
            ORDER BY revenue DESC, titles DESC
        """)

    def order_lists(self) -> List[Dict[str, Any]]:
        """Specification item 7, as a query.

        More than three ratings and a weighted average below 2 puts a title on
        the no-order list; above 4 puts it on the to-order list. This is the
        same arithmetic ``rules.py`` applies; agreeing with it is what the
        test in ``test_analytics.py`` checks.
        """
        return self.query("""
            SELECT title, author, category, rating_count,
                   plain_average, weighted_average,
                   CASE WHEN rating_count > 3 AND weighted_average > 4
                            THEN 'to-order'
                        WHEN rating_count > 3 AND weighted_average < 2
                            THEN 'no-order'
                        ELSE 'normal'
                   END AS list_status
            FROM book_ratings
            ORDER BY weighted_average DESC, rating_count DESC
        """)

    def customer_value(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Who spends, and how far each customer is from the next tier."""
        return self.query("""
            SELECT u.username,
                   u.display_name,
                   u.role,
                   ROUND(u.balance, 2)     AS balance,
                   u.purchase_count,
                   ROUND(u.total_spent, 2) AS total_spent,
                   ROUND(u.total_fines, 2) AS total_fines,
                   u.warnings,
                   CASE WHEN u.role IN ('VC', 'SU') THEN 0
                        ELSE MAX(0, 4 - u.purchase_count)
                   END AS purchases_to_vip,
                   CASE WHEN u.role IN ('VC', 'SU') THEN 0
                        ELSE ROUND(MAX(0, 200.01 - u.total_spent), 2)
                   END AS spend_to_vip
            FROM users u
            WHERE u.role <> 'SU'
            ORDER BY u.total_spent DESC
            LIMIT ?
        """, (limit,))

    def moderation(self) -> List[Dict[str, Any]]:
        """Taboo-word activity per customer -- item 7's warning ladder."""
        return self.query("""
            SELECT u.username,
                   u.role,
                   u.warnings,
                   CASE u.suspended WHEN 1 THEN 'yes' ELSE 'no' END  AS suspended,
                   COUNT(r.review_id)                                AS comments,
                   SUM(r.blocked)                                    AS blocked,
                   SUM(r.taboo_hits)                                 AS taboo_words,
                   ROUND(100.0 * SUM(r.blocked)
                         / NULLIF(COUNT(r.review_id), 0), 1)         AS pct_blocked
            FROM users u
            JOIN reviews r ON r.user_id = u.user_id
            GROUP BY u.user_id, u.username, u.role, u.warnings, u.suspended
            HAVING SUM(r.taboo_hits) > 0 OR SUM(r.blocked) > 0
            ORDER BY taboo_words DESC
        """)

    def dead_stock(self) -> List[Dict[str, Any]]:
        """Browsed but never bought: the shelf's quiet failures."""
        return self.query("""
            SELECT title, author, category, price, stock, browse_count
            FROM books
            WHERE purchase_count = 0
            ORDER BY browse_count DESC, stock DESC
        """)

    def report(self) -> Dict[str, List[Dict[str, Any]]]:
        """Every report in one dictionary, for the CLI and for tests."""
        return {
            "revenue_by_month": self.revenue_by_month(),
            "category_performance": self.category_performance(),
            "best_sellers": self.best_sellers(),
            "order_lists": self.order_lists(),
            "customer_value": self.customer_value(),
            "moderation": self.moderation(),
            "dead_stock": self.dead_stock(),
        }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_table(title: str, rows: List[Dict[str, Any]]) -> None:
    print("\n" + title)
    print("-" * len(title))
    if not rows:
        print("(nothing to report)")
        return

    columns = list(rows[0])
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows))
              for c in columns}
    print("  ".join(c.ljust(widths[c]) for c in columns))
    print("  ".join("-" * widths[c] for c in columns))
    for row in rows:
        print("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns))


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=("Report on the shop with SQL, over a disposable SQLite "
                     "projection of the text files in data/."))
    parser.add_argument("--data-dir", default=None,
                        help="the shop's data directory (default: data/)")
    parser.add_argument("--out", default=":memory:",
                        help="write the projection to this file and keep it")
    parser.add_argument("--sql", default=None,
                        help="run one SELECT instead of the standard report")
    parser.add_argument("--json", action="store_true",
                        help="print JSON instead of tables")
    args = parser.parse_args(argv)

    storage = Storage(args.data_dir) if args.data_dir else Storage()
    with Analytics(storage, path=args.out) as sql:
        if args.sql:
            rows = sql.query(args.sql)
            print(json.dumps(rows, indent=2) if args.json
                  else _print_table("Query", rows) or "")
            return 0

        report = sql.report()
        if args.json:
            print(json.dumps(report, indent=2))
            return 0

        print("Inkwell & Co. -- shop report")
        print("Projected from %s; the text files remain the record."
              % os.path.abspath(storage.data_dir))
        titles = {
            "revenue_by_month": "Revenue by month (with running total)",
            "category_performance": "Category performance",
            "best_sellers": "Best sellers (ranked within category)",
            "order_lists": "Order lists (weighted average, spec item 7)",
            "customer_value": "Customers by lifetime spend",
            "moderation": "Moderation",
            "dead_stock": "Browsed but never bought",
        }
        for key, rows in report.items():
            _print_table(titles.get(key, key), rows)

        if args.out != ":memory:":
            print("\nProjection kept at %s -- delete it freely, `python -m "
                  "bookstore.analytics` rebuilds it." % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
