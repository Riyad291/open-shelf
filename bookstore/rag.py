"""Retrieval-augmented generation over the store's own text files
(spec requirement 12 and 13).

The corpus is every ``.txt``/``.md`` file in ``data/kb/`` -- store hours, the
team roster, return policy, shipping, the FAQ.  Files are split into
paragraph-sized chunks, indexed with TF-IDF, and queried by cosine similarity.
No embedding model is needed, which keeps the whole thing dependency-free and
instant; for a knowledge base of a few dozen pages TF-IDF is genuinely the
right tool.

The router in ``chat.py`` uses :meth:`Retriever.retrieve` and compares the best
score against ``rag_min_similarity``.  Above the threshold the answer is built
from local files and labelled as RAG; below it, the question goes to the LLM.
"""

from __future__ import annotations

import math
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .catalog import tokenize
from .storage import Storage

#: Chunks shorter than this are merged into the next one, so a bare heading
#: never becomes its own retrieval unit.
MIN_CHUNK_CHARS = 120
MAX_CHUNK_CHARS = 1200

#: A query term appearing in more than this fraction of chunks is too common to
#: count as evidence that the question is about the local files.
RARE_TERM_MAX_DOC_RATIO = 0.34

#: Extra weight for a query term that appears in a chunk's heading.  Headings
#: are short and highly descriptive, so "opening hours" should beat a passing
#: mention of Sunday in the staff roster.
HEADING_BONUS = 0.45


@dataclass
class Chunk:
    chunk_id: str
    source: str          # file basename, shown to the user as the citation
    source_path: str
    heading: str
    text: str
    tokens: Counter


@dataclass
class RagHit:
    chunk: Chunk
    score: float
    #: Query terms that are rare in the corpus and genuinely appear in this
    #: chunk.  A hit with none of these is a coincidence, not an answer.
    rare_matches: List[str] = field(default_factory=list)

    @property
    def citation(self) -> str:
        if self.chunk.heading:
            return "%s -> %s" % (self.chunk.source, self.chunk.heading)
        return self.chunk.source


