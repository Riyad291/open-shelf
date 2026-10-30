"""An adapter so picture search and Shelf Scan can read the SQL catalogue.

``imagesearch.ImageIndex`` and ``shelfscan.ShelfScanner`` are the most
distinctive code in this project, and neither of them cares where a book
record comes from -- they want a title, a cover path and some figure paths.
They were written against the file-backed ``Storage`` and used exactly seven
things from it:

    data_dir  covers_dir  figures_dir  list_books()  get_book()

Rather than rewrite two carefully tuned modules for a storage change they are
indifferent to, this presents the relational catalogue through the same five
names. The image code is untouched by the migration.

This is the adapter pattern, and the reason to reach for it here is that the
alternative -- threading a Session through a descriptor-matching routine --
would put database concerns inside an image-processing module that has no
business knowing about them.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import models as m
from ..storage import Storage


def book_as_dict(book: m.Book) -> dict:
    """The shape the image modules expect.

    ``book_id`` is the integer primary key rendered as a string, because the
    index stores it as a JSON object key and JSON keys are strings. Callers
    turn it back with int().
    """
    return {
        "book_id": str(book.id),
        "title": book.title,
        "author": book.author,
        "category": book.category,
        "year": book.year,
        "description": book.description or "",
        "cover_path": book.cover_path or "",
        "figure_paths": [figure.path for figure in book.figures],
        "text_path": book.text_path or "",
    }


class ImageCatalogue:
    """Storage's read surface, backed by SQL."""

    def __init__(self, session: Session, files: Storage):
        self._session = session
        self._files = files

        # Paths still come from the filesystem layout: covers and figures are
        # files, and always were. Only the records moved.
        self.data_dir = files.data_dir
        self.covers_dir = files.covers_dir
        self.figures_dir = files.figures_dir

    def list_books(self) -> List[dict]:
        books = self._session.execute(
            select(m.Book).options(selectinload(m.Book.figures))
            .order_by(m.Book.id)
        ).scalars().all()
        return [book_as_dict(book) for book in books]

    def get_book(self, book_id) -> Optional[dict]:
        try:
            key = int(str(book_id).lstrip("b") or 0)
        except (TypeError, ValueError):
            return None
        book = self._session.get(m.Book, key)
        return book_as_dict(book) if book is not None else None
