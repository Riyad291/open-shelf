"""The HTTP contract.

The rules are tested against the services in ``test_rules.py``. These tests
cover what is only visible at the boundary:

* **Status codes.** A missing book, a refused review and a conflicting delete
  are different failures and must not all arrive as a 200 with a string.
* **Privilege.** Every route that needs a role must reject the role below it.
  A rule that is right in the service is still a hole if the route forgot to
  ask who is calling.
* **Scoping.** One member must not be able to edit another's review.
* **Leakage.** No response anywhere may contain a password hash or a salt.
"""

from __future__ import annotations

import pytest

from bookstore.db import models as m
from bookstore.services import accounts


@pytest.fixture()
def shop(session, config, make_book, make_user):
    """A small library with every role present."""
    books = {
        "cheap": make_book(title="A Cheap Book", author="Ann Author",
                           category="literature", year=1920),
        "dear": make_book(title="An Expensive Book", author="Bo Writer",
                          category="science", year=1960),
        "last": make_book(title="The Last Copy", author="Cy Scribe",
                          category="history", year=1890),
    }
    people = {
        "alice": make_user("alice", display_name="Alice"),
        "broke": make_user("broke", display_name="Quiet"),
        "vip": make_user("vip", role=m.VIP),
        "boss": make_user("boss", role=m.SUPER_USER),
    }
    session.commit()
    return {"books": books, "people": people}


class TestPublicRoutes:

    def test_health_reports_what_it_is_serving(self, client, shop):
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["books"] == 3
        assert body["database"] == "ok"

    def test_the_catalogue_is_public_and_paginated(self, client, shop):
        body = client.get("/api/books").json()
        assert body["total"] == 3
        assert len(body["items"]) == 3
        assert {"items", "total", "limit", "offset"} <= set(body)

    def test_a_missing_book_is_404_not_an_empty_200(self, client, shop):
        assert client.get("/api/books/999999").status_code == 404

    def test_facets_count_what_is_shelved(self, client, shop):
        facets = client.get("/api/books/facets").json()
        categories = {f["value"]: f["count"] for f in facets["categories"]}
        assert categories == {"literature": 1, "science": 1, "history": 1}

    def test_the_decade_facet_agrees_with_the_decade_filter(self, client, shop):
        """These disagreed once: true division put an 1859 book in the 1860s."""
        facets = client.get("/api/books/facets").json()
        for facet in facets["decades"]:
            page = client.get("/api/books",
                              params={"decade": facet["value"]}).json()
            assert page["total"] == facet["count"], facet["value"]

    def test_search_needs_a_query(self, client, shop):
        assert client.get("/api/search").status_code == 422

    def test_a_visitor_is_a_valid_caller_not_an_error(self, client, shop):
        body = client.get("/api/auth/session").json()
        assert body["signed_in"] is False
        assert body["role"] == m.VISITOR


