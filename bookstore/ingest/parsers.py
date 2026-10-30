"""Turn a markdown list of free books into plain records.

The free-programming-books lists are written by hundreds of people, so the
format is a convention rather than a grammar. A typical line:

    * [Think Python](https://greenteapress.com/thinkpython2/) - Allen B. Downey (PDF, HTML) (CC BY-NC)

The rules here were worked out from the real files, not from the
contributing guide, and each one has a test built from a real line. A line
that does not parse is skipped: one bad line costs only itself.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterator, List, Optional

_ENTRY = re.compile(r"^\s*[*-]\s+\[(?P<title>[^\]]+)\]\((?P<url>[^)\s]+)")
_HEADING = re.compile(r"^(?P<depth>#{3,4})\s+(?P<name>.+?)\s*$")
_PAREN = re.compile(r"\(([^()]*)\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_TAG = re.compile(r"<[^>]+>")

_KNOWN_MEDIA = {
    "pdf": "PDF", "html": "HTML", "epub": "EPUB", "mobi": "MOBI",
    "ipynb": "Notebook", "kindle": "Kindle", "postscript": "PostScript",
    "ps": "PostScript", "djvu": "DjVu", "markdown": "Markdown",
    "md": "Markdown", "txt": "Text", "text": "Text", "doc": "Doc",
    "rtf": "RTF", "xps": "XPS", "ibook": "iBook", "gitbook": "GitBook",
    "asciidoc": "AsciiDoc", "latex": "LaTeX",
}

# Sections that are not books. "Meta-Lists" links to other people's lists.
_SKIP_HEADINGS = {"index", "contents", "table of contents", "meta-lists"}


def clean_heading(raw: str) -> str:
    """A heading as a reader would say it.

    The upstream files put HTML anchors in some headings and escape "#" for
    markdown, so the raw text of the C# heading is
    ``<a id="csharp"></a>C\\#``. Kept as-is, that became a topic name on
    the page. A leading "0 - " ordering prefix goes too.
    """
    text = _TAG.sub("", raw)
    text = re.sub(r"\\(.)", r"\1", text)
    text = re.sub(r"^\d+\s*-\s*", "", text)
    return text.strip()


@dataclass
class ParsedResource:
    external_id: str
    title: str
    url: str
    authors: str = ""
    description: str = ""
    media: str = ""
    licence: str = ""
    language: str = "en"
    kind: str = "book"
    in_progress: bool = False
    topics: List[str] = field(default_factory=list)
    work_key: str = ""

    def content_hash(self) -> str:
        """A fingerprint of everything upstream controls.

        If it matches what is stored, the row is unchanged and the import
        does not touch it. The separator is a character no field contains.
        """
        parts = [self.title, self.url, self.authors, self.description,
                 self.media, self.licence, self.language, self.kind,
                 "1" if self.in_progress else "0",
                 ",".join(sorted(self.topics))]
        return hashlib.sha256("\u241f".join(parts).encode("utf-8")).hexdigest()


def _slug_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def normalise_url(url: str) -> str:
    """The form of a URL used for identity.

    Scheme and host are case-insensitive, so they are lowercased; the path
    is not, so it is left alone. A trailing slash is dropped because the
    lists are inconsistent about it.
    """
    url = url.strip()
    match = re.match(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<host>[^/]+)(?P<rest>.*)$", url)
    if not match:
        return url
    rest = match.group("rest").rstrip("/")
    return "%s://%s%s" % (match.group("scheme").lower(),
                          match.group("host").lower(), rest)


def _plain(text: str) -> str:
    """Lowercase, with accents removed: "Gödel" and "godel" compare equal."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def work_key(title: str, authors: str) -> str:
    """Group the same work listed more than once.

    Uses the title before any subtitle, without a leading article, plus the
    first author's name. "The Rust Book" by Steve Klabnik and "the rust
    book" by Steve Klabnik, Carol Nichols get the same key.
    """
    first_author = authors.split(",")[0] if authors else ""
    head = re.split(r":|\u2014| - ", _plain(title))[0].strip()
    head = re.sub(r"^(the|a|an)\s+", "", head)
    key = re.sub(r"[^a-z0-9]+", " ", head + " " + _plain(first_author))
    key = key.strip()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:32]