def split_into_chunks(text: str) -> List[Tuple[str, str]]:
    """Split a document into ``(heading, body)`` pairs.

    Markdown-style headings (``# ...``) start a new chunk and are remembered as
    that chunk's heading; otherwise blank lines separate chunks.
    """
    lines = (text or "").replace("\r\n", "\n").split("\n")
    chunks: List[Tuple[str, str]] = []
    heading = ""
    buffer: List[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            chunks.append((heading, body))
        buffer.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            heading = stripped.lstrip("#").strip()
            continue
        # A short ALL-CAPS or "Title:" line also reads as a heading.
        if stripped and not buffer and len(stripped) < 60 and stripped.endswith(":"):
            flush()
            heading = stripped.rstrip(":").strip()
            continue
        if not stripped:
            if buffer:
                flush()
            continue
        buffer.append(line)
        if sum(len(b) for b in buffer) > MAX_CHUNK_CHARS:
            flush()
    flush()

    # Merge runaway-short chunks into their neighbour -- but only when they sit
    # under the *same* heading.  A short section with its own heading ("Opening
    # hours") is a meaningful retrieval unit, and folding it into the section
    # above would lose exactly the chunk a question is most likely to want.
    merged: List[Tuple[str, str]] = []
    for head, body in chunks:
        if (
            merged
            and head == merged[-1][0]
            and len(body) < MIN_CHUNK_CHARS
            and len(merged[-1][1]) < MAX_CHUNK_CHARS
        ):
            prev_head, prev_body = merged[-1]
            merged[-1] = (prev_head, prev_body + "\n" + body)
        else:
            merged.append((head, body))
    return merged


class Retriever:
    """TF-IDF retrieval over the knowledge-base files."""

    def __init__(self, storage: Storage):
        self.db = storage
        self.chunks: List[Chunk] = []
        self._doc_freq: Counter = Counter()
        self._stamps: Dict[str, str] = {}
        self.reload()

    # -- index --------------------------------------------------------------

    @staticmethod
    def _stamp(path: str) -> str:
        try:
            stat = os.stat(path)
            return "%d:%d" % (int(stat.st_mtime), stat.st_size)
        except OSError:
            return ""

    def _needs_reload(self) -> bool:
        files = self.db.kb_files()
        stamps = {p: self._stamp(p) for p in files}
        return stamps != self._stamps

    def reload(self, force: bool = False) -> int:
        """(Re)build the index.  Returns the number of chunks indexed."""
        if not force and not self._needs_reload():
            return len(self.chunks)

        files = self.db.kb_files()
        self.chunks = []
        self._doc_freq = Counter()
        self._stamps = {p: self._stamp(p) for p in files}

        for path in files:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                    text = handle.read()
            except OSError:
                continue
            source = os.path.basename(path)
            # The file name is part of what a chunk is about: a passage in
            # policies.txt is a policy even when its text never says so.
            name = source.rsplit(".", 1)[0].replace("_", " ")
            for i, (heading, body) in enumerate(split_into_chunks(text)):
                # Index the heading alongside the body so "opening hours"
                # matches a chunk headed "Opening hours".
                counts = Counter(tokenize(name + " " + heading + " " + heading
                                          + " " + body))
                if not counts:
                    continue
                chunk = Chunk(
                    chunk_id="%s#%d" % (source, i),
                    source=source,
                    source_path=path,
                    heading=heading,
                    text=body,
                    tokens=counts,
                )
                self.chunks.append(chunk)
                for term in counts:
                    self._doc_freq[term] += 1

        return len(self.chunks)

    @property
    def size(self) -> int:
        return len(self.chunks)

    @property
    def sources(self) -> List[str]:
        return sorted({c.source for c in self.chunks})

    # -- query --------------------------------------------------------------

    def _is_rare(self, term: str) -> bool:
        """Is this term selective enough to be evidence of a local question?

        The ratio alone is wrong on a small corpus.  With only two passages,
        a term appearing in one of them has a document ratio of 0.5 -- above
        the threshold -- so *nothing* would count as rare and no question would
        ever be answered from local files.  A term appearing in a single
        passage is highly selective whatever the corpus size, so the budget
        has an absolute floor of one document.
        """
        total = max(1, len(self.chunks))
        frequency = self._doc_freq.get(term, 0)
        if frequency == 0:
            return False                 # absent terms are not evidence
        budget = max(1, int(RARE_TERM_MAX_DOC_RATIO * total))
        return frequency <= budget

    def retrieve(self, question: str, top_k: int = 3) -> List[RagHit]:
        """Rank chunks against the question by TF-IDF cosine similarity.

        Each hit also records which *rare* query terms it genuinely contains.
        The router uses that to reject coincidental matches: a question about
        the capital of Mongolia shares only filler words with the knowledge
        base, so it has a nonzero cosine but no rare-term support, and belongs
        with the LLM rather than with RAG.
        """
        self.reload()
        query_terms = Counter(tokenize(question))
        if not query_terms or not self.chunks:
            return []

        total_docs = max(1, len(self.chunks))

        def idf(term: str) -> float:
            return math.log((total_docs + 1) / (1 + self._doc_freq.get(term, 0))) + 1.0

        query_vec: Dict[str, float] = {}
        for term, count in query_terms.items():
            query_vec[term] = (1.0 + math.log(count)) * idf(term)
        query_norm = math.sqrt(sum(v * v for v in query_vec.values())) or 1.0

        rare_query_terms = {t for t in query_terms if self._is_rare(t)}

        hits: List[RagHit] = []
        for chunk in self.chunks:
            dot = 0.0
            chunk_norm_sq = 0.0
            for term, count in chunk.tokens.items():
                weight = (1.0 + math.log(count)) * idf(term)
                chunk_norm_sq += weight * weight
                if term in query_vec:
                    dot += weight * query_vec[term]
            if dot <= 0:
                continue
            chunk_norm = math.sqrt(chunk_norm_sq) or 1.0
            score = dot / (query_norm * chunk_norm)

            # Heading match bonus.
            heading_terms = set(tokenize(chunk.heading))
            overlap = heading_terms & set(query_terms)
            if overlap:
                score += HEADING_BONUS * (len(overlap) / max(1, len(query_terms)))

            rare_matches = sorted(rare_query_terms & set(chunk.tokens))
            hits.append(
                RagHit(chunk=chunk, score=round(min(score, 1.0), 4),
                       rare_matches=rare_matches)
            )

        hits.sort(key=lambda h: (-len(h.rare_matches), -h.score))
        return hits[:top_k]

    def best_score(self, question: str) -> float:
        hits = self.retrieve(question, top_k=1)
        return hits[0].score if hits else 0.0

    def foreign_terms(self, question: str) -> List[str]:
        """Content words in the question that appear nowhere in the files.

        Matching words is evidence *for* a question being local; a word the
        files have never used is evidence *against*. "What are the opening
        hours of the Louvre?" matches "opening hours" strongly, and before
        this check it was answered from the shop's own hours, with a
        citation. "louvre" appears in no file, and that is the tell.

        Words of three letters or fewer are ignored: they are mostly function
        words the tokenizer kept, and too short to carry the signal.
        """
        return sorted({term for term in set(tokenize(question))
                       if len(term) > 3 and self._doc_freq.get(term, 0) == 0})

    def is_local_question(self, question: str, min_similarity: float,
                          top_k: int = 3) -> Tuple[bool, List[RagHit]]:
        """R13's first step: "check if the question is related to local files".

        A question counts as local when the best chunk clears the similarity
        threshold, shares at least one rare term with the corpus, **and**
        uses no content word the corpus has never seen (``foreign_terms``).

        Measured with tools/evaluate_ai.py on 18 covered and 15 uncovered
        questions: see the README for the before-and-after figures. The
        veto is a heuristic, and the adversarial set is small and was
        written by the same person who wrote the veto -- a reason to grow
        the set, not to trust the rate.
        """
        hits = self.retrieve(question, top_k=top_k)
        if not hits:
            return False, []
        best = hits[0]
        local = (best.score >= min_similarity
                 and bool(best.rare_matches)
                 and not self.foreign_terms(question))
        return local, hits

    def context_for(self, question: str, top_k: int = 3,
                    max_chars: int = 2400,
                    hits: Optional[List[RagHit]] = None) -> Tuple[str, List[RagHit]]:
        """Build an LLM-ready context block plus the hits it came from."""
        if hits is None:
            hits = self.retrieve(question, top_k=top_k)
        blocks: List[str] = []
        used = 0
        kept: List[RagHit] = []
        for hit in hits:
            block = "[%s]\n%s" % (hit.citation, hit.chunk.text)
            if used + len(block) > max_chars and blocks:
                break
            blocks.append(block)
            kept.append(hit)
            used += len(block)
        return "\n\n".join(blocks), kept
