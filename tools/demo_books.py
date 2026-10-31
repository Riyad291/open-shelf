"""Demo book data and the images that go with it.

This file used to be the v1 seeder, which wrote accounts, books and
reviews into JSON files. That half is gone; the database is seeded by
``tools/seed_db.py``. This is what was worth keeping: the sixteen
public-domain titles, their generated covers and figures, and the
Gutenberg download.
"""

from __future__ import annotations

import os
import random


try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None  # type: ignore

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore


# ---------------------------------------------------------------------------
# Catalogue.  Gutenberg ebook numbers let --gutenberg fetch the real text.
# ---------------------------------------------------------------------------

BOOKS = [
    # title, author, category, year, price, stock, gutenberg_id, description
    ("Pride and Prejudice", "Jane Austen", "literature", 1813, 9.99, 12, 1342,
     "Elizabeth Bennet, five sisters, and the slow demolition of first impressions."),
    ("Frankenstein", "Mary Shelley", "literature", 1818, 8.50, 9, 84,
     "A student assembles a living creature and abandons it. Often called the first science fiction novel."),
    ("Moby Dick", "Herman Melville", "literature", 1851, 13.75, 5, 2701,
     "Ahab hunts a white whale across three oceans. Also an encyclopaedia of whaling."),
    ("Alice's Adventures in Wonderland", "Lewis Carroll", "children", 1865, 7.25, 18, 11,
     "A girl falls down a rabbit hole into a logic puzzle with teeth."),
    ("The Adventures of Sherlock Holmes", "Arthur Conan Doyle", "literature", 1892, 10.50, 14, 1661,
     "Twelve cases, one consulting detective, and the invention of the modern mystery."),
    ("Dracula", "Bram Stoker", "literature", 1897, 11.00, 7, 345,
     "Told entirely in letters, diaries and telegrams. Still the template for the vampire."),
    ("The Time Machine", "H. G. Wells", "literature", 1895, 7.99, 11, 35,
     "A traveller goes forward to the year 802,701 and finds humanity split in two."),
    ("On the Origin of Species", "Charles Darwin", "science", 1859, 16.50, 6, 1228,
     "The argument for natural selection, laid out patiently and at length."),
    ("Relativity: The Special and General Theory", "Albert Einstein", "science", 1916, 14.25, 8, 5001,
     "Einstein's own popular account, written for readers without the mathematics."),
    ("The Elements of Euclid", "Euclid", "science", -300, 19.99, 4, 21076,
     "Thirteen books of geometry that set the standard for proof for two thousand years."),
    ("Meditations", "Marcus Aurelius", "philosophy", 180, 10.00, 13, 2680,
     "A Roman emperor's private notes to himself on duty, anger and death."),
    ("Beyond Good and Evil", "Friedrich Nietzsche", "philosophy", 1886, 12.50, 6, 4363,
     "An attack on inherited morality, written in aphorisms that argue with each other."),
    ("The Decline and Fall of the Roman Empire", "Edward Gibbon", "history", 1776, 24.00, 3, 25717,
     "The long unravelling of Rome, told with a famously dry wit."),
    ("The Souls of Black Folk", "W. E. B. Du Bois", "history", 1903, 11.75, 10, 408,
     "Fourteen essays on race in America that founded a discipline."),
    ("Leaves of Grass", "Walt Whitman", "poetry", 1855, 13.00, 9, 1322,
     "The 1855 edition: twelve untitled poems and a preface that reads like a manifesto."),
    ("The Autobiography of Benjamin Franklin", "Benjamin Franklin", "biography", 1791, 9.25, 12, 20203,
     "Printer, diplomat, inventor, and a self-made man explaining how he made himself."),
]

# ---------------------------------------------------------------------------
# Cover generation
# ---------------------------------------------------------------------------

PALETTE = {
    "literature": [(38, 54, 92), (84, 32, 48), (46, 68, 60)],
    "science": [(22, 68, 84), (30, 52, 78), (18, 74, 70)],
    "history": [(92, 62, 30), (74, 48, 34), (58, 44, 36)],
    "philosophy": [(52, 40, 76), (40, 36, 58), (66, 48, 84)],
    "poetry": [(104, 54, 68), (88, 48, 82), (72, 40, 56)],
    "children": [(206, 110, 44), (64, 124, 148), (182, 90, 110)],
    "technology": [(32, 60, 64), (28, 44, 56), (40, 72, 76)],
    "biography": [(70, 60, 44), (54, 56, 48), (84, 70, 52)],
}

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",   # macOS
    "C:\\Windows\\Fonts\\georgiab.ttf",                       # Windows
]


