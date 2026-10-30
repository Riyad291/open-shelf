"""The import pipeline: idempotent, concurrent-safe, and polite to our data."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from bookstore.db import models as m
from bookstore.ingest.parsers import ParsedResource
from bookstore.ingest.pipeline import (
    get_or_create_source, reconcile, run_import,
)


def make_parsed(n, **overrides):
    fields = dict(external_id="ext-%d" % n, title="Book %d" % n,
                  url="https://example.org/book-%d" % n,
                  authors="Ann Author", topics=["Python"])
    fields.update(overrides)
    return ParsedResource(**fields)


@pytest.fixture()
def source(session):
    return get_or_create_source(session, "probe", name="Probe Source",
                                licence="CC-BY-4.0",
                                attribution="Probe attribution")


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


class TestIdempotency:
    def test_first_import_inserts(self, session, source):
        report = reconcile(session, source, [make_parsed(i) for i in range(5)])
        assert report.inserted == 5
        assert count(session, m.Resource) == 5

    def test_second_import_changes_nothing(self, session, source):
        rows = [make_parsed(i) for i in range(5)]
        reconcile(session, source, rows)
        report = reconcile(session, source, rows)
        assert (report.inserted, report.updated, report.unchanged) == (0, 0, 5)
        assert count(session, m.Resource) == 5

    def test_a_changed_entry_is_updated_in_place(self, session, source):
        reconcile(session, source, [make_parsed(1)])
        report = reconcile(session, source, [make_parsed(1, title="Renamed")])
        assert report.updated == 1
        assert count(session, m.Resource) == 1
        assert session.scalar(select(m.Resource.title)) == "Renamed"

    def test_a_duplicate_in_the_feed_is_skipped(self, session, source):
        report = reconcile(session, source, [make_parsed(1), make_parsed(1)])
        assert (report.inserted, report.skipped) == (1, 1)

    def test_an_entry_gone_upstream_is_kept(self, session, source):
        reconcile(session, source, [make_parsed(1), make_parsed(2)])
        reconcile(session, source, [make_parsed(1)])
        assert count(session, m.Resource) == 2


class TestOwnership:
    def test_an_import_does_not_reset_what_belongs_to_us(self, session,
                                                         source):
        reconcile(session, source, [make_parsed(1)])
        resource = session.scalar(select(m.Resource))
        resource.click_count = 7
        resource.dead = True
        resource.last_status = 404
        session.flush()

        reconcile(session, source, [make_parsed(1, title="Upstream edit")])
        session.expire_all()
        resource = session.scalar(select(m.Resource))
        assert resource.title == "Upstream edit"
        assert (resource.click_count, resource.dead,
                resource.last_status) == (7, True, 404)


class TestTopics:
    def test_topics_are_created_once_and_linked(self, session, source):
        reconcile(session, source, [make_parsed(1), make_parsed(2)])
        assert count(session, m.Topic) == 1
        assert count(session, m.ResourceTopic) == 2

    def test_reimporting_does_not_duplicate_links(self, session, source):
        rows = [make_parsed(1, topics=["Python", "Web"])]
        reconcile(session, source, rows)
        reconcile(session, source, [make_parsed(1, topics=["Python", "Web"],
                                                title="Changed")])
        assert count(session, m.ResourceTopic) == 2


class TestRunAccounting:
    def test_a_successful_run_is_recorded(self, session, source):
        run_import(session, source, [make_parsed(1)])
        run = session.scalar(select(m.ImportRun))
        assert run.status == "ok" and run.inserted == 1
        assert run.finished_at is not None

    def test_a_failed_run_is_recorded_and_raises(self, session, source):
        def broken():
            yield make_parsed(1)
            raise ValueError("upstream exploded")

        with pytest.raises(ValueError):
            run_import(session, source, broken())
        run = session.scalar(select(m.ImportRun))
        assert run.status == "failed"
        assert "upstream exploded" in run.detail

    def test_a_dry_run_writes_nothing(self, session, source):
        report = run_import(session, source, [make_parsed(1)], dry_run=True)
        assert report.inserted == 1
        assert count(session, m.Resource) == 0
        assert session.scalar(select(m.ImportRun.detail)) == "dry run"


class TestConcurrentImports:
    def test_overlapping_imports_neither_duplicate_nor_crash(
            self, session, source, supports_row_locks):
        """Six importers start on the same feed at the same instant.

        With check-then-insert this produced no duplicates -- the constraint
        saw to that -- but most importers died on IntegrityError. The
        upsert's job is the second half: everyone finishes.
        """
        if not supports_row_locks:
            pytest.skip("needs a database with concurrent writers")
        session.commit()
        rows = [make_parsed(i) for i in range(60)]

        url = str(session.get_bind().engine.url.render_as_string(
            hide_password=False))
        engine = create_engine(url, pool_size=12, max_overflow=8)
        barrier = threading.Barrier(6)
        failures = []

        def importer():
            with Session(engine) as own:
                try:
                    probe = own.get(m.Source, source.id)
                    barrier.wait()
                    reconcile(own, probe, rows)
                    own.commit()
                except Exception as exc:          # pragma: no cover
                    failures.append(exc)

        threads = [threading.Thread(target=importer) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        engine.dispose()

        assert failures == []
        session.expire_all()
        assert count(session, m.Resource) == len(rows)
