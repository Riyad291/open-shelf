"""What the recognition and assistant modules need from the rest of the app.

The assistant, the recommender and Shelf Scan were written against the v1
rules and finance engines, which read JSON files. The web app answers the
same questions from SQL. All three modules use only five methods between
them, so those five are written down here as protocols.

Protocols rather than a base class: the SQL facades in
``services/chat_adapter.py`` fit these by having the methods, without
inheriting from anything, and a test double can do the same.
"""

from __future__ import annotations

from typing import Any, Dict, Protocol


class RatingRules(Protocol):
    def rating_summary(self, book_id: Any) -> Any:
        """Count and averages for one book."""

    def list_status(self, book_id: Any) -> str:
        """``"to-order"``, ``"no-order"`` or ``"normal"``."""


class CatalogueRules(RatingRules, Protocol):
    def order_lists(self) -> Dict[str, Any]:
        """The books to order more of, and the ones to stop ordering."""


class FinanceReport(Protocol):
    """The two texts the assistant prints for the money questions.

    Supplied by the caller. ``ChatService`` used to build its own from the
    store it was given, which worked for the JSON store and failed silently
    for the SQL one: the answer said "something went wrong" while being
    reported as a success.
    """

    def report_text(self) -> str:
        """The administrator's report."""

    def user_statement(self, user: Any) -> str:
        """One account's own statement."""