class TestAuthentication:

    def test_register_then_use_the_token_it_returns(self, client):
        created = client.post("/api/auth/register", json={
            "username": "newcomer", "password": "pw12345",
            "display_name": "New Comer"})
        assert created.status_code == 201

        token = created.json()["access_token"]
        who = client.get("/api/auth/session",
                         headers={"Authorization": "Bearer " + token}).json()
        assert who["username"] == "newcomer"
        assert who["role_name"] == "Member"

    def test_a_duplicate_username_is_refused(self, client):
        body = {"username": "twice", "password": "pw12345"}
        assert client.post("/api/auth/register", json=body).status_code == 201
        assert client.post("/api/auth/register", json=body).status_code == 400

    def test_wrong_password_and_unknown_user_are_indistinguishable(
            self, client, shop):
        """Telling them apart lets anyone enumerate which usernames exist."""
        missing = client.post("/api/auth/login",
                              json={"username": "ghost", "password": "x"})
        wrong = client.post("/api/auth/login",
                            json={"username": "alice", "password": "x"})
        assert missing.status_code == wrong.status_code == 401
        assert missing.json()["detail"] == wrong.json()["detail"]

    def test_a_refresh_token_can_be_exchanged_once(self, client, shop):
        tokens = client.post("/api/auth/login",
                             json={"username": "alice", "password": "pw12345"}).json()

        again = client.post("/api/auth/refresh",
                            json={"refresh_token": tokens["refresh_token"]})
        assert again.status_code == 200
        assert again.json()["refresh_token"] != tokens["refresh_token"]

        reused = client.post("/api/auth/refresh",
                             json={"refresh_token": tokens["refresh_token"]})
        assert reused.status_code == 401, "a rotated token must not work twice"

    def test_reusing_a_rotated_token_revokes_the_whole_family(
            self, client, shop):
        first = client.post("/api/auth/login",
                            json={"username": "alice", "password": "pw12345"}).json()
        second = client.post("/api/auth/refresh",
                             json={"refresh_token": first["refresh_token"]}).json()

        client.post("/api/auth/refresh",
                    json={"refresh_token": first["refresh_token"]})

        assert client.post("/api/auth/refresh",
                           json={"refresh_token": second["refresh_token"]}
                           ).status_code == 401

    def test_logging_out_twice_is_not_an_error(self, client, shop):
        tokens = client.post("/api/auth/login",
                             json={"username": "alice", "password": "pw12345"}).json()
        body = {"refresh_token": tokens["refresh_token"]}
        assert client.post("/api/auth/logout", json=body).status_code == 204
        assert client.post("/api/auth/logout", json=body).status_code == 204

    def test_a_suspended_account_cannot_sign_in(self, client, session, shop):
        accounts.suspend(session, shop["people"]["broke"], "testing")
        session.commit()
        response = client.post("/api/auth/login",
                               json={"username": "broke", "password": "pw12345"})
        assert response.status_code == 403

    def test_no_response_carries_a_credential(self, client, sign_in, shop):
        boss = sign_in("boss")
        for path in ("/api/auth/session", "/api/admin/accounts"):
            body = client.get(path, headers=boss).text
            for secret in ("password_hash", "salt", "pw12345"):
                assert secret not in body, "%s leaked %r" % (path, secret)


class TestPrivilege:

    ADMIN_GETS = ("/api/admin/accounts", "/api/admin/summary",
                  "/api/admin/analytics", "/api/admin/rating-lists",
                  "/api/admin/taboo", "/api/admin/books")

    @pytest.mark.parametrize("route", ADMIN_GETS)
    def test_a_visitor_is_refused(self, client, shop, route):
        assert client.get(route).status_code == 401

    @pytest.mark.parametrize("route", ADMIN_GETS)
    def test_a_customer_is_refused(self, client, sign_in, shop, route):
        assert client.get(route, headers=sign_in("alice")).status_code == 403

    @pytest.mark.parametrize("route", ADMIN_GETS)
    def test_the_super_user_is_admitted(self, client, sign_in, shop, route):
        assert client.get(route, headers=sign_in("boss")).status_code == 200

    def test_a_vip_is_still_not_an_administrator(self, client, sign_in, shop):
        assert client.get("/api/admin/summary",
                          headers=sign_in("vip")).status_code == 403

    def test_a_customer_cannot_add_a_book(self, client, sign_in, shop):
        response = client.post("/api/admin/books", headers=sign_in("alice"),
                               json={"title": "Sneaky", "author": "X",
                                     "category": "literature", "year": 2000})
        assert response.status_code == 403


