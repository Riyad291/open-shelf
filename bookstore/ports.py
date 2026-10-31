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


class BookCatalogue(Protocol):
    """A catalogue of books, as the image code reads it.

    ``imagesearch`` and ``shelfscan`` need a list of books, one book by id,
    and the three directories the images sit in. They do not need a
    database, and saying so here is what lets the same two modules run
    against files in a test and against SQL in the application.
    """

    data_dir: str
    covers_dir: str
    figures_dir: str

    def list_books(self) -> Any:
        """Every book, as plain dictionaries."""

    def get_book(self, book_id: Any) -> Any:
        """One book by id, or None."""


class Browsable(Protocol):
    """Browsing and keyword search over the catalogue."""

    def all_books(self) -> Any: ...
    def categories(self) -> Any: ...
    def search(self, query: str, limit: int = 20) -> Any: ...


class Recommends(Protocol):
    """Three picks, either for one reader or like a set of books."""

    def top_for(self, user: Any, count: int = 3) -> Any: ...
    def similar_to(self, book_ids: Any, count: int = 3,
                   exclude: Any = None) -> Any: ...


class SiteReport(Protocol):
    """The two texts the assistant prints for the account questions.

    Supplied by the caller. ``ChatService`` used to build its own from the
    store it was given, which worked for the file-backed store and failed
    silently for the SQL one: the answer said "something went wrong" while
    being reported as a success.
    """

    def report_text(self) -> str:
        """The administrator's catalogue report."""

    def user_statement(self, user: Any) -> str:
        """One account's own statement."""