def _font(size: int):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap(draw, text, font, max_width):
    words = text.split()
    lines, current = [], ""
    for word in words:
        trial = (current + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def make_cover(path: str, title: str, author: str, category: str, year: int,
               seed: int) -> bool:
    """Draw a distinct cover so picture search has real images to match."""
    if Image is None:
        return False
    width, height = 400, 600
    rng = random.Random(seed)
    base = rng.choice(PALETTE.get(category, PALETTE["literature"]))

    img = Image.new("RGB", (width, height), base)
    draw = ImageDraw.Draw(img)

    # Vertical gradient, so the colour histogram varies down the image.
    for y in range(height):
        factor = 1.0 - 0.35 * (y / height)
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(max(0, min(255, int(c * factor))) for c in base),
        )

    # A per-category geometric motif gives the edge/structure descriptor
    # something distinctive to latch onto.
    accent = tuple(min(255, int(c * 1.9) + 40) for c in base)
    motif = seed % 4
    if motif == 0:
        for i in range(6):
            offset = 60 + i * 28
            draw.arc([40 - offset // 3, 150 + offset // 2,
                      width - 40 + offset // 3, 460 + offset // 2],
                     200, 340, fill=accent, width=3)
    elif motif == 1:
        for i in range(9):
            x = 36 + i * 42
            draw.line([(x, 170), (x, 430)], fill=accent,
                      width=2 + (i % 3))
    elif motif == 2:
        for i in range(5):
            pad = 40 + i * 26
            draw.rectangle([pad, 170 + i * 18, width - pad, 440 - i * 18],
                           outline=accent, width=2)
    else:
        cx, cy = width // 2, 300
        for i in range(7):
            r = 30 + i * 24
            draw.ellipse([cx - r, cy - r // 2, cx + r, cy + r // 2],
                         outline=accent, width=2)

    # Title block.
    draw.rectangle([0, 60, width, 150], fill=(250, 246, 238))
    title_font = _font(30)
    lines = _wrap(draw, title, title_font, width - 48)
    if len(lines) > 3:
        title_font = _font(22)
        lines = _wrap(draw, title, title_font, width - 48)[:3]
    y = 70 + max(0, (80 - len(lines) * (title_font.size + 4)) // 2)
    for line in lines:
        w = draw.textlength(line, font=title_font)
        draw.text(((width - w) / 2, y), line, font=title_font, fill=(28, 28, 34))
        y += title_font.size + 4

    # Author and year footer.
    author_font = _font(20)
    small_font = _font(15)
    draw.rectangle([0, height - 110, width, height], fill=(18, 18, 22))
    for i, line in enumerate(_wrap(draw, author.upper(), author_font, width - 48)[:2]):
        w = draw.textlength(line, font=author_font)
        draw.text(((width - w) / 2, height - 94 + i * 24), line,
                  font=author_font, fill=(242, 238, 228))
    label = category.upper() + ("  -  %d" % year if year > 0 else "  -  ANTIQUITY")
    w = draw.textlength(label, font=small_font)
    draw.text(((width - w) / 2, height - 34), label, font=small_font,
              fill=(168, 162, 150))

    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "PNG")
    return True


def make_figure(path: str, title: str, category: str, index: int, seed: int) -> bool:
    """A plate or diagram 'from inside' the book, for figure search."""
    if Image is None:
        return False
    width, height = 480, 360
    rng = random.Random(seed * 31 + index)
    img = Image.new("RGB", (width, height), (248, 244, 234))
    draw = ImageDraw.Draw(img)
    ink = (46, 42, 38)

    if category == "science":
        # A plotted curve with axes.
        draw.line([(50, height - 50), (width - 30, height - 50)], fill=ink, width=2)
        draw.line([(50, 30), (50, height - 50)], fill=ink, width=2)
        points = []
        for i in range(60):
            x = 50 + i * ((width - 90) / 60.0)
            phase = rng.uniform(0, 3.14)
            y = height - 60 - 110 * abs((i / 60.0) ** (1 + index) )
            points.append((x, y - 20 * ((i % 7) / 7.0)))
        draw.line(points, fill=(150, 40, 40), width=2)
        for i in range(0, 60, 10):
            draw.ellipse([points[i][0] - 3, points[i][1] - 3,
                          points[i][0] + 3, points[i][1] + 3], fill=ink)
    elif category in ("history", "biography"):
        # A map-ish plate with borders and hatching.
        draw.rectangle([30, 30, width - 30, height - 30], outline=ink, width=3)
        for i in range(14):
            y = 50 + i * 20
            draw.line([(40 + rng.randint(0, 40), y),
                       (width - 40 - rng.randint(0, 60), y)],
                      fill=(120, 112, 100), width=1)
        draw.polygon([(140, 120), (250, 90), (330, 170), (280, 260), (160, 230)],
                     outline=ink)
    else:
        # A decorative engraving-style frame.
        draw.rectangle([24, 24, width - 24, height - 24], outline=ink, width=2)
        draw.rectangle([36, 36, width - 36, height - 36], outline=(140, 130, 118), width=1)
        for i in range(9):
            r = 16 + i * 14
            draw.ellipse([width // 2 - r, height // 2 - r // 2,
                          width // 2 + r, height // 2 + r // 2],
                         outline=(100 + i * 10, 92, 84))

    caption = "Fig. %d -- %s" % (index, title[:40])
    font = _font(14)
    draw.text((34, height - 22), caption, font=font, fill=(90, 84, 76))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "PNG")
    return True


# ---------------------------------------------------------------------------
# Gutenberg text download (spec note 4)
# ---------------------------------------------------------------------------

def download_gutenberg(book_id: int, out_path: str, timeout: int = 30) -> bool:
    import urllib.error
    import urllib.request

    urls = [
        "https://www.gutenberg.org/cache/epub/%d/pg%d.txt" % (book_id, book_id),
        "https://www.gutenberg.org/files/%d/%d-0.txt" % (book_id, book_id),
        "https://www.gutenberg.org/ebooks/%d.txt.utf-8" % book_id,
    ]
    for url in urls:
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "smart-bookstore-the desktop version/1.0"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8", "ignore")
            if len(text) < 2000:
                continue
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as handle:
                handle.write(text)
            return True
        except Exception:
            continue
    return False


def placeholder_text(title: str, author: str, category: str, description: str) -> str:
    return (
        "%s\nby %s\n\nCategory: %s\n\n%s\n\n"
        "---\n"
        "This is a placeholder text file. The real public-domain text was not\n"
        "downloaded, so full-text keyword search for this title only covers the\n"
        "metadata and description above. Run:\n\n"
        "    python tools/seed_db.py --gutenberg\n\n"
        "on a machine with internet access to replace this file with the\n"
        "complete text from gutenberg.org.\n"
        % (title, author, category, description)
    )
