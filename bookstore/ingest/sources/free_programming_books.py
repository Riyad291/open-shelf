"""The free-programming-books lists, by the Free Ebook Foundation.

The project's data is published under CC BY 4.0, which permits reuse with
attribution, so the attribution text travels with the source row. We index
titles and links; the books themselves stay where their authors put them.
"""

from __future__ import annotations

import pathlib
import urllib.request
from typing import Dict, Iterator

from ..parsers import ParsedResource, parse_markdown_list

SLUG = "free-programming-books"

SOURCE_FIELDS = {
    "name": "Free Programming Books",
    "homepage": "https://github.com/EbookFoundation/free-programming-books",
    "licence": "CC-BY-4.0",
    "attribution": ("Catalogue data from the free-programming-books project "
                    "by the Free Ebook Foundation, used under CC BY 4.0."),
    "terms_url": ("https://github.com/EbookFoundation/"
                  "free-programming-books/blob/main/LICENSE"),
}

RAW_BASE = ("https://raw.githubusercontent.com/EbookFoundation/"
            "free-programming-books/main/")

# The English lists. The project has others, by language; each would need
# its language code passed through to the parser.
FILES = [
    ("books/free-programming-books-langs.md", "language"),
    ("books/free-programming-books-subjects.md", "subject"),
]

USER_AGENT = "open-shelf-importer/1.0 (+catalogue indexer; links out only)"


def read_local(repo_dir: str) -> Dict[str, str]:
    """Read the lists from a clone of the repository."""
    root = pathlib.Path(repo_dir)
    documents = {}
    for path, _ in FILES:
        file = root / path
        if not file.exists():
            raise FileNotFoundError(
                "%s not found. --repo should point at a clone of "
                "EbookFoundation/free-programming-books." % file)
        documents[path] = file.read_text(encoding="utf-8")
    return documents


def read_remote(timeout: float = 30.0) -> Dict[str, str]:
    """Fetch the lists from GitHub."""
    documents = {}
    for path, _ in FILES:
        request = urllib.request.Request(RAW_BASE + path,
                                         headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                documents[path] = reply.read().decode("utf-8")
        except OSError as exc:
            raise RuntimeError(
                "Could not fetch %s (%s). If this machine is offline, clone "
                "the repository and pass --repo." % (path, exc)) from exc
    return documents


def parse(documents: Dict[str, str]) -> Iterator[ParsedResource]:
    for text in documents.values():
        yield from parse_markdown_list(text, language="en")
