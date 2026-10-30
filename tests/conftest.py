"""Shared test fixtures.

Every test gets a database of its own, created and dropped around it. That is
slower than sharing one and rolling back, and it is worth it: a test that
leaves a row behind should not be able to make the next test pass.

The suite runs against whatever ``TEST_DATABASE_URL`` names, defaulting to an
in-memory SQLite database so ``pytest`` works on a fresh clone with nothing
installed but the requirements. CI points it at a real PostgreSQL, because
some of what this project does -- row locks above all -- only exists there.
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest
from sqlalchemy.orm import Session

os.environ.setdefault("JWT_SECRET", "test-secret-not-used-anywhere-real")


def database_url(tmp_dir: str) -> str:
    configured = os.environ.get("TEST_DATABASE_URL")
    if configured:
        return configured
    return "sqlite:///" + os.path.join(tmp_dir, "test.db")


@pytest.fixture()
def workspace():
    """A temporary data directory: covers, knowledge base, face samples."""
    path = tempfile.mkdtemp(prefix="bookstore-test-")
    os.makedirs(os.path.join(path, "data", "kb"), exist_ok=True)
    with open(os.path.join(path, "data", "kb", "store.txt"), "w",
              encoding="utf-8") as handle:
        handle.write("# Opening hours\n"
                     "Monday to Friday: 9:00 to 19:00.\n"
                     "Sunday: 12:00 to 17:00.\n\n"
                     "# Returns\nFourteen days, receipt or not.\n")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def app(workspace):
    """A whole application against a throwaway database."""
    from bookstore.api.app import create_app
    from bookstore.db.base import Base, engine

    data_dir = os.path.join(workspace, "data")
    application = create_app(database=database_url(workspace),
                            data_dir=data_dir, create_tables=True)
    yield application
    Base.metadata.drop_all(engine())


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def session(app) -> Session:
    from bookstore.db.base import session_factory
    db_session = session_factory()()
    try:
        yield db_session
        db_session.commit()
    finally:
        db_session.close()


@pytest.fixture()
def config(app):
    from bookstore.api import runtime
    cfg = runtime.config()
    cfg.set("taboo_words", ["idiot", "garbage", "trash", "stupid", "moron"])
    return cfg


@pytest.fixture()
def supports_row_locks(session):
    """True where SELECT ... FOR UPDATE really locks a row.

    SQLite serialises writers with a database-wide lock instead, which is
    correct but means the concurrency tests are not exercising what they are
    written to exercise. They skip rather than pass vacuously.
    """
    from bookstore.db.base import supports_row_locks as supported
    return supported(session)


# --------------------------------------------------------------- factories


@pytest.fixture()
def make_book(session):
    from bookstore.db import models as m

    def factory(title="A Book", author="Ann Author", category="literature",
                year=1900, **extra):
        book = m.Book(title=title, author=author, category=category,
                      year=year,
                      description=extra.pop("description", "A book."), **extra)
        session.add(book)
        session.flush()
        return book

    return factory


@pytest.fixture()
def make_user(session):
    from bookstore.db import models as m
    from bookstore.services import accounts

    counter = {"n": 0}

    def factory(username=None, password="pw12345", role=m.REGISTERED,
                **extra):
        counter["n"] += 1
        return accounts.create_account(
            session, username or ("person%d" % counter["n"]), password,
            role=role, **extra)

    return factory


@pytest.fixture()
def sign_in(client):
    """Return an Authorization header for a username and password."""

    def factory(username, password="pw12345"):
        response = client.post("/api/auth/login",
                               json={"username": username, "password": password})
        assert response.status_code == 200, response.text
        return {"Authorization": "Bearer " + response.json()["access_token"]}

    return factory