_NAME_PARTICLES = {"of", "the", "and", "de", "del", "della", "da", "di",
                   "van", "von", "der", "den", "bin", "ibn", "al", "le",
                   "la", "du", "dos", "das"}


def looks_like_names(text: str) -> bool:
    """Is the text after the dash a list of authors or a sentence?

    Length does not tell them apart -- the longest fields in the files are
    author lists. Capitalisation does: names are capitalised words, a
    sentence mostly is not.
    """
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'.\u2019-]*", text)
             if w.lower() not in _NAME_PARTICLES]
    if not words:
        return False
    capitalised = sum(1 for w in words if w[0].isupper())
    return capitalised / len(words) >= 0.6


def _split_notes(remainder: str) -> List[str]:
    return _PAREN.findall(_LINK.sub(r"\1", remainder))


def _classify_notes(notes: List[str]):
    media: List[str] = []
    licence = ""
    leftovers: List[str] = []
    for note in notes:
        if re.match(r"^(CC[ -]|GFDL|MIT|Apache|public domain)", note.strip(),
                    re.IGNORECASE):
            if not licence:
                licence = note.strip()
            continue
        for part in note.split(","):
            key = part.strip().lower()
            if key in _KNOWN_MEDIA:
                media.append(_KNOWN_MEDIA[key])
            else:
                cleaned = re.sub(r":[a-z_]+:", "", part).strip(" .,")
                if cleaned:
                    leftovers.append(cleaned)
    seen = []
    for item in media:
        if item not in seen:
            seen.append(item)
    return ", ".join(seen), licence, leftovers


def parse_entry(line: str, topics: List[str], language: str = "en",
                kind: str = "book") -> Optional[ParsedResource]:
    """One list line to a record, or None if it is not an entry."""
    match = _ENTRY.match(line)
    if not match:
        return None
    title = match.group("title").strip()
    url = match.group("url").strip()
    if not url or url.startswith("#") or not re.match(r"^https?://", url,
                                                     re.IGNORECASE):
        return None

    remainder = line[match.end():]
    # The url pattern stops at whitespace or ")", so skip the closing paren.
    if remainder.startswith(")"):
        remainder = remainder[1:]

    in_progress = ":construction:" in remainder or "in process" in remainder

    authors = ""
    dash_description = ""
    dash = re.search(r"\s[-\u2013\u2014]\s+(?P<rest>.+)$", remainder)
    if dash:
        text = dash.group("rest").split("(")[0]
        text = re.sub(r"\*+", "", text)
        text = _LINK.sub(r"\1", text).strip(" .,*")
        if text:
            if looks_like_names(text):
                authors = text
            else:
                dash_description = text

    media, licence, leftovers = _classify_notes(_split_notes(remainder))
    leftovers = [x for x in leftovers
                 if "construction" not in x.lower()
                 and "in process" not in x.lower()]
    if dash_description:
        leftovers.insert(0, dash_description)
    description = "; ".join(leftovers)

    normalised = normalise_url(url)
    return ParsedResource(
        external_id=_slug_hash(normalised), title=title, url=normalised,
        authors=authors[:500], description=description[:2000], media=media,
        licence=licence[:64], language=language, kind=kind,
        in_progress=in_progress, topics=list(topics),
        work_key=work_key(title, authors))


def parse_markdown_list(text: str, language: str = "en",
                        topic_prefix: str = "") -> Iterator[ParsedResource]:
    """Every entry in a list file, tagged with its section headings."""
    section = ""
    subsection = ""
    in_index = False
    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            name = clean_heading(heading.group("name"))
            if len(heading.group("depth")) == 3:
                section = name
                subsection = ""
                in_index = name.lower() in _SKIP_HEADINGS
            else:
                subsection = name
            continue
        if in_index or not section:
            continue
        topics = [t for t in (section, subsection) if t]
        if topic_prefix:
            topics.insert(0, topic_prefix)
        entry = parse_entry(line, topics, language=language)
        if entry is not None:
            yield entry
