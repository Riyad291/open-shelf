"""HTTP contract tests for the REST API.

The rules themselves are tested in ``test_rules.py`` and ``test_integration.py``
against the domain layer directly. This file tests the *transport*: that the
API exposes those rules without weakening them.

Three things are worth checking at this boundary and nowhere else:

* **Status codes.** A refused purchase and a missing book are different
  failures and must not both arrive as a 200 with an error string inside.
* **Privilege.** Every route that needs a role must reject the role below it.
  A rule that is correct in ``rules.py`` is still a hole if the route forgot
  to ask who is calling.
* **Leakage.** Password hashes, salts and other people's records must not
  appear in any response, however convenient the internal dict was.

Each test class gets its own temporary data directory and config, so the live
shop data in ``data/`` is never touched and the tests do not depend on each
other's writes.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi.testclient import TestClient
    HAVE_API = True
except ImportError:                                   # pragma: no cover
    HAVE_API = False

from bookstore import models                          # noqa: E402

if HAVE_API:
    from bookstore.api.app import create_app          # noqa: E402


SKIP = "fastapi and httpx are not installed (pip install -r requirements.txt)"


@unittest.skipUnless(HAVE_API, SKIP)
class ApiTestCase(unittest.TestCase):
    """Base class: a whole API over a throwaway shop."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="api-test-")
        cls.data_dir = os.path.join(cls.tmp, "data")
        cls.config_path = os.path.join(cls.tmp, "settings.json")

        # Face login is mandatory in the shipped config. These tests drive the
        # API without a camera, so the password path has to stay open; the
        # face requirement itself is covered by test_face_flow.py.
        with open(cls.config_path, "w", encoding="utf-8") as handle:
            json.dump({"require_face_login": False,
                       "require_face_enrolment": False,
                       "taboo_words": ["idiot", "garbage", "trash"]},
                      handle)

        cls.app = create_app(data_dir=cls.data_dir, config_path=cls.config_path)
        cls.client = TestClient(cls.app)
        cls.services = cls.app.state.services
        cls._seed()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _seed(cls):
        """A shop small enough to reason about, built through the rules."""
        rules, cat = cls.services.rules, cls.services.catalog

        cls.cheap = cat.add_book("A Cheap Book", "Ann Author", "literature",
                                 1920, 5.00, stock=4)
        cls.dear = cat.add_book("An Expensive Book", "Ann Author", "science",
                                1960, 80.00, stock=2)
        cls.rare = cat.add_book("The Last Copy", "Bo Writer", "history",
                                1890, 12.00, stock=1)

        rules.create_account("alice", "alicepw", balance=200.0,
                             display_name="Alice")
        rules.create_account("broke", "brokepw", balance=1.00,
                             display_name="Broke")
        su = rules.create_account("boss", "bosspw", balance=0.0,
                                  display_name="Boss")
        su["role"] = models.SUPER_USER
        cls.services.storage.save_users()

    # -- helpers -----------------------------------------------------------

    def token_for(self, username: str, password: str) -> str:
        response = self.client.post("/api/auth/login",
                                    json={"username": username,
                                          "password": password})
        self.assertEqual(response.status_code, 200,
                         "%s could not sign in: %s" % (username, response.text))
        return response.json()["token"]

    def auth(self, username: str, password: str) -> dict:
        return {"Authorization": "Bearer " + self.token_for(username, password)}