class TestAdminCrud:

    def test_a_book_can_be_created_patched_and_deleted(
            self, client, sign_in, shop):
        boss = sign_in("boss")
        created = client.post("/api/admin/books", headers=boss,
                              json={"title": "Temporary", "author": "T",
                                    "category": "test", "year": 2000})
        assert created.status_code == 201
        book_id = created.json()["id"]

        patched = client.patch("/api/admin/books/%d" % book_id, headers=boss,
                               json={"year": 2001})
        assert patched.status_code == 200
        assert patched.json()["year"] == 2001
        assert patched.json()["title"] == "Temporary", \
            "a PATCH must not blank the fields it did not mention"

        assert client.delete("/api/admin/books/%d" % book_id,
                             headers=boss).status_code == 204
        assert client.get("/api/books/%d" % book_id).status_code == 404

    def test_a_reviewed_book_cannot_be_deleted(self, client, sign_in, shop):
        book_id = shop["books"]["cheap"].id
        client.post("/api/books/%d/reviews" % book_id, headers=sign_in("alice"),
                    json={"stars": 4, "comment": "Good."})

        response = client.delete("/api/admin/books/%d" % book_id,
                                 headers=sign_in("boss"))
        assert response.status_code == 409, "deleting it would destroy reviews"
        assert "review" in response.json()["detail"]

    def test_taboo_words_round_trip(self, client, sign_in, shop):
        boss = sign_in("boss")
        assert client.post("/api/admin/taboo", headers=boss,
                           json={"word": "zzztest"}).status_code == 201
        assert client.post("/api/admin/taboo", headers=boss,
                           json={"word": "zzztest"}).status_code == 409
        assert client.delete("/api/admin/taboo/zzztest",
                             headers=boss).status_code == 200
        assert client.delete("/api/admin/taboo/zzztest",
                             headers=boss).status_code == 404

    def test_the_super_user_cannot_suspend_themselves(
            self, client, sign_in, shop):
        boss = sign_in("boss")
        me = client.get("/api/auth/session", headers=boss).json()["user_id"]
        response = client.post("/api/admin/accounts/%d/suspend" % me,
                               headers=boss, json={"reason": "oops"})
        assert response.status_code == 409


class TestReviewsOverHttp:

    def test_a_visitor_may_comment(self, client, shop):
        response = client.post(
            "/api/books/%d/reviews" % shop["books"]["cheap"].id,
            json={"stars": 4, "comment": "Read it in one sitting."})
        assert response.status_code == 200
        assert response.json()["ok"]

    @pytest.mark.parametrize("stars", [0, 6, -1])
    def test_stars_outside_one_to_five_are_rejected(self, client, shop, stars):
        response = client.post(
            "/api/books/%d/reviews" % shop["books"]["cheap"].id,
            json={"stars": stars, "comment": "x"})
        assert response.status_code == 422

    def test_a_review_can_only_be_edited_by_its_author(
            self, client, sign_in, shop):
        alice = sign_in("alice")
        posted = client.post("/api/books/%d/reviews" % shop["books"]["cheap"].id,
                             headers=alice,
                             json={"stars": 4, "comment": "Fine."}).json()
        review_id = posted["review"]["id"]

        assert client.patch("/api/reviews/%d" % review_id,
                            headers=sign_in("vip"),
                            json={"stars": 1}).status_code == 404
        assert client.patch("/api/reviews/%d" % review_id, headers=alice,
                            json={"stars": 5}).status_code == 200

    def test_an_edit_is_screened_like_a_new_comment(
            self, client, sign_in, shop):
        alice = sign_in("alice")
        posted = client.post("/api/books/%d/reviews" % shop["books"]["cheap"].id,
                             headers=alice,
                             json={"stars": 4, "comment": "Fine."}).json()

        response = client.patch(
            "/api/reviews/%d" % posted["review"]["id"], headers=alice,
            json={"comment": "stupid garbage trash idiot moron"})
        assert response.status_code == 400


class TestAssistant:

    def test_a_visitor_is_refused_the_insights(self, client, shop):
        body = client.post("/api/chat", json={
            "question": "How many resources are in the catalogue?"}).json()
        assert body["denied_for_role"]

    def test_the_super_user_gets_real_figures_not_an_error(
            self, client, sign_in, shop):
        """This failed silently once: the answer was labelled a success and
        read 'Something went wrong answering that'."""
        body = client.post("/api/chat", headers=sign_in("boss"), json={
            "question": "How many resources are in the catalogue?"}).json()
        assert not body["denied_for_role"]
        assert body["source"] == "store data"
        assert "CATALOGUE" in body["text"]
        assert "went wrong" not in body["text"]

    def test_a_site_question_is_answered_from_the_files_and_cited(
            self, client, shop):
        body = client.post("/api/chat",
                           json={"question": "Where do the books come from?"}).json()
        assert body["source"] == "via RAG"
        assert body["citations"]

    def test_an_off_topic_question_is_not_answered_from_the_files(
            self, client, shop):
        """Without the rare-term rule this scored just high enough on filler
        words to be answered from the site's own help files."""
        body = client.post(
            "/api/chat",
            json={"question": "What is the capital of Mongolia?"}).json()
        assert body["source"] != "via RAG"

    def test_every_answer_says_where_it_came_from(self, client, shop):
        for question in ("Where do the books come from?",
                         "What books do you have?",
                         "What is the capital of Mongolia?"):
            body = client.post("/api/chat", json={"question": question}).json()
            assert body["source"] in {"store data", "via RAG", "via LLM",
                                      "access denied", "unavailable"}


