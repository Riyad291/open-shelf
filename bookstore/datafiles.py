"""Where the files live.

Not everything the app stores belongs in a table. Cover images, book texts,
the knowledge-base documents the assistant reads, and the face photographs
are files, and the modules that use them need to agree on where they are.
That agreement is this class, and nothing more: it owns the layout under
``data/`` and creates it on first use.

It is deliberately small. An earlier version of this project had no
database, and this module was the whole persistence layer -- users, books,
reviews and a ledger, each a JSON document with its own atomic write. The
database replaced all of it. What is left is the part a database was never
going to do, which is tell you where a PNG is.

Nothing here is the system of record. If a value belongs in a row, it is in
a row; see ``db/models.py``.
"""

from __future__ import annotations

import os
from typing import List, Optional

from . import config as cfg


class DataFiles:
    """The ``data/`` directory layout, created on first use."""

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = data_dir or cfg.DATA_DIR
        for path in (self.data_dir, self.kb_dir, self.covers_dir,
                     self.figures_dir, self.book_text_dir, self.faces_dir):
            os.makedirs(path, exist_ok=True)

    @property
    def kb_dir(self) -> str:
        """The assistant's source documents."""
        return os.path.join(self.data_dir, "kb")

    @property
    def covers_dir(self) -> str:
        return os.path.join(self.data_dir, "covers")

    @property
    def figures_dir(self) -> str:
        """Illustrations inside a book, used by picture search."""
        return os.path.join(self.data_dir, "figures")

    @property
    def book_text_dir(self) -> str:
        """Full text, for the TF-IDF index over the demo shelf."""
        return os.path.join(self.data_dir, "books")

    @property
    def faces_dir(self) -> str:
        return os.path.join(self.data_dir, "biometrics", "faces")

    def kb_files(self) -> List[str]:
        """Every knowledge-base document, in a stable order.

        Sorted, so the retriever's chunk numbering does not depend on the
        order the filesystem happens to return. Hidden files are skipped:
        an editor's ``.swp`` is not a document.
        """
        if not os.path.isdir(self.kb_dir):
            return []
        names = sorted(
            name for name in os.listdir(self.kb_dir)
            if name.lower().endswith((".txt", ".md"))
            and not name.startswith(".")
        )
        return [os.path.join(self.kb_dir, name) for name in names]
