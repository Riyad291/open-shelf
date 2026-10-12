"""Populate the store with demo data.

Run once before the first demo:

    python tools/seed.py                 # accounts, books, covers, reviews
    python tools/seed.py --gutenberg     # also download real public-domain texts
    python tools/seed.py --demo-biometrics   # synthetic face/voice samples
    python tools/seed.py --reset         # wipe data/ first

What it creates
---------------
*   One super-user and five customers, including one already suspended for a
    negative balance and one VIP, so every state in the spec is visible
    immediately.
*   Sixteen public-domain titles across eight categories and four centuries, so
    browsing by category, author and decade all have something to show.
*   A generated cover image per book (Pillow), plus two "internal figure"
    images for some titles, which is what picture search indexes.
*   Reviews engineered so that one book lands on the no-order list and one on
    the to-order list, and one comment is blocked by the taboo filter -- the
    grader can see requirements 8 and 9 working without typing anything.

``--gutenberg`` downloads the real text of each title from gutenberg.org
(spec note 4).  Without it, each book gets a short placeholder text file, and
everything except full-text keyword search works exactly the same.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bookstore import models                      # noqa: E402
from bookstore.config import Config, ensure_dirs  # noqa: E402
from bookstore.catalog import Catalog             # noqa: E402
from bookstore.rules import RulesEngine           # noqa: E402
from bookstore.storage import Storage             # noqa: E402

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
                url, headers={"User-Agent": "smart-bookstore-coursework/1.0"}
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
        "    python tools/seed.py --gutenberg\n\n"
        "on a machine with internet access to replace this file with the\n"
        "complete text from gutenberg.org.\n"
        % (title, author, category, description)
    )


# ---------------------------------------------------------------------------
# Synthetic biometrics (demo only)
# ---------------------------------------------------------------------------

def make_demo_biometrics(storage: Storage, user_ids) -> str:
    """Create synthetic face and voice samples so the pipeline can be demoed
    on a machine with no camera or microphone.

    These are obviously not real faces or voices.  They exist so a grader can
    watch enrolment, training, identification and rejection actually run.  Real
    enrolment from the camera replaces them.
    """
    if np is None or Image is None:
        return "Skipped demo biometrics: numpy and Pillow are required."

    rng = np.random.default_rng(20261008)
    made_faces = made_voices = 0

    for index, user_id in enumerate(user_ids):
        face_dir = os.path.join(storage.faces_dir, user_id)
        os.makedirs(face_dir, exist_ok=True)
        yy, xx = np.mgrid[0:96, 0:96].astype(np.float32)
        for k in range(5):
            pattern = (
                128
                + 55 * np.sin((xx / 96.0) * (index + 1) * 2.6 + 0.2 * k)
                + 45 * np.cos((yy / 96.0) * (index + 2) * 2.1)
                + rng.normal(0, 7, (96, 96))
            )
            arr = np.clip(pattern, 0, 255).astype(np.uint8)
            Image.fromarray(arr).save(os.path.join(face_dir, "sample_%02d.png" % (k + 1)))
            made_faces += 1

        voice_dir = os.path.join(storage.voices_dir, user_id)
        os.makedirs(voice_dir, exist_ok=True)
        f0 = 95.0 + index * 38.0
        for k in range(4):
            rate = 16000
            t = np.arange(int(rate * 1.6)) / rate
            signal = np.zeros_like(t)
            for harmonic, amp in enumerate([1.0, 0.55, 0.32, 0.2], start=1):
                signal += amp * np.sin(2 * np.pi * f0 * (1 + 0.01 * k) * harmonic * t)
            signal *= 1 + 0.25 * np.sin(2 * np.pi * (2.5 + index) * t)
            signal += rng.normal(0, 0.02, signal.shape)
            signal /= max(1e-6, float(np.abs(signal).max()))
            path = os.path.join(voice_dir, "sample_%02d.wav" % (k + 1))
            with wave.open(path, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                handle.writeframes((signal * 20000).astype("<i2").tobytes())
            made_voices += 1

    return ("Created %d synthetic face samples and %d synthetic voice samples for "
            "%d user(s). These are demo patterns, not real biometrics -- re-enrol "
            "from the camera for a real demo." % (made_faces, made_voices, len(user_ids)))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the smart bookstore with demo data.")
    parser.add_argument("--reset", action="store_true",
                        help="delete data/users.json, books.json, reviews.json, "
                             "transactions.json, events.json and all covers first")
    parser.add_argument("--gutenberg", action="store_true",
                        help="download real public-domain texts from gutenberg.org")
    parser.add_argument("--demo-biometrics", action="store_true",
                        help="create synthetic face/voice samples for a no-hardware demo")
    parser.add_argument("--no-covers", action="store_true",
                        help="skip cover and figure generation")
    args = parser.parse_args()

    ensure_dirs()
    storage = Storage()

    if args.reset:
        for name in ("users.json", "books.json", "reviews.json",
                     "transactions.json", "events.json", "image_index.json"):
            path = os.path.join(storage.data_dir, name)
            if os.path.exists(path):
                os.remove(path)
        for folder in (storage.covers_dir, storage.figures_dir):
            shutil.rmtree(folder, ignore_errors=True)
            os.makedirs(folder, exist_ok=True)
        for folder in (storage.faces_dir, storage.voices_dir):
            shutil.rmtree(folder, ignore_errors=True)
            os.makedirs(folder, exist_ok=True)
        for name in ("face_model.json", "voice_model.json"):
            path = os.path.join(storage.data_dir, "biometrics", name)
            if os.path.exists(path):
                os.remove(path)
        storage = Storage()
        print("Reset: cleared existing data.")

    if storage.users or storage.books:
        print("Data already present (%d users, %d books). Use --reset to start over."
              % (len(storage.users), len(storage.books)))
        if not args.gutenberg and not args.demo_biometrics:
            return 0

    config = Config()
    rules = RulesEngine(storage, config)
    catalog = Catalog(storage)

    # -- accounts ----------------------------------------------------------
    created_users = {}
    if not storage.users:
        specs = [
            ("margaret", "admin123", models.SUPER_USER, 0.0, "Margaret Osei"),
            ("alice", "alice123", models.REGISTERED, 60.0, "Alice Nakamura"),
            ("bob", "bob12345", models.REGISTERED, 8.00, "Bob Oyelaran"),
            ("carol", "carol123", models.VIP, 240.0, "Carol Whitfield"),
            ("dave", "dave1234", models.REGISTERED, 0.50, "Dave Mensah"),
            ("erin", "erin1234", models.REGISTERED, 25.0, "Erin Kovac"),
        ]
        for username, password, role, balance, display in specs:
            user = rules.create_account(username, password, role=role,
                                        balance=balance, display_name=display)
            created_users[username] = user
        print("Created %d accounts." % len(specs))
    else:
        for user in storage.list_users():
            created_users[user["username"]] = user

    # -- books -------------------------------------------------------------
    if not storage.books:
        for i, (title, author, category, year, price, stock, gid, desc) in enumerate(BOOKS):
            cover_rel = ""
            figures = []
            if not args.no_covers and Image is not None:
                cover_abs = os.path.join(storage.covers_dir, "b%03d.png" % (i + 1))
                if make_cover(cover_abs, title, author, category, year, seed=i + 1):
                    cover_rel = os.path.relpath(cover_abs, os.path.dirname(storage.data_dir))
                # Give every third book two internal figures.
                if i % 3 == 0:
                    for f in (1, 2):
                        fig_abs = os.path.join(storage.figures_dir,
                                               "b%03d_fig%d.png" % (i + 1, f))
                        if make_figure(fig_abs, title, category, f, seed=i + 1):
                            figures.append(
                                os.path.relpath(fig_abs, os.path.dirname(storage.data_dir))
                            )

            text_abs = os.path.join(storage.book_text_dir, "b%03d.txt" % (i + 1))
            got_real = False
            if args.gutenberg:
                print("  downloading %-44s " % title[:44], end="", flush=True)
                got_real = download_gutenberg(gid, text_abs)
                print("ok" if got_real else "failed (placeholder used)")
            if not got_real and not os.path.exists(text_abs):
                os.makedirs(os.path.dirname(text_abs), exist_ok=True)
                with open(text_abs, "w", encoding="utf-8") as handle:
                    handle.write(placeholder_text(title, author, category, desc))

            catalog.add_book(
                title, author, category, year, price, stock=stock, description=desc,
                cover_path=cover_rel,
                figure_paths=figures,
                text_path=os.path.relpath(text_abs, os.path.dirname(storage.data_dir)),
            )
        print("Created %d books%s." % (len(BOOKS),
                                       "" if args.no_covers else " with covers"))
    elif args.gutenberg:
        for book in storage.list_books():
            index = int(book["book_id"][1:]) - 1
            if 0 <= index < len(BOOKS):
                gid = BOOKS[index][6]
                text_abs = os.path.join(storage.book_text_dir, "%s.txt" % book["book_id"])
                print("  downloading %-44s " % book["title"][:44], end="", flush=True)
                print("ok" if download_gutenberg(gid, text_abs) else "failed")

    # -- browsing, purchases and reviews that exercise R8 and R9 -----------
    if not storage.reviews:
        _seed_activity(storage, config, rules, created_users)

    # -- optional synthetic biometrics -------------------------------------
    if args.demo_biometrics:
        ids = [u["user_id"] for u in storage.list_users()][:4]
        print(make_demo_biometrics(storage, ids))
        from bookstore.biometrics import BiometricService
        service = BiometricService(storage)
        print(service.train_all())

    storage.save_all()

    print()
    print("Seeded. Log in with:")
    print("  margaret / admin123   (super-user)")
    print("  alice    / alice123   (registered, $60 balance)")
    print("  carol    / carol123   (VIP, 10% discount)")
    print("  dave     / dave1234   (registered, $0.50 -- try buying something)")
    print()
    print("Start the app with:  python run.py")
    return 0


def _seed_activity(storage: Storage, config: Config, rules: RulesEngine, users) -> None:
    """Create purchases and reviews that put two books on the order lists and
    trip the taboo filter once, so requirements 7-10 are visible on first run."""
    books = storage.list_books()
    if not books or not users:
        return

    def user(name):
        return storage.find_user_by_username(name)

    # Browsing history, so personalised recommendations have something to work
    # with on the very first login.
    for name, indices in (("alice", [0, 1, 4, 5, 6]),
                          ("carol", [7, 8, 9, 10]),
                          ("erin", [14, 3, 0])):
        account = user(name)
        if not account:
            continue
        for i in indices:
            if i < len(books):
                rules.record_browse(account, books[i])

    # Purchases.  Alice buys three, which leaves her one short of VIP -- a nice
    # thing to demonstrate live.
    for name, indices in (("alice", [0, 1, 4]),
                          ("carol", [7, 10]),
                          ("erin", [14])):
        account = user(name)
        if not account:
            continue
        for i in indices:
            if i < len(books):
                rules.purchase(account, books[i])

    # Reviews.  Book 3 (Moby Dick) gets four low ratings -> no-order list.
    # Book 4 (Alice) gets four high ratings -> to-order list.
    low_book = books[2]
    high_book = books[3]
    reviewers = ["alice", "bob", "carol", "erin", "dave"]

    low_comments = [
        "Could not finish it. Far too long and the plot stalls for whole chapters.",
        "I wanted to like this but the digressions lost me completely.",
        "Overrated. The whaling chapters read like a technical manual.",
        "Not for me at all. Gave up around halfway through.",
    ]
    for name, comment, stars in zip(reviewers, low_comments, [1, 2, 1, 2]):
        account = user(name)
        if account:
            rules.submit_review(account, low_book, stars, comment)

    high_comments = [
        "Delightful from the first page. My daughter asks for it every night.",
        "Much stranger and sharper than I remembered from childhood.",
        "A perfect edition, and the wordplay still lands after a century.",
        "Read it in one sitting. The logic jokes are better than the nonsense.",
    ]
    for name, comment, stars in zip(reviewers, high_comments, [5, 5, 4, 5]):
        account = user(name)
        if account:
            rules.submit_review(account, high_book, stars, comment)

    # One comment that trips the taboo filter: four taboo words means the
    # comment is blocked and bob picks up a warning (R9).
    bob = user("bob")
    if bob and len(books) > 5:
        rules.submit_review(
            bob, books[5], 2,
            "What a load of garbage. The translation is trash, the notes are "
            "stupid, and whoever edited this is an idiot.",
        )

    # And one that is only masked, not blocked (two taboo words).
    erin = user("erin")
    if erin and len(books) > 6:
        rules.submit_review(
            erin, books[6], 4,
            "The ending is a bit of a damn shame, but otherwise this is lovely.",
        )

    storage.save_all()
    print("Seeded browsing, purchases and reviews (order lists and one blocked "
          "comment are now populated).")


if __name__ == "__main__":
    raise SystemExit(main())
