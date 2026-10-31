"""The production entrypoint: migrate, make sure there is data, then serve.

    python tools/start.py

Everything here is safe to run on every boot. That matters because a free
hosting tier stops the container when nobody visits and starts it again on
the next request, so this runs far more often than a deploy does.

Four steps, each of which checks before it acts:

1. **Migrate.** ``alembic upgrade head``. A no-op when the schema is current.
2. **Seed the demo shelf** if the database has no books -- the sixteen
   public-domain titles the recommendation and review features need.
3. **Regenerate the cover images** if they are missing. This is the one that
   is not obvious: the database lives on a managed host and persists, but
   the container's filesystem does not. After a restart the rows are still
   there and the PNGs are gone, so picture search and Shelf Scan would match
   against nothing. Covers are regenerated from the book rows, deterministic
   from the book id, so they come back identical.
4. **Import the catalogue** if it is short. Normally that means a first boot
   on an empty database. It also means the catalogue self-heals if someone
   signs in with the public demo administrator account and deletes rows.

Then uvicorn, on ``$PORT`` -- which the host assigns and the process must
not choose for itself.
"""

from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

#: Below this many resources, the catalogue is treated as missing.
MINIMUM_RESOURCES = 100


def log(message: str) -> None:
    print("[start] %s" % message, flush=True)


def migrate() -> None:
    log("applying migrations")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   check=True)


def counts():
    from sqlalchemy import func, select
    from bookstore.db import models as m
    from bookstore.db.base import session_factory

    session = session_factory()()
    try:
        return (
            session.scalar(select(func.count(m.Book.id))) or 0,
            session.scalar(select(func.count(m.Resource.id))) or 0,
        )
    finally:
        session.close()


def seed_demo_shelf() -> None:
    log("no books: seeding the demo shelf")
    import seed_db
    seed_db.main([])


def restore_missing_covers() -> None:
    """Redraw cover and figure images for books that have lost their files.

    The paths are already stored on the row; only the bytes are missing.
    ``make_cover`` is seeded from the book id, so a regenerated cover is the
    same image the picture-search index was built from.
    """
    from bookstore.config import PROJECT_ROOT
    from bookstore.db import models as m
    from bookstore.db.base import session_factory
    from demo_books import make_cover, make_figure
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    session = session_factory()()
    redrawn = 0
    try:
        books = session.scalars(
            select(m.Book).options(selectinload(m.Book.figures))).all()
        for book in books:
            if not book.cover_path:
                continue
            absolute = os.path.join(PROJECT_ROOT, book.cover_path)
            if os.path.exists(absolute):
                continue
            os.makedirs(os.path.dirname(absolute), exist_ok=True)
            if make_cover(absolute, book.title, book.author, book.category,
                          book.year or 1900, book.id):
                redrawn += 1
            for figure in book.figures:
                figure_path = os.path.join(PROJECT_ROOT, figure.path)
                os.makedirs(os.path.dirname(figure_path), exist_ok=True)
                if not os.path.exists(figure_path):
                    make_figure(figure_path, book.title, book.category,
                                figure.position + 1, book.id)
    finally:
        session.close()

    if redrawn:
        log("redrew %d cover image(s) the container had lost" % redrawn)


def import_catalogue() -> None:
    log("catalogue is short: importing free-programming-books")
    import import_resources
    code = import_resources.main(["free-programming-books"])
    if code != 0:
        # A failed import must not stop the site from starting. The demo
        # shelf, the assistant and every account feature still work; the
        # library page is empty until the next boot retries.
        log("import failed; starting anyway with whatever is stored")


def serve() -> None:
    port = os.environ.get("PORT", "8000")
    log("serving on 0.0.0.0:%s" % port)
    os.execvp(sys.executable, [
        sys.executable, "-m", "uvicorn", "bookstore.api.app:app",
        "--host", "0.0.0.0", "--port", port,
        # One worker: the free tier has one small instance, and the
        # in-process caches (retrieval index, image descriptors, the trained
        # face model) would otherwise be built once per worker.
        "--workers", "1",
        # Render and Fly both put a proxy in front, so the client's real
        # address arrives in X-Forwarded-For.
        "--proxy-headers", "--forwarded-allow-ips", "*",
    ])


def main() -> int:
    from bookstore.config import ensure_dirs
    ensure_dirs()

    migrate()

    books, resources = counts()
    log("found %d book(s) and %d resource(s)" % (books, resources))

    if books == 0:
        seed_demo_shelf()
    else:
        restore_missing_covers()

    if resources < MINIMUM_RESOURCES:
        import_catalogue()

    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
