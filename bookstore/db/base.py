"""Engine, session factory and the declarative base.

The application targets a SQLAlchemy ``Session``, never a dialect. That is
what lets the same code run on PostgreSQL (the default, and what the tests
and CI use) and on SQLite (so somebody can clone the repository and run it
without Docker).

The two dialects are not quite interchangeable, and the differences are
handled here rather than being allowed to leak into the domain code:

* SQLite does not enforce foreign keys unless asked, so a ``PRAGMA`` is set
  on every connection. Without it the constraints in ``models.py`` would be
  decoration on that backend.
* SQLite has no row-level locking. ``SELECT ... FOR UPDATE`` parses but does
  nothing, because its writer lock is already exclusive. ``lock_row()``
  below names that difference instead of hiding it.
"""

from __future__ import annotations

import os
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


DEFAULT_URL = "postgresql+psycopg://postgres@/bookstore_dev?host=/tmp&port=5433"


def database_url() -> str:
    """Where the shop's records live.

    ``DATABASE_URL`` wins. It is the one knob that moves this between
    PostgreSQL, MySQL and SQLite:

        postgresql+psycopg://user:pass@host/dbname
        mysql+pymysql://user:pass@host/dbname
        sqlite:///bookstore.db
    """
    return os.environ.get("DATABASE_URL") or DEFAULT_URL


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None, **kwargs) -> Engine:
    url = url or database_url()
    options: dict = {"future": True, "pool_pre_ping": True}

    if url.startswith("sqlite"):
        # A file-backed SQLite database shared between request threads needs
        # this; the default would reject the connection on a second thread.
        options["connect_args"] = {"check_same_thread": False}
        options.pop("pool_pre_ping", None)

    options.update(kwargs)
    engine = create_engine(url, **options)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _enforce_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


_engine: Engine | None = None
_SessionFactory: sessionmaker | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def session_factory() -> sessionmaker:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            bind=engine(), expire_on_commit=False, future=True,
        )
    return _SessionFactory


def configure(url: str) -> None:
    """Point the process at a different database. Used by tests and the CLI."""
    global _engine, _SessionFactory
    _engine = make_engine(url)
    _SessionFactory = sessionmaker(
        bind=_engine, expire_on_commit=False, future=True,
    )


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, committed or rolled back.

    A request either commits every change it made or none of them. That is
    the whole reason this project moved to a database, so it is enforced in
    one place rather than trusted to each handler.
    """
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def supports_row_locks(session: Session) -> bool:
    """Whether ``SELECT ... FOR UPDATE`` actually locks a row here.

    PostgreSQL and MySQL: yes. SQLite: no -- it serialises writers with a
    database-wide lock instead, which is correct but coarser. Code that cares
    about the difference asks, rather than assuming.

    ``get_bind()`` rather than ``.bind``: the latter is None on a session
    bound per-mapper or via a scoped factory, and this would then raise
    AttributeError inside checkout -- the single worst place for a surprise.
    """
    try:
        return session.get_bind().dialect.name not in ("sqlite",)
    except Exception:
        # Unknown binding: assume no row locks and let the database's own
        # concurrency control stand. Being wrong this way is slow, not unsafe.
        return False
