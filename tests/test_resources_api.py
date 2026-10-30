"""The resource catalogue's public API."""

from __future__ import annotations

import pytest
from sqlalchemy import event

from bookstore.db import models as m
from bookstore.ingest.parsers import ParsedResource
from bookstore.ingest.pipeline import get_or_create_source, reconcile


@pytest.fixture()
def catalogue(session):
    source = get_or_create_source(
        session, "probe", name="Probe Source", homepage="https://probe.org",
        attribution="Data from Probe, CC BY 4.0.")
    reconcile(session, source, [
        ParsedResource(external_id="a", title="Learning Python",
                       url="https://example.org/a", authors="Mark Lutz",
                       media="PDF", topics=["Python"],
                       work_key="shared-key"),
        ParsedResource(external_id="b", title="Learning Python",
                       url="https://example.org/b", authors="Mark Lutz",
                       media="HTML", topics=["Python"],
                       work_key="shared-key"),
        ParsedResource(external_id="c", title="Rust in Action",
                       url="https://example.org/c", authors="Tim McNamara",
                       media="EPUB", topics=["Rust", "Systems"],
                       work_key="rust-key"),
    ])
    session.commit()
    return {r.external_id: r for r in session.query(m.Resource)}


class TestBrowsing:
    def test_everything_is_listed(self, client, catalogue):
        body = client.get("/api/resources").json()
        assert body["total"] == 3 and len(body["items"]) == 3

    def test_filter_by_topic(self, client, catalogue):
        body = client.get("/api/resources?topic=rust").json()
        assert [r["title"] for r in body["items"]] == ["Rust in Action"]

    def test_filter_by_media(self, client, catalogue):
        body = client.get("/api/resources?media=pdf").json()
        assert body["total"] == 1

    def test_search_matches_authors_too(self, client, catalogue):
        body = client.get("/api/resources?q=mcnamara").json()
        assert body["total"] == 1

    def test_dead_links_are_hidden(self, client, session, catalogue):
        catalogue["c"].dead = True
        session.commit()
        assert client.get("/api/resources").json()["total"] == 2

    def test_topics_are_counted(self, client, catalogue):
        topics = {t["slug"]: t["count"]
                  for t in client.get("/api/resources/topics").json()}
        assert topics["python"] == 2 and topics["rust"] == 1

    def test_an_absurd_page_size_is_refused(self, client, catalogue):
        assert client.get("/api/resources?limit=500").status_code == 422


class TestDetail:
    def test_detail_carries_attribution(self, client, catalogue):
        body = client.get("/api/resources/%d" % catalogue["c"].id).json()
        assert body["attribution"] == "Data from Probe, CC BY 4.0."
        assert body["source_homepage"] == "https://probe.org"

    def test_other_editions_are_offered(self, client, catalogue):
        body = client.get("/api/resources/%d" % catalogue["a"].id).json()
        assert [r["url"] for r in body["also_available"]] == [
            "https://example.org/b"]

    def test_a_missing_resource_is_404(self, client, catalogue):
        assert client.get("/api/resources/99999").status_code == 404


class TestClickThrough:
    def test_open_redirects_to_the_book(self, client, catalogue):
        reply = client.get("/api/resources/%d/open" % catalogue["a"].id,
                           follow_redirects=False)
        assert reply.status_code == 303
        assert reply.headers["location"] == "https://example.org/a"

    def test_open_counts_the_click(self, client, session, catalogue):
        for _ in range(3):
            client.get("/api/resources/%d/open" % catalogue["a"].id,
                       follow_redirects=False)
        session.expire_all()
        assert session.get(m.Resource, catalogue["a"].id).click_count == 3

    def test_popular_sorts_by_clicks(self, client, catalogue):
        client.get("/api/resources/%d/open" % catalogue["c"].id,
                   follow_redirects=False)
        body = client.get("/api/resources?sort=popular").json()
        assert body["items"][0]["title"] == "Rust in Action"


class TestQueryCost:
    def test_a_bigger_page_does_not_mean_more_queries(self, client, session,
                                                      catalogue):
        """No N+1: the statement count must not grow with the page size."""
        engine = session.get_bind().engine
        counted = []

        def count(*_args, **_kwargs):
            counted.append(1)

        def statements_for(limit):
            counted.clear()
            event.listen(engine, "before_cursor_execute", count)
            try:
                client.get("/api/resources?limit=%d" % limit)
            finally:
                event.remove(engine, "before_cursor_execute", count)
            return len(counted)

        assert statements_for(1) == statements_for(50)