class TestBiometricRoutes:

    def test_the_status_route_needs_an_account(self, client, shop):
        assert client.get("/api/biometrics/status").status_code == 401

    def test_face_login_before_anyone_enrols_is_a_clear_conflict(
            self, client, shop):
        response = client.post(
            "/api/biometrics/face/login",
            files=[("frames", ("f.png", b"not-an-image", "image/png"))])
        assert response.status_code == 409
        assert "enrolled" in response.json()["detail"].lower()


class TestModerationCannotBeBypassed:
    """Reviews and moderation:'s warning ladder applies to every route that
    accepts a comment, not only the one that creates it.

    Before this was fixed, PATCH /api/reviews/{id} screened the text and
    refused a blocked edit without issuing a warning. Posting one clean
    review and then editing it was an unlimited supply of blocked comments
    at no cost, which made the ladder decorative.
    """

    def _taboo(self, config):
        return " ".join(config.taboo_words[:5])

    def test_a_blocked_edit_issues_a_warning(
            self, client, session, config, make_book, make_user, sign_in):
        book = make_book()
        person = make_user("editor")
        session.commit()
        head = sign_in("editor")

        created = client.post("/api/books/%d/reviews" % book.id, headers=head,
                              json={"stars": 5, "comment": "A fine book."})
        assert created.status_code == 200, created.text
        review_id = created.json()["review"]["id"]

        before = person.warnings
        response = client.patch("/api/reviews/%d" % review_id, headers=head,
                                json={"comment": self._taboo(config)})
        assert response.status_code == 400

        session.expire_all()
        after = session.get(type(person), person.id).warnings
        assert after == before + 1, (
            "a blocked edit must cost a warning, as a blocked post does")

    def test_repeated_blocked_edits_eventually_suspend(
            self, client, session, config, make_book, make_user, sign_in):
        book = make_book()
        person = make_user("persistent")
        session.commit()
        head = sign_in("persistent")

        created = client.post("/api/books/%d/reviews" % book.id, headers=head,
                              json={"stars": 4, "comment": "Good."})
        review_id = created.json()["review"]["id"]

        model = type(person)
        for _ in range(config.warning_limit + 1):
            client.patch("/api/reviews/%d" % review_id, headers=head,
                         json={"comment": self._taboo(config)})

        session.expire_all()
        fresh = session.get(model, person.id)
        assert fresh.suspended or fresh.role != m.VIP, (
            "the ladder must reach its end through the edit route too")

    def test_a_rejected_edit_does_not_apply_the_rating(
            self, client, session, config, make_book, make_user, sign_in):
        """An edit is one act: if the comment is refused, the stars that
        travelled with it are refused too."""
        book = make_book()
        make_user("partial")
        session.commit()
        head = sign_in("partial")

        created = client.post("/api/books/%d/reviews" % book.id, headers=head,
                              json={"stars": 5, "comment": "Excellent."})
        review_id = created.json()["review"]["id"]

        response = client.patch(
            "/api/reviews/%d" % review_id, headers=head,
            json={"stars": 1, "comment": self._taboo(config)})
        assert response.status_code == 400

        listed = client.get("/api/books/%d/reviews" % book.id).json()
        still = [r for r in listed if r["id"] == review_id]
        assert still and still[0]["stars"] == 5, (
            "the rating from a refused edit must not survive")
