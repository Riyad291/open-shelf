"""Catalogue: browsing and keyword search (spec requirement 5).

Browsing axes required by the spec: category, author, time (publication year).
Keyword search covers title, author, category, description and the first slice
of the book's own text file, scored with a small TF-IDF-ish ranking so that a
rare word counts for more than a common one.

Image search lives in ``imagesearch.py`` because it needs optional
dependencies; this module stays pure-stdlib.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from . import models
from .storage import Storage

_TOKEN = re.compile(r"[a-z0-9']+")

#: Words too common to be worth indexing.  Interrogatives and auxiliaries are
#: included deliberately: without them, "What is the capital of Mongolia?"
#: matches every knowledge-base chunk containing the word "what" and scores
#: just high enough to be mistaken for a local-files question.
STOPWORDS = {
    "a", "about", "am", "an", "and", "any", "anything", "are", "as", "at",
    "be", "been", "but", "by", "can", "could", "did", "do", "does", "for",
    "from", "get", "got", "had", "has", "have", "he", "her", "here", "his",
    "how", "i", "if", "in", "into", "is", "it", "its", "just", "many", "me",
    "much", "my", "of", "on", "or", "our", "out", "please", "s", "she",
    "should", "so", "some", "such", "tell", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "those", "to", "up",
    "want", "was", "we", "were", "what", "when", "where", "which", "who",
    "why", "will", "with", "would", "you", "your",
}

#: Suffixes stripped by :func:`stem`, longest first.  This is deliberately a
#: light stemmer rather than a full Porter implementation: it collapses the
#: inflections that actually matter for this corpus ("opening" -> "open",
#: "ratings" -> "rate") without needing NLTK.
_SUFFIXES = ("ingly", "edly", "ing", "ies", "ied", "ers", "est", "ed", "ly",
             "es", "s", "er")


def stem(token: str) -> str:
    """Collapse common inflections so a query term matches a document term.

    Applied identically when indexing and when querying.  The only property
    that really matters is *consistency*: ``stem(a) == stem(b)`` for any two
    inflections of the same word.  A single pass is not enough for that --
    "ratings" would strip to "rating" while "rating" strips to "rat" -- so the
    stripping repeats until the token stops changing.
    """
    if len(token) <= 3:
        return token
    current = token
    for _ in range(3):                       # a handful of passes is plenty
        stripped = _strip_one_suffix(current)
        if stripped == current:
            break
        current = stripped
    return current


_VOWELS = "aeiou"


def _tidy_verb_stem(root: str) -> str:
    """Repair the stem left behind by removing "-ed" or "-ing".

    This is Porter's step 1b cleanup, and without it the stemmer is
    inconsistent in exactly the way that breaks search: "rated" and "rating"
    strip to "rat" while "rate" stays "rate", so a query never matches.
    """
    # "rat" -> "rate", "troubl" -> "trouble", "s" + "iz" -> "ize".
    if root.endswith(("at", "bl", "iz")):
        return root + "e"
    # "shipp" -> "ship", but keep "ll", "ss", "zz".
    if (len(root) > 2 and root[-1] == root[-2]
            and root[-1] not in _VOWELS and root[-1] not in "lsz"):
        return root[:-1]
    # A short consonant-vowel-consonant stem lost a silent e: "hop" -> "hope".
    # Restricted to very short stems so "open" is not mangled into "opene".
    if (len(root) == 3 and root[0] not in _VOWELS
            and root[1] in _VOWELS and root[2] not in _VOWELS
            and root[2] not in "wxy"):
        return root + "e"
    return root


def _strip_one_suffix(token: str) -> str:
    if len(token) <= 3:
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            root = token[: -len(suffix)]
            # "ies" -> "y" reads better than a bare root ("policies" -> "policy").
            if suffix in ("ies", "ied"):
                return root + "y"
            if suffix in ("ing", "ingly", "ed", "edly"):
                return _tidy_verb_stem(root)
            return root
    return token

#: How much of a book's text file feeds the keyword index.  Full Gutenberg
#: texts are megabytes; the opening is plenty to make search useful and keeps
#: index building instant.
TEXT_SAMPLE_CHARS = 20_000


def tokenize(text: str, keep_stopwords: bool = False, apply_stem: bool = True) -> List[str]:
    """Lowercase, drop stopwords, and lightly stem.

    Every index in the project (book search and RAG alike) goes through this
    one function, so the query and the documents are always tokenised the same
    way.
    """
    tokens = _TOKEN.findall((text or "").lower())
    if keep_stopwords:
        return [stem(t) if apply_stem else t for t in tokens]
    kept = [t for t in tokens if t not in STOPWORDS and len(t) > 1]
    return [stem(t) for t in kept] if apply_stem else kept


@dataclass
class SearchHit:
    book: dict
    score: float
    matched: List[str]
    where: str = ""


class Catalog:
    """Browse and search over the book collection."""

    def __init__(self, storage: Storage):
        self.db = storage
        self._index: Dict[str, Counter] = {}
        self._doc_freq: Counter = Counter()
        self._indexed_ids: set = set()

    # -- browsing -----------------------------------------------------------

    def all_books(self) -> List[dict]:
        return [models.normalise_book(b) for b in self.db.list_books()]

    def categories(self) -> List[Tuple[str, int]]:
        counts = Counter(
            (b.get("category") or "uncategorised").lower() for b in self.all_books()
        )
        return sorted(counts.items())

    def authors(self) -> List[Tuple[str, int]]:
        counts = Counter(b.get("author") or "Unknown" for b in self.all_books())
        return sorted(counts.items())

    def decades(self) -> List[Tuple[str, int]]:
        """'Time' axis: group by decade, which reads better than raw years."""
        counts: Counter = Counter()
        for book in self.all_books():
            year = int(book.get("year") or 0)
            counts["Unknown" if year <= 0 else "%ds" % (year // 10 * 10)] += 1

        def sort_key(item: Tuple[str, int]):
            label = item[0]
            return (1, 0) if label == "Unknown" else (0, int(label[:-1]))

        return sorted(counts.items(), key=sort_key)

    def by_category(self, category: str) -> List[dict]:
        target = (category or "").strip().lower()
        return [b for b in self.all_books() if (b.get("category") or "").lower() == target]

    def by_author(self, author: str) -> List[dict]:
        target = (author or "").strip().lower()
        return [b for b in self.all_books() if (b.get("author") or "").lower() == target]

    def by_decade(self, label: str) -> List[dict]:
        if label == "Unknown":
            return [b for b in self.all_books() if int(b.get("year") or 0) <= 0]
        try:
            start = int(str(label).rstrip("s"))
        except ValueError:
            return []
        return [
            b for b in self.all_books()
            if start <= int(b.get("year") or 0) < start + 10
        ]

    def by_year_range(self, start: int, end: int) -> List[dict]:
        return [
            b for b in self.all_books()
            if start <= int(b.get("year") or 0) <= end
        ]

    # -- keyword search -----------------------------------------------------

    def _book_text(self, book: dict) -> str:
        """Searchable text for one book: metadata plus a sample of its content."""
        parts = [
            book.get("title", ""),
            book.get("title", ""),      # title counts double
            book.get("author", ""),
            book.get("category", ""),
            book.get("description", ""),
        ]
        text_path = book.get("text_path") or ""
        if text_path:
            full = text_path if os.path.isabs(text_path) else os.path.join(
                os.path.dirname(self.db.data_dir), text_path
            )
            if not os.path.exists(full):
                full = os.path.join(self.db.book_text_dir, os.path.basename(text_path))
            if os.path.exists(full):
                try:
                    with open(full, "r", encoding="utf-8", errors="ignore") as handle:
                        parts.append(handle.read(TEXT_SAMPLE_CHARS))
                except OSError:
                    pass
        return " ".join(parts)

    def build_index(self, force: bool = False) -> None:
        """Build the inverted index lazily, and only for new books."""
        books = self.all_books()
        current = {b["book_id"] for b in books}
        if force:
            self._index.clear()
            self._doc_freq.clear()
            self._indexed_ids.clear()

        # Drop books that no longer exist.
        for stale in self._indexed_ids - current:
            for term in self._index.pop(stale, {}):
                self._doc_freq[term] -= 1
                if self._doc_freq[term] <= 0:
                    del self._doc_freq[term]
        self._indexed_ids &= current

        for book in books:
            book_id = book["book_id"]
            if book_id in self._indexed_ids:
                continue
            counts = Counter(tokenize(self._book_text(book)))
            self._index[book_id] = counts
            for term in counts:
                self._doc_freq[term] += 1
            self._indexed_ids.add(book_id)

    def search(self, query: str, limit: int = 20) -> List[SearchHit]:
        """Rank books against a keyword query using TF-IDF cosine-ish scoring."""
        terms = tokenize(query)
        if not terms:
            return []
        self.build_index()

        total_docs = max(1, len(self._index))
        hits: List[SearchHit] = []

        for book in self.all_books():
            counts = self._index.get(book["book_id"])
            if not counts:
                continue
            score = 0.0
            matched: List[str] = []
            for term in set(terms):
                tf = counts.get(term, 0)
                if not tf:
                    continue
                idf = math.log((total_docs + 1) / (1 + self._doc_freq.get(term, 0))) + 1.0
                score += (1.0 + math.log(tf)) * idf
                matched.append(term)

            # An exact phrase appearing in the title is a very strong signal.
            if query.strip().lower() in (book.get("title") or "").lower():
                score += 8.0
            if query.strip().lower() in (book.get("author") or "").lower():
                score += 5.0

            if score > 0:
                where = "title/author" if any(
                    t in (book.get("title", "") + " " + book.get("author", "")).lower()
                    for t in matched
                ) else "description/text"
                hits.append(SearchHit(book=book, score=round(score, 3),
                                      matched=sorted(matched), where=where))

        hits.sort(key=lambda h: (-h.score, h.book.get("title", "")))
        return hits[:limit]

    # -- SU book management (requirement 5) ---------------------------------

    def add_book(
        self,
        title: str,
        author: str,
        category: str,
        year: int,
        price: float,
        stock: int = 10,
        description: str = "",
        cover_path: str = "",
        figure_paths: Optional[Iterable[str]] = None,
        text_path: str = "",
    ) -> dict:
        if not (title or "").strip():
            raise ValueError("Title is required.")
        if not (author or "").strip():
            raise ValueError("Author is required.")
        try:
            price = float(price)
        except (TypeError, ValueError):
            raise ValueError("Price must be a number.")
        if price < 0:
            raise ValueError("Price cannot be negative.")
        try:
            stock = int(stock)
        except (TypeError, ValueError):
            raise ValueError("Stock must be a whole number.")
        if stock < 0:
            raise ValueError("Stock cannot be negative.")
        try:
            year = int(year or 0)
        except (TypeError, ValueError):
            raise ValueError("Year must be a whole number.")

        book = models.new_book(
            self.db.next_book_id(), title, author, category, year, price,
            stock=stock, description=description, cover_path=cover_path,
            figure_paths=list(figure_paths or []), text_path=text_path,
        )
        self.db.put_book(book)
        self._indexed_ids.discard(book["book_id"])  # force reindex on next search
        return book

    def update_book(self, book_id: str, **changes) -> dict:
        book = self.db.get_book(book_id)
        if not book:
            raise ValueError("No such book: %s" % book_id)
        allowed = {
            "title", "author", "category", "year", "price", "stock",
            "description", "cover_path", "figure_paths", "text_path",
        }
        for key, value in changes.items():
            if key in allowed:
                book[key] = value
        self.db.put_book(book)
        self._indexed_ids.discard(book_id)
        return book

    def delete_book(self, book_id: str) -> bool:
        if book_id not in self.db.books:
            return False
        del self.db.books[book_id]
        self.db.save_books()
        self._indexed_ids.discard(book_id)
        self._index.pop(book_id, None)
        return True
