"""Taboo-word filtering (spec requirement 9).

Rules implemented
-----------------
*   Every occurrence of a taboo word is replaced by asterisks, one ``*`` per
    character, so the shape of the redaction matches the word length.
*   Matching is case-insensitive and whole-word, so "classic" is not censored
    because "ass" happens to be a substring.  Multi-word taboo phrases are
    supported too.
*   A comment containing **more than** ``max_taboo_before_block`` hits (default
    3) is blocked entirely and earns its author one warning.
*   Hits are counted as *occurrences*, not distinct words: saying the same
    taboo word four times is four hits.  (The spec says "more than 3 taboo
    words"; counting occurrences is the stricter and more defensible reading,
    and it is the one a human moderator would use.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Sequence


@dataclass
class ModerationResult:
    """Outcome of screening one comment."""

    original: str
    masked: str
    hits: int
    blocked: bool
    matched_words: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return self.hits == 0


def _pattern_for(word: str) -> re.Pattern:
    """Whole-word, case-insensitive pattern for one taboo entry.

    ``\\b`` is used where the edge character is alphanumeric; for an entry that
    starts or ends with punctuation a word boundary would never match, so we
    drop it on that side.
    """
    escaped = re.escape(word.strip())
    if not escaped:
        return re.compile(r"(?!x)x")  # never matches
    left = r"\b" if word.strip()[0].isalnum() else ""
    right = r"\b" if word.strip()[-1].isalnum() else ""
    return re.compile(left + escaped + right, re.IGNORECASE)


def screen_comment(
    comment: str,
    taboo_words: Sequence[str],
    max_taboo_before_block: int = 3,
) -> ModerationResult:
    """Mask taboo words and decide whether the comment is blocked."""
    text = comment or ""
    counts: Dict[str, int] = {}
    matched: List[str] = []
    masked = text

    # Longest first, so a phrase like "really stupid" is caught before the
    # single word "stupid" inside it and we do not double-count.
    ordered = sorted({w.strip().lower() for w in taboo_words if w and w.strip()},
                     key=len, reverse=True)

    for word in ordered:
        pattern = _pattern_for(word)
        found = pattern.findall(masked)
        if not found:
            continue
        counts[word] = len(found)
        matched.append(word)
        masked = pattern.sub(lambda m: "*" * len(m.group(0)), masked)

    hits = sum(counts.values())
    blocked = hits > int(max_taboo_before_block)
    return ModerationResult(
        original=text,
        masked=masked,
        hits=hits,
        blocked=blocked,
        matched_words=matched,
        counts=counts,
    )


def preview(comment: str, taboo_words: Sequence[str], max_taboo_before_block: int = 3) -> str:
    """Human-readable summary, used by the SU moderation panel."""
    result = screen_comment(comment, taboo_words, max_taboo_before_block)
    if result.blocked:
        return "BLOCKED - %d taboo word(s): %s" % (
            result.hits,
            ", ".join(result.matched_words),
        )
    if result.hits:
        return "Masked %d taboo word(s): %s" % (
            result.hits,
            ", ".join(result.matched_words),
        )
    return "Clean"
