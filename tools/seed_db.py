"""Populate the database with a demo library.

    python tools/seed_db.py                  # tables, books, covers, accounts
    python tools/seed_db.py --reset          # drop everything first
    python tools/seed_db.py --gutenberg      # also fetch the real book texts

Every state the specification describes is visible straight away: a
suspended account, a trusted contributor, a title on each list, a masked and
a blocked comment, and a member one review away from promotion.

This is the demo shelf of sixteen public-domain titles. The free technology
catalogue is imported separately, by tools/import_resources.py.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select                                    # noqa: E402

from bookstore.config import Config, ensure_dirs                 # noqa: E402
from bookstore.db import models as m                             # noqa: E402
from bookstore.db.base import Base, configure, engine, session_factory  # noqa: E402
from bookstore.services import accounts, reviews                  # noqa: E402
from bookstore.storage import Storage                            # noqa: E402

from demo_books import BOOKS, download_gutenberg, make_cover, make_figure  # noqa: E402
from demo_books import placeholder_text                                # noqa: E402


#: username, display name, password, role
ACCOUNTS = [
    ("margaret", "Margaret Osei",   "admin123", m.SUPER_USER),
    ("alice",    "Alice Nakamura",  "alice123", m.REGISTERED),
    ("bob",      "Bob Oyelaran",    "bob12345", m.REGISTERED),
    ("carol",    "Carol Whitfield", "carol123", m.VIP),
    ("dave",     "Dave Mensah",     "dave1234", m.REGISTERED),
    ("erin",     "Erin Kovac",      "erin1234", m.REGISTERED),
]


def seed_books(session, files: Storage, fetch_texts: bool) -> list:
    created = []
    # BOOKS still carries the old price and stock columns; they are unused.
    for title, author, category, year, _price, _stock, gid, description \
            in BOOKS:
        book = m.Book(
            title=title, author=author, category=category, year=year,
            description=description, gutenberg_id=gid,
        )
        session.add(book)
        session.flush()

        slug = "b%03d" % book.id
        cover = os.path.join(files.covers_dir, "%s.png" % slug)
        if make_cover(cover, title, author, category, year, book.id):
            book.cover_path = os.path.relpath(cover, os.path.dirname(
                files.data_dir))

        for index in (1, 2):
            path = os.path.join(files.figures_dir,
                                "%s_fig%d.png" % (slug, index))
            if make_figure(path, title, category, index, book.id):
                session.add(m.BookFigure(
                    book_id=book.id, position=index - 1,
                    path=os.path.relpath(path,
                                         os.path.dirname(files.data_dir))))

        text_path = os.path.join(files.book_text_dir, "%s.txt" % slug)
        fetched = False
        if fetch_texts and gid:
            fetched = download_gutenberg(gid, text_path)
        if not fetched:
            with open(text_path, "w", encoding="utf-8") as handle:
                handle.write(placeholder_text(title, author, category,
                                              description))
        book.text_path = os.path.relpath(text_path,
                                         os.path.dirname(files.data_dir))

        created.append(book)

    session.flush()
    return created


def seed_accounts(session) -> dict:
    out = {}
    for username, display, password, role in ACCOUNTS:
        out[username] = accounts.create_account(
            session, username, password, role=role, display_name=display)
    session.flush()
    return out


def seed_activity(session, config, books: list, users: dict) -> None:
    """Rate and misbehave, so every rule has something to show."""
    by_title = {book.title: book for book in books}

    # Dave has collected more warnings than allowed and is suspended:
    # specification item 6, visible on arrival.
    for index in range(config.warning_limit + 1):
        accounts.apply_warning(session, users["dave"],
                               "demo warning %d" % (index + 1), config)

    # Ratings engineered so one title lands on each list. Carol is a trusted
    # contributor, so hers count double.
    loved = by_title["Alice's Adventures in Wonderland"]
    hated = by_title["Moby Dick"]
    for name, stars in [("alice", 5), ("bob", 5), ("carol", 4), ("erin", 5)]:
        reviews.submit(session, users[name], loved, stars,
                       "A delight from start to finish.", config)
    for name, stars in [("alice", 1), ("bob", 2), ("carol", 1), ("erin", 2)]:
        reviews.submit(session, users[name], hated, stars,
                       "Could not finish it; the plot stalls for chapters.",
                       config)

    # One masked comment and one blocked one, so moderation has both states.
    reviews.submit(session, users["erin"], by_title["The Time Machine"], 4,
                   "A damn fine little book.", config)
    # Four taboo words, not three: blocking needs *more than*
    # max_taboo_before_block (3), and "idiotic" is not the listed word
    # "idiot" -- matching is whole-word. An earlier version of this line read
    # "idiotic" and scored 3, so the comment was merely masked and the demo
    # library opened with no blocked comment and no warning to see.
    reviews.submit(session, users["bob"], by_title["Dracula"], 2,
                   "What a stupid, garbage, idiot pile of trash.", config)

    session.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="drop every table first")
    parser.add_argument("--gutenberg", action="store_true",
                        help="download the real texts from gutenberg.org")
    parser.add_argument("--database", default=None,
                        help="override DATABASE_URL for this run")
    args = parser.parse_args()

    ensure_dirs()
    if args.database:
        configure(args.database)

    if args.reset:
        Base.metadata.drop_all(engine())
    Base.metadata.create_all(engine())

    files = Storage()
    config = Config()
    session = session_factory()()

    try:
        existing = session.execute(select(m.Book).limit(1)).scalar_one_or_none()
        if existing is not None and not args.reset:
            print("The library already has books. Use --reset to start over.")
            return 0

        print("Seeding books and covers...")
        books = seed_books(session, files, args.gutenberg)
        print("  %d titles" % len(books))

        print("Creating accounts...")
        users = seed_accounts(session)

        print("Rating and misbehaving...")
        seed_activity(session, config, books, users)

        for word in config.taboo_words:
            session.merge(m.TabooWord(word=word))

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    print("\nDone. Sign in with:")
    for username, display, password, role in ACCOUNTS:
        print("  %-9s / %-9s (%s)" % (username, password, m.role_name(role)))
    print("\nStart it with: python -m bookstore.api")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
