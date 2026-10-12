"""Second pass of the SRS build: find which page each heading lands on.

The contents page is generated rather than left as a Word field, because a
Word TOC field renders blank until somebody presses F9 -- which is not a
reasonable thing to require of a reader.  So the document is built twice: once
to establish the layout, then this script reads the rendered PDF to find each
heading's page, and the second build writes those numbers in.

The entry count is identical between passes, so the contents page occupies the
same space both times and the numbers it reports stay correct.

    node docs/build_srs.js            # pass 1
    soffice --convert-to pdf ...      # render
    python docs/number_toc.py         # extract
    node docs/build_srs.js            # pass 2, now numbered
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PDF = os.path.join(HERE, "Smart_Bookstore_SRS.pdf")
INDEX = os.path.join(HERE, "toc_index.json")
OUT = os.path.join(HERE, "toc_pages.json")


def normalise(text: str) -> str:
    """Collapse whitespace and drop punctuation that the PDF may render
    differently (en dashes, non-breaking spaces, doubled spaces)."""
    text = text.replace("—", "-").replace("–", "-")
    text = text.replace(" ", " ")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def page_texts(pdf_path: str) -> list:
    """Return the text of each page, one entry per page."""
    try:
        total = int(
            re.search(
                r"Pages:\s+(\d+)",
                subprocess.run(["pdfinfo", pdf_path], capture_output=True,
                               text=True, check=True).stdout,
            ).group(1)
        )
    except Exception as exc:
        raise SystemExit("could not read the PDF: %s" % exc)

    pages = []
    for n in range(1, total + 1):
        result = subprocess.run(
            ["pdftotext", "-f", str(n), "-l", str(n), "-layout", pdf_path, "-"],
            capture_output=True, text=True,
        )
        pages.append(normalise(result.stdout))
    return pages


def main() -> int:
    if not os.path.exists(PDF):
        print("No rendered PDF yet; run the converter first.", file=sys.stderr)
        return 1
    with open(INDEX, "r", encoding="utf-8") as handle:
        headings = json.load(handle)

    pages = page_texts(PDF)
    found, missing = {}, []

    for item in headings:
        text = item["text"]
        needle = normalise(text)
        if not needle:
            continue
        # Search from the front; a heading's first appearance is its own page
        # (an entry in the contents page is skipped because the contents comes
        # before the body and is matched separately below).
        hit = None
        for page_no, page in enumerate(pages, start=1):
            if needle in page:
                hit = page_no
                break
        if hit is None:
            missing.append(text)
            continue
        found[text] = hit

    # The contents page itself lists every heading, so a heading whose first
    # match is the contents page must be looked up again after it.
    contents_page = None
    for page_no, page in enumerate(pages, start=1):
        if "table of contents" in page:
            contents_page = page_no
            break

    if contents_page:
        for text, page_no in list(found.items()):
            if page_no != contents_page:
                continue
            needle = normalise(text)
            for later in range(contents_page + 1, len(pages) + 1):
                if needle in pages[later - 1]:
                    found[text] = later
                    break

    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(found, handle, indent=1, ensure_ascii=False)

    print("located %d of %d headings" % (len(found), len(headings)))
    if missing:
        print("not found: %s" % ", ".join(missing[:6]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