class TestPublicRoutes(ApiTestCase):
    """What a visitor -- nobody signed in -- is allowed to see."""

    def test_health_reports_the_shop_it_is_serving(self):
        body = self.client.get("/api/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["books"], 3)
        self.assertEqual(body["accounts"], 3)

    def test_catalogue_is_public(self):
        response = self.client.get("/api/books")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 3)

    def test_a_missing_book_is_404_not_an_empty_200(self):
        self.assertEqual(self.client.get("/api/books/nope").status_code, 404)

    def test_facets_count_what_is_actually_shelved(self):
        facets = self.client.get("/api/books/facets").json()
        categories = {f["value"]: f["count"] for f in facets["categories"]}
        self.assertEqual(categories, {"literature": 1, "science": 1,
                                      "history": 1})

    def test_search_finds_by_author(self):
        hits = self.client.get("/api/search", params={"q": "Ann Author"}).json()
        self.assertEqual(len(hits), 2)

    def test_search_requires_a_query(self):
        self.assertEqual(self.client.get("/api/search").status_code, 422)

    def test_visitor_sees_the_list_price(self):
        book = self.client.get("/api/books/" + self.cheap["book_id"]).json()
        self.assertEqual(book["your_price"], 5.00)

    def test_session_of_a_visitor_is_an_answer_not_an_error(self):
        body = self.client.get("/api/auth/session").json()
        self.assertFalse(body["signed_in"])
        self.assertEqual(body["role"], models.VISITOR)


class TestAuthentication(ApiTestCase):

    def test_register_then_use_the_token_it_returns(self):
        response = self.client.post("/api/auth/register", json={
            "username": "newcomer", "password": "pw12345",
            "display_name": "New Comer", "opening_deposit": 30,
        })
        self.assertEqual(response.status_code, 201)
        token = response.json()["token"]

        who = self.client.get("/api/auth/session",
                              headers={"Authorization": "Bearer " + token}).json()
        self.assertTrue(who["signed_in"])
        self.assertEqual(who["username"], "newcomer")
        self.assertEqual(who["balance"], 30.0)

    def test_duplicate_username_is_rejected(self):
        self.client.post("/api/auth/register", json={
            "username": "twice", "password": "pw12345"})
        again = self.client.post("/api/auth/register", json={
            "username": "twice", "password": "pw12345"})
        self.assertEqual(again.status_code, 400)

    def test_wrong_password_is_401(self):
        response = self.client.post("/api/auth/login",
                                    json={"username": "alice",
                                          "password": "not-it"})
        self.assertEqual(response.status_code, 401)

    def test_unknown_user_is_401_and_says_no_more_than_that(self):
        """The message must not reveal whether the username exists."""
        missing = self.client.post("/api/auth/login",
                                   json={"username": "ghost",
                                         "password": "whatever"})
        wrong = self.client.post("/api/auth/login",
                                 json={"username": "alice",
                                       "password": "whatever"})
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json()["detail"], wrong.json()["detail"])

    def test_a_made_up_token_is_rejected(self):
        response = self.client.get(
            "/api/auth/session",
            headers={"Authorization": "Bearer not-a-real-token"})
        self.assertFalse(response.json()["signed_in"])

    def test_logout_invalidates_the_token(self):
        headers = self.auth("alice", "alicepw")
        self.client.post("/api/auth/logout", headers=headers)
        after = self.client.get("/api/auth/session", headers=headers).json()
        self.assertFalse(after["signed_in"])

    def test_no_response_ever_carries_a_password_hash(self):
        headers = self.auth("boss", "bosspw")
        for path in ("/api/auth/session", "/api/admin/accounts"):
            body = self.client.get(path, headers=headers).text
            for secret in ("password_hash", "salt", "bosspw"):
                self.assertNotIn(secret, body,
                                 "%s leaked %r" % (path, secret))


