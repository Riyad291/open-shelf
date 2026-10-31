"""The hosted-model client.

Every test here drives the real code path with ``urlopen`` replaced, because
the one thing that must not happen is a test suite that reaches the network:
it would be slow, it would cost money, and it would fail in CI where there
is no key.

What is worth testing is not "does it call the API" -- it is every way the
call can fail, because the assistant has to keep working when it does.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from bookstore.config import Config
from bookstore.llm import LlmClient


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    return LlmClient(Config())


def reply_body(text: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


class FakeResponse(io.BytesIO):
    """Enough of an HTTP response for a `with urlopen(...)` block."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def http_error(code: int, message: str = "") -> urllib.error.HTTPError:
    body = json.dumps({"error": {"message": message}}).encode()
    return urllib.error.HTTPError("https://api.example.com/v1", code,
                                  "error", {}, io.BytesIO(body))


class TestTheHappyPath:

    def test_an_answer_comes_back(self, client, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data)
            return FakeResponse(reply_body("Paris."))

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        reply = client.generate("What is the capital of France?",
                                system="Be brief.")

        assert reply.ok and reply.text == "Paris."
        assert captured["url"] == "https://api.example.com/v1/chat/completions"
        assert captured["payload"]["model"] == "test-model"
        # The system prompt must arrive as its own message, not glued to the
        # question: providers weight the two differently.
        roles = [m["role"] for m in captured["payload"]["messages"]]
        assert roles == ["system", "user"]

    def test_the_key_is_sent_as_a_bearer_token(self, client, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured.update(dict(request.header_items()))
            return FakeResponse(reply_body("ok"))

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        client.generate("hello")
        assert captured["Authorization"] == "Bearer test-key-not-real"


class TestFailuresDegradeInsteadOfRaising:
    """Every one of these used to be a 500 waiting to happen."""

    def test_no_key_is_not_an_error(self, monkeypatch):
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        offline = LlmClient(Config())

        assert offline.is_up() is False
        reply = offline.generate("anything")
        assert not reply.ok
        assert "LLM_API_KEY" in reply.error

    def test_no_key_makes_no_request(self, monkeypatch):
        monkeypatch.delenv("LLM_API_KEY", raising=False)

        def explode(*args, **kwargs):           # pragma: no cover
            raise AssertionError("called the API without a key")

        monkeypatch.setattr("urllib.request.urlopen", explode)
        assert LlmClient(Config()).generate("anything").ok is False

    def test_a_rejected_key_says_so_without_printing_it(self, client,
                                                        monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(
                                http_error(401, "Invalid API Key")))
        reply = client.generate("hello")

        assert not reply.ok
        assert "LLM_API_KEY" in reply.error
        assert "test-key-not-real" not in reply.error, "never leak the key"

    def test_an_unknown_model_names_the_setting_to_change(self, client,
                                                          monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(
                                http_error(404)))
        assert "LLM_MODEL" in client.generate("hello").error

    def test_the_network_being_down_is_handled(self, client, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(
                                urllib.error.URLError("no route to host")))
        reply = client.generate("hello")
        assert not reply.ok and "Could not reach" in reply.error

    def test_a_timeout_is_handled(self, client, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(
                                TimeoutError()))
        assert "timed out" in client.generate("hello").error

    @pytest.mark.parametrize("body", [
        b'{"choices": []}',                       # no choices
        b'{"choices": [{"message": {}}]}',        # no content
        b'{"unexpected": true}',                  # another shape entirely
    ])
    def test_a_response_in_the_wrong_shape_is_handled(self, client,
                                                      monkeypatch, body):
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: FakeResponse(body))
        reply = client.generate("hello")
        assert not reply.ok and reply.error


class TestRetries:

    def test_a_rate_limit_is_retried_once_and_can_succeed(self, client,
                                                          monkeypatch):
        monkeypatch.setattr("bookstore.llm.RETRY_AFTER_SECONDS", 0)
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise http_error(429, "Rate limit reached")
            return FakeResponse(reply_body("second time lucky"))

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        reply = client.generate("hello")

        assert reply.ok and reply.text == "second time lucky"
        assert len(calls) == 2

    def test_a_rejected_key_is_not_retried(self, client, monkeypatch):
        """Retrying a 401 only makes the person wait twice for the same no."""
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(1)
            raise http_error(401, "Invalid API Key")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        client.generate("hello")
        assert len(calls) == 1

    def test_retrying_gives_up_rather_than_looping(self, client, monkeypatch):
        monkeypatch.setattr("bookstore.llm.RETRY_AFTER_SECONDS", 0)
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(1)
            raise http_error(503)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        reply = client.generate("hello")
        assert not reply.ok
        assert len(calls) == 2, "one retry, not an unbounded loop"


class TestConfiguration:

    def test_the_environment_wins_over_the_config_file(self, client):
        assert client.base_url == "https://api.example.com/v1"
        assert client.model == "test-model"

    def test_the_defaults_are_groq(self, monkeypatch):
        for name in ("LLM_BASE_URL", "LLM_MODEL"):
            monkeypatch.delenv(name, raising=False)
        config = Config()
        config.set("llm_base_url", "")
        config.set("llm_model", "")
        fresh = LlmClient(config)
        assert "groq.com" in fresh.base_url and fresh.model

    def test_the_provider_is_named_from_the_url(self, monkeypatch):
        monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1")
        assert LlmClient(Config()).provider == "OpenAI"
