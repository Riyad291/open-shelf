"""Turning text into terms: the tokenizer and stemmer.

One tokenizer, used by everything that ranks text -- keyword search over the
catalogue (``services/catalog.py``) and retrieval over the assistant's own
documents (``rag.py``). They share it on purpose: a question and a document
have to be cut into terms the same way, or they cannot match.

Stdlib only, and small enough to read. The stopword list and the stemmer are
both tuned to this corpus, and both have been measured -- see the retrieval
figures in ``tools/evaluate_ai.py``.
"""

from __future__ import annotations

import re
from typing import List


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
    # A final silent "e" is dropped last, so "store" and "stored" both end
    # as "stor". Without this the bare word kept its "e" and its inflections
    # did not: measured on the help files, "What data do you store?" could
    # never match a passage about what is "stored".
    if len(current) > 4 and current.endswith("e"):
        current = current[:-1]
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