class TestPurchasing(ApiTestCase):

    def test_a_visitor_cannot_buy(self):
        response = self.client.post(
            "/api/books/%s/purchase" % self.cheap["book_id"])
        self.assertEqual(response.status_code, 401)

    def test_buying_moves_money_and_stock(self):
        headers = self.auth("alice", "alicepw")
        before = self.client.get("/api/auth/session", headers=headers).json()
        stock_before = self.client.get(
            "/api/books/" + self.cheap["book_id"]).json()["stock"]

        response = self.client.post(
            "/api/books/%s/purchase" % self.cheap["book_id"], headers=headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertAlmostEqual(body["new_balance"], before["balance"] - 5.00, 2)

        stock_after = self.client.get(
            "/api/books/" + self.cheap["book_id"]).json()["stock"]
        self.assertEqual(stock_after, stock_before - 1)

    def test_insufficient_funds_is_refused_with_a_reason(self):
        headers = self.auth("broke", "brokepw")
        response = self.client.post(
            "/api/books/%s/purchase" % self.dear["book_id"], headers=headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertTrue(body["message"])
        self.assertGreater(body["fine_applied"], 0,
                           "the spec fines an attempt that cannot be covered")

    def test_buying_the_last_copy_then_one_more_fails(self):
        headers = self.auth("alice", "alicepw")
        first = self.client.post(
            "/api/books/%s/purchase" % self.rare["book_id"], headers=headers)
        self.assertTrue(first.json()["ok"])
        second = self.client.post(
            "/api/books/%s/purchase" % self.rare["book_id"], headers=headers)
        self.assertFalse(second.json()["ok"])

    def test_purchase_history_is_the_caller_s_own(self):
        headers = self.auth("alice", "alicepw")
        self.client.post("/api/books/%s/purchase" % self.cheap["book_id"],
                         headers=headers)
        rows = self.client.get("/api/account/purchases", headers=headers).json()
        self.assertTrue(rows)
        self.assertTrue(all("price_paid" in r and "title" in r for r in rows))

    def test_purchase_history_needs_an_account(self):
        self.assertEqual(
            self.client.get("/api/account/purchases").status_code, 401)


class TestPrivilege(ApiTestCase):
    """Every admin route must reject the tier below it."""

    # /api/admin/books is POST-only (adding a book); it is exercised on its
    # own below rather than in the GET sweep.
    ADMIN_ROUTES = ("/api/admin/accounts", "/api/admin/finance",
                    "/api/admin/order-lists", "/api/admin/taboo")

    def test_admin_routes_reject_a_visitor(self):
        for route in self.ADMIN_ROUTES:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 401)

    def test_admin_routes_reject_a_registered_customer(self):
        headers = self.auth("alice", "alicepw")
        for route in self.ADMIN_ROUTES:
            with self.subTest(route=route):
                self.assertEqual(
                    self.client.get(route, headers=headers).status_code, 403)

    def test_admin_routes_admit_the_super_user(self):
        headers = self.auth("boss", "bosspw")
        for route in self.ADMIN_ROUTES:
            with self.subTest(route=route):
                self.assertEqual(
                    self.client.get(route, headers=headers).status_code, 200)

    def test_a_customer_cannot_add_a_book(self):
        response = self.client.post("/api/admin/books",
                                    headers=self.auth("alice", "alicepw"),
                                    json={"title": "Sneaky", "author": "X",
                                          "category": "literature",
                                          "year": 2000, "price": 1.0,
                                          "stock": 1})
        self.assertEqual(response.status_code, 403)

    def test_the_super_user_can_add_a_book_and_it_appears(self):
        headers = self.auth("boss", "bosspw")
        before = len(self.client.get("/api/books").json())
        response = self.client.post("/api/admin/books", headers=headers,
                                    json={"title": "Added By Boss",
                                          "author": "Boss", "category": "poetry",
                                          "year": 2001, "price": 7.5, "stock": 3})
        self.assertEqual(response.status_code, 201)
        after = self.client.get("/api/books").json()
        self.assertEqual(len(after), before + 1)
        self.assertIn("Added By Boss", [b["title"] for b in after])

    def test_finance_totals_are_numbers_not_strings(self):
        body = self.client.get("/api/admin/finance",
                               headers=self.auth("boss", "bosspw")).json()
        for key in ("total_revenue", "revenue_sales", "revenue_fines",
                    "average_order", "inventory_value"):
            with self.subTest(key=key):
                self.assertIsInstance(body[key], (int, float))

    def test_revenue_is_sales_plus_fines_less_refunds(self):
        """The report must not disagree with its own line items."""
        body = self.client.get("/api/admin/finance",
                               headers=self.auth("boss", "bosspw")).json()
        self.assertAlmostEqual(
            body["total_revenue"],
            body["revenue_sales"] + body["revenue_fines"] - body["refunds"], 2)


class TestReviews(ApiTestCase):

    def test_a_visitor_may_comment(self):
        """Spec item 7: "Any user can comment on any book"."""
        response = self.client.post(
            "/api/books/%s/reviews" % self.cheap["book_id"],
            json={"stars": 4, "comment": "Read it in one sitting."})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_a_taboo_word_is_masked_not_stored_raw(self):
        response = self.client.post(
            "/api/books/%s/reviews" % self.dear["book_id"],
            headers=self.auth("alice", "alicepw"),
            json={"stars": 2, "comment": "What an idiot of an author."})
        self.assertEqual(response.status_code, 200)

        book = self.client.get("/api/books/" + self.dear["book_id"]).json()
        comments = " ".join(r["comment"] for r in book.get("reviews", []))
        self.assertNotIn("idiot", comments)

    def test_stars_outside_one_to_five_are_rejected_by_the_schema(self):
        for stars in (0, 6, -1):
            with self.subTest(stars=stars):
                response = self.client.post(
                    "/api/books/%s/reviews" % self.cheap["book_id"],
                    json={"stars": stars, "comment": "x"})
                self.assertEqual(response.status_code, 422)


class TestChatTiers(ApiTestCase):
    """The assistant answers more as privilege rises -- spec items 12-14."""

    SOURCES = {"store data", "via RAG", "via LLM", "access denied",
               "unavailable"}

    def ask(self, question, headers=None):
        response = self.client.post("/api/chat",
                                    json={"question": question},
                                    headers=headers or {})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_a_visitor_can_ask_about_the_catalogue(self):
        answer = self.ask("What books do you have by Ann Author?")
        self.assertTrue(answer["text"])
        self.assertFalse(answer["denied_for_role"])

    def test_a_visitor_is_refused_the_finance_report(self):
        answer = self.ask("What is the total revenue?")
        self.assertTrue(answer["denied_for_role"])
        self.assertEqual(answer["source"], "access denied")

    def test_the_super_user_is_not_refused_the_finance_report(self):
        answer = self.ask("What is the total revenue?",
                          self.auth("boss", "bosspw"))
        self.assertFalse(answer["denied_for_role"])

    def test_a_refusal_never_includes_the_number_it_refused(self):
        """Denying the question but printing the answer would be worse than
        allowing it."""
        truth = self.ask("What is the total revenue?",
                         self.auth("boss", "bosspw"))["text"]
        amounts = [w for w in truth.replace("\n", " ").split() if "$" in w]
        refusal = self.ask("What is the total revenue?")["text"]
        for amount in amounts:
            self.assertNotIn(amount, refusal)

    def test_no_answer_ever_carries_a_credential(self):
        answer = self.ask("What is the balance of the account called boss?",
                          self.auth("alice", "alicepw"))
        blob = json.dumps(answer)
        for secret in ("bosspw", "password_hash", "salt"):
            self.assertNotIn(secret, blob)

    def test_every_answer_names_where_it_came_from(self):
        for question in ("What are your opening hours?",
                         "What books do you have?",
                         "What is the capital of Mongolia?"):
            with self.subTest(question=question):
                self.assertIn(self.ask(question)["source"], self.SOURCES)

    def test_a_rag_answer_cites_the_file_it_came_from(self):
        answer = self.ask("What are your opening hours?")
        if answer["source"] == "via RAG":
            self.assertTrue(answer["citations"],
                            "a retrieved answer must say what it retrieved")


class TestStaticClient(ApiTestCase):
    """The browser client is served by the same process as the API."""

    def test_the_web_app_is_mounted(self):
        response = self.client.get("/app/", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("<title>", response.text)

    def test_the_root_sends_you_to_the_app(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertIn(response.status_code, (301, 302, 307, 308))

    def test_openapi_describes_every_route(self):
        schema = self.client.get("/openapi.json").json()
        self.assertGreaterEqual(len(schema["paths"]), 20)
        for path in ("/api/books", "/api/chat", "/api/auth/login"):
            self.assertIn(path, schema["paths"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
