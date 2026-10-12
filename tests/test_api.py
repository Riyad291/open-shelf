"""The HTTP contract.

The rules are tested against the services in ``test_rules.py``. These tests
cover what is only visible at the boundary:

* **Status codes.** A refused purchase, a missing book and a stock conflict
  are different failures and must not all arrive as a 200 with a string.
* **Privilege.** Every route that needs a role must reject the role below it.
  A rule that is right in the service is still a hole if the route forgot to
  ask who is calling.
* **Scoping.** One customer must not be able to read another's orders.
* **Leakage.** No response anywhere may contain a password hash or a salt.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bookstore.db import models as m
from bookstore.services import accounts, cart, checkout, reviews


@pytest.fixture()
def shop(session, config, make_book, make_user):
    """A small shop with every role present and some history."""
    books = {
        "cheap": make_book(title="A Cheap Book", author="Ann Author",
                           category="literature", year=1920, price="5.00",
                           stock=4),
        "dear": make_book(title="An Expensive Book", author="Bo Writer",
                          category="science", year=1960, price="80.00",
                          stock=2),
        "last": make_book(title="The Last Copy", author="Cy Scribe",
                          category="history", year=1890, price="12.00",
                          stock=1),
    }
    people = {
        "alice": make_user("alice", balance="200.00", display_name="Alice"),
        "broke": make_user("broke", balance="1.00", display_name="Broke"),
        "vip": make_user("vip", role=m.VIP, balance="200.00"),
        "boss": make_user("boss", role=m.SUPER_USER, balance="0.00"),
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
            "display_name": "New Comer", "opening_deposit": "30"})
        assert created.status_code == 201

        token = created.json()["access_token"]
        who = client.get("/api/auth/session",
                         headers={"Authorization": "Bearer " + token}).json()
        assert who["username"] == "newcomer"
        assert Decimal(who["balance"]) == Decimal("30")

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


class TestCartAndCheckout:

    def test_the_cart_needs_an_account(self, client, shop):
        assert client.get("/api/cart").status_code == 401

    def test_adding_a_missing_book_is_404(self, client, sign_in, shop):
        response = client.post("/api/cart/items", headers=sign_in("alice"),
                               json={"book_id": 999999, "quantity": 1})
        assert response.status_code == 404

    def test_asking_for_more_than_the_stock_is_409(self, client, sign_in, shop):
        response = client.post(
            "/api/cart/items", headers=sign_in("alice"),
            json={"book_id": shop["books"]["last"].id, "quantity": 5})
        assert response.status_code == 409

    def test_adding_the_same_book_twice_raises_the_quantity(
            self, client, sign_in, shop):
        alice = sign_in("alice")
        book_id = shop["books"]["cheap"].id
        client.post("/api/cart/items", headers=alice,
                    json={"book_id": book_id, "quantity": 1})
        body = client.post("/api/cart/items", headers=alice,
                           json={"book_id": book_id, "quantity": 1}).json()

        assert len(body["lines"]) == 1
        assert body["lines"][0]["quantity"] == 2

    def test_setting_a_quantity_to_zero_removes_the_line(
            self, client, sign_in, shop):
        alice = sign_in("alice")
        book_id = shop["books"]["cheap"].id
        client.post("/api/cart/items", headers=alice,
                    json={"book_id": book_id, "quantity": 2})
        body = client.patch("/api/cart/items/%d" % book_id, headers=alice,
                            json={"quantity": 0}).json()
        assert body["lines"] == []

    def test_checkout_creates_an_order_and_a_payment(
            self, client, sign_in, shop):
        alice = sign_in("alice")
        client.post("/api/cart/items", headers=alice,
                    json={"book_id": shop["books"]["cheap"].id, "quantity": 2})

        result = client.post("/api/checkout", headers=alice).json()
        assert result["ok"]
        assert Decimal(result["order"]["total"]) == Decimal("10.00")
        assert len(result["order"]["payments"]) == 1

    def test_an_empty_cart_cannot_be_checked_out(self, client, sign_in, shop):
        body = client.post("/api/checkout", headers=sign_in("alice")).json()
        assert not body["ok"]
        assert "empty" in body["message"].lower()

    def test_a_refusal_is_a_200_with_the_reason_not_an_error_status(
            self, client, sign_in, shop):
        """The client has to show the fine and the new balance, so a refusal
        is a result rather than a failure."""
        broke = sign_in("broke")
        client.post("/api/cart/items", headers=broke,
                    json={"book_id": shop["books"]["dear"].id, "quantity": 1})

        response = client.post("/api/checkout", headers=broke)
        assert response.status_code == 200
        body = response.json()
        assert not body["ok"]
        assert Decimal(body["fine_applied"]) == Decimal("1.00")

    def test_a_vip_is_charged_less_for_the_same_cart(
            self, client, sign_in, shop):
        book_id = shop["books"]["cheap"].id
        for who, expected in (("alice", "5.00"), ("vip", "4.50")):
            headers = sign_in(who)
            client.post("/api/cart/items", headers=headers,
                        json={"book_id": book_id, "quantity": 1})
            body = client.post("/api/checkout", headers=headers).json()
            assert Decimal(body["order"]["total"]) == Decimal(expected), who


class TestOrderScoping:

    def test_an_order_is_visible_to_its_owner(self, client, sign_in, shop):
        alice = sign_in("alice")
        client.post("/api/cart/items", headers=alice,
                    json={"book_id": shop["books"]["cheap"].id, "quantity": 1})
        order_id = client.post("/api/checkout", headers=alice).json()["order"]["id"]

        assert client.get("/api/orders/%d" % order_id,
                          headers=alice).status_code == 200

    def test_another_customer_gets_404_not_403(self, client, sign_in, shop):
        """403 would confirm the order exists, which is not theirs to know."""
        alice = sign_in("alice")
        client.post("/api/cart/items", headers=alice,
                    json={"book_id": shop["books"]["cheap"].id, "quantity": 1})
        order_id = client.post("/api/checkout", headers=alice).json()["order"]["id"]

        assert client.get("/api/orders/%d" % order_id,
                          headers=sign_in("vip")).status_code == 404

    def test_the_order_list_cannot_be_pointed_at_someone_else(
            self, client, sign_in, shop):
        alice = sign_in("alice")
        client.post("/api/cart/items", headers=alice,
                    json={"book_id": shop["books"]["cheap"].id, "quantity": 1})
        client.post("/api/checkout", headers=alice)

        assert client.get("/api/orders", headers=sign_in("vip")).json() == []


class TestPrivilege:

    ADMIN_GETS = ("/api/admin/accounts", "/api/admin/finance",
                  "/api/admin/analytics", "/api/admin/order-lists",
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
        assert client.get("/api/admin/finance",
                          headers=sign_in("vip")).status_code == 403

    def test_a_customer_cannot_add_a_book(self, client, sign_in, shop):
        response = client.post("/api/admin/books", headers=sign_in("alice"),
                               json={"title": "Sneaky", "author": "X",
                                     "category": "literature", "year": 2000,
                                     "price": "1.00", "stock": 1})
        assert response.status_code == 403


class TestAdminCrud:

    def test_a_book_can_be_created_patched_and_deleted(
            self, client, sign_in, shop):
        boss = sign_in("boss")
        created = client.post("/api/admin/books", headers=boss,
                              json={"title": "Temporary", "author": "T",
                                    "category": "test", "year": 2000,
                                    "price": "5.00", "stock": 3})
        assert created.status_code == 201
        book_id = created.json()["id"]

        patched = client.patch("/api/admin/books/%d" % book_id, headers=boss,
                               json={"price": "7.50"})
        assert patched.status_code == 200
        assert Decimal(patched.json()["price"]) == Decimal("7.50")
        assert patched.json()["title"] == "Temporary", \
            "a PATCH must not blank the fields it did not mention"

        assert client.delete("/api/admin/books/%d" % book_id,
                             headers=boss).status_code == 204
        assert client.get("/api/books/%d" % book_id).status_code == 404

    def test_a_sold_book_cannot_be_deleted(self, client, sign_in, shop):
        alice = sign_in("alice")
        book_id = shop["books"]["cheap"].id
        client.post("/api/cart/items", headers=alice,
                    json={"book_id": book_id, "quantity": 1})
        client.post("/api/checkout", headers=alice)

        response = client.delete("/api/admin/books/%d" % book_id,
                                 headers=sign_in("boss"))
        assert response.status_code == 409, "deleting it would destroy receipts"

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

    def test_a_visitor_is_refused_the_finances(self, client, shop):
        body = client.post("/api/chat",
                           json={"question": "What is the total revenue?"}).json()
        assert body["denied_for_role"]

    def test_the_super_user_gets_real_figures_not_an_error(
            self, client, sign_in, shop):
        """This failed silently once: the answer was labelled a success and
        read 'Something went wrong answering that'."""
        body = client.post("/api/chat", headers=sign_in("boss"),
                           json={"question": "What is the total revenue?"}).json()
        assert not body["denied_for_role"]
        assert body["source"] == "store data"
        assert "TOTAL REVENUE" in body["text"]
        assert "went wrong" not in body["text"]

    def test_a_shop_question_is_answered_from_the_files_and_cited(
            self, client, shop):
        body = client.post("/api/chat",
                           json={"question": "What are your opening hours?"}).json()
        assert body["source"] == "via RAG"
        assert body["citations"]

    def test_an_off_topic_question_is_not_answered_from_the_files(
            self, client, shop):
        """Without the rare-term rule this scored just high enough on filler
        words to be answered from the shop's own FAQ."""
        body = client.post(
            "/api/chat",
            json={"question": "What is the capital of Mongolia?"}).json()
        assert body["source"] != "via RAG"

    def test_every_answer_says_where_it_came_from(self, client, shop):
        for question in ("What are your opening hours?",
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
