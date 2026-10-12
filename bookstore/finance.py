"""Finance reporting.

Requirement 3 names this explicitly: "SU can ask a question about the finance
report of the system and receive the answer; the other 3 types of users won't
get any meaningful results".  The chat router calls into here only for a
super-user, and the admin panel shows the same figures.

Every number is derived from the ``transactions.json`` ledger, so the report
can never drift out of step with the accounts.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import models
from .storage import Storage


@dataclass
class FinanceReport:
    revenue_sales: float = 0.0
    revenue_fines: float = 0.0
    deposits_taken: float = 0.0
    refunds: float = 0.0
    purchase_count: int = 0
    fine_count: int = 0
    average_order: float = 0.0
    customer_liability: float = 0.0   # money customers still hold on account
    inventory_value: float = 0.0
    top_books: List[Tuple[str, int, float]] = field(default_factory=list)
    revenue_by_category: List[Tuple[str, float]] = field(default_factory=list)
    user_counts: Dict[str, int] = field(default_factory=dict)
    suspended_count: int = 0
    negative_balance_count: int = 0

    @property
    def total_revenue(self) -> float:
        return round(self.revenue_sales + self.revenue_fines - self.refunds, 2)


class FinanceService:
    def __init__(self, storage: Storage):
        self.db = storage

    def report(self) -> FinanceReport:
        rep = FinanceReport()

        per_book_units: Counter = Counter()
        per_book_revenue: Dict[str, float] = defaultdict(float)

        for txn in self.db.transactions:
            kind = txn.get("kind")
            amount = float(txn.get("amount", 0.0))
            if kind == "purchase":
                rep.revenue_sales += amount
                rep.purchase_count += 1
                book_id = txn.get("book_id") or ""
                if book_id:
                    per_book_units[book_id] += 1
                    per_book_revenue[book_id] += amount
            elif kind == "fine":
                rep.revenue_fines += amount
                rep.fine_count += 1
            elif kind == "deposit":
                # Stored as a negative store-revenue line; report it positive.
                rep.deposits_taken += abs(amount)
            elif kind == "refund":
                rep.refunds += abs(amount)

        rep.revenue_sales = round(rep.revenue_sales, 2)
        rep.revenue_fines = round(rep.revenue_fines, 2)
        rep.deposits_taken = round(rep.deposits_taken, 2)
        rep.refunds = round(rep.refunds, 2)
        rep.average_order = round(
            rep.revenue_sales / rep.purchase_count, 2
        ) if rep.purchase_count else 0.0

        # Accounts.
        category_revenue: Dict[str, float] = defaultdict(float)
        for book_id, revenue in per_book_revenue.items():
            book = self.db.get_book(book_id)
            category = (book.get("category") if book else "") or "uncategorised"
            category_revenue[category] += revenue

        rep.revenue_by_category = sorted(
            ((c, round(v, 2)) for c, v in category_revenue.items()),
            key=lambda item: -item[1],
        )

        top: List[Tuple[str, int, float]] = []
        for book_id, units in per_book_units.most_common(10):
            book = self.db.get_book(book_id)
            title = book.get("title") if book else book_id
            top.append((title, units, round(per_book_revenue[book_id], 2)))
        rep.top_books = top

        liability = 0.0
        counts: Counter = Counter()
        for user in self.db.list_users():
            models.normalise_user(user)
            counts[user.get("role", "?")] += 1
            balance = float(user.get("balance", 0.0))
            if balance > 0:
                liability += balance
            if balance < 0:
                rep.negative_balance_count += 1
            if user.get("suspended"):
                rep.suspended_count += 1
        rep.customer_liability = round(liability, 2)
        rep.user_counts = dict(counts)

        rep.inventory_value = round(
            sum(
                float(b.get("price", 0.0)) * int(b.get("stock", 0))
                for b in self.db.list_books()
            ),
            2,
        )
        return rep

    # -- rendering ----------------------------------------------------------

    def report_text(self) -> str:
        """Plain-text finance report, used by the chat box and admin panel."""
        rep = self.report()
        lines = [
            "FINANCE REPORT",
            "=" * 46,
            "Sales revenue        $%10.2f  (%d orders)" % (rep.revenue_sales, rep.purchase_count),
            "Fine income          $%10.2f  (%d fines)" % (rep.revenue_fines, rep.fine_count),
            "Refunds              $%10.2f" % rep.refunds,
            "-" * 46,
            "TOTAL REVENUE        $%10.2f" % rep.total_revenue,
            "",
            "Average order value  $%10.2f" % rep.average_order,
            "Deposits taken       $%10.2f" % rep.deposits_taken,
            "Customer balances    $%10.2f  (store liability)" % rep.customer_liability,
            "Inventory at retail  $%10.2f" % rep.inventory_value,
            "",
        ]

        if rep.top_books:
            lines.append("Best sellers:")
            for title, units, revenue in rep.top_books[:5]:
                lines.append("  %-34s %3d sold  $%8.2f" % (title[:34], units, revenue))
            lines.append("")

        if rep.revenue_by_category:
            lines.append("Revenue by category:")
            for category, revenue in rep.revenue_by_category:
                lines.append("  %-20s $%8.2f" % (category, revenue))
            lines.append("")

        role_bits = ", ".join(
            "%s %d" % (models.role_name(role), count)
            for role, count in sorted(rep.user_counts.items())
        )
        lines.append("Accounts: %s" % (role_bits or "none"))
        lines.append(
            "Suspended: %d    Negative balances: %d"
            % (rep.suspended_count, rep.negative_balance_count)
        )
        return "\n".join(lines)

    def user_statement(self, user: dict) -> str:
        """Per-customer statement, available to that customer in the chat box."""
        models.normalise_user(user)
        txns = self.db.transactions_for_user(user["user_id"])
        lines = [
            "Account statement for %s (%s)"
            % (user.get("display_name") or user.get("username"),
               models.role_name(user.get("role", ""))),
            "Balance            $%8.2f" % float(user.get("balance", 0.0)),
            "Lifetime spend     $%8.2f" % float(user.get("total_spent", 0.0)),
            "Fines paid         $%8.2f" % float(user.get("total_fines", 0.0)),
            "Purchases          %8d" % int(user.get("purchase_count", 0)),
            "Warnings           %8d" % int(user.get("warnings", 0)),
        ]
        if txns:
            lines.append("")
            lines.append("Recent activity:")
            for txn in txns[-8:][::-1]:
                book = self.db.get_book(txn.get("book_id") or "")
                label = book.get("title") if book else (txn.get("note") or "")
                lines.append(
                    "  %-10s %-26s $%7.2f  %s"
                    % (txn.get("kind", ""), str(label)[:26],
                       abs(float(txn.get("amount", 0.0))),
                       str(txn.get("created_at", ""))[:10])
                )
        return "\n".join(lines)
