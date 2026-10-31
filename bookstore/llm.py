"""The hosted language model behind the assistant's general-knowledge answers.

Open Shelf answers questions about itself from local files (``rag.py``).
Anything the files do not cover is passed here, to a chat-completions API.

**Any OpenAI-compatible provider works**, because they all expose the same
``POST /chat/completions`` shape. Only the base URL, the model name and the
key change, and all three come from the environment:

    LLM_API_KEY    required; without it the assistant still answers from the
                   local files and says the model is not configured
    LLM_BASE_URL   default https://api.groq.com/openai/v1
    LLM_MODEL      default openai/gpt-oss-20b

Groq is the default because its free tier needs no card, which matters for a
deployed demo. The same code reaches OpenAI, OpenRouter, Together or
DeepSeek by changing ``LLM_BASE_URL``.

Written against ``urllib`` from the standard library rather than a vendor
SDK. An SDK here would be one dependency, one more thing to keep current,
and a layer between this project and an HTTP request that is worth being
able to read. The whole client is one POST, and the error handling is the
interesting part.

**The key is read from the environment and never written to a file.** It is
not in ``config/settings.json``, which is committed, and it is never logged
or returned in an error message.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Tuple

from .config import Config

#: Groq's free tier. Any OpenAI-compatible endpoint can replace it.
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
#: Groq's Llama models moved to an Enterprise plan, so a free key is told
#: the model "does not exist or you do not have access to it". The open
#: GPT-OSS models are the ones a free key can reach. Any provider retires
#: models eventually, which is why this is a setting and not a literal in
#: the request: LLM_MODEL overrides it without a code change.
DEFAULT_MODEL = "openai/gpt-oss-20b"

#: One retry, for the two failures that are worth retrying: a rate limit and
#: a server-side error. A bad key or a bad request will fail again
#: identically, so retrying those only makes the person wait twice.
RETRY_STATUSES = (429, 500, 502, 503, 504)
RETRY_AFTER_SECONDS = 1.5

#: Identify this client by name. Without it urllib sends
#: ``Python-urllib/3.12``, and the content-delivery networks in front of
#: several providers answer that with a bare 403 and an HTML body. The call
#: never reaches the API, so the failure looks like a rejected key when the
#: key is fine. Found against Groq on the deployed site.
USER_AGENT = "open-shelf/1.0 (+https://github.com/Riyad291/open-shelf)"


@dataclass
class LlmReply:
    ok: bool
    text: str
    model: str = ""
    error: str = ""


class LlmClient:
    """A chat-completions client for any OpenAI-compatible provider."""

    def __init__(self, config: Config):
        self.cfg = config

    # -- configuration ------------------------------------------------------
    #
    # Environment first, then settings.json, then the default. Deployment
    # sets environment variables; it does not edit a file inside the image.

    def _setting(self, env_name: str, key: str, fallback: str) -> str:
        return str(os.environ.get(env_name)
                   or self.cfg.get(key)
                   or fallback)

    @property
    def base_url(self) -> str:
        return self._setting("LLM_BASE_URL", "llm_base_url",
                             DEFAULT_BASE_URL).rstrip("/")

    @property
    def model(self) -> str:
        return self._setting("LLM_MODEL", "llm_model", DEFAULT_MODEL)

    @property
    def timeout(self) -> int:
        return int(self.cfg.get("llm_timeout_seconds") or 30)

    @property
    def api_key(self) -> str:
        """The key, from the environment only. Never from a committed file."""
        return os.environ.get("LLM_API_KEY", "").strip()

    @property
    def provider(self) -> str:
        """A readable name for the host, for status lines and logs."""
        host = self.base_url.split("//")[-1].split("/")[0]
        return {"api.groq.com": "Groq",
                "api.openai.com": "OpenAI",
                "openrouter.ai": "OpenRouter",
                "localhost:11434": "Ollama"}.get(host, host)

    # -- health -------------------------------------------------------------

    def is_up(self) -> bool:
        """Whether a call is worth attempting.

        Deliberately not a network probe. This is called on the health
        endpoint and before every retrieval-augmented answer, and a probe
        would spend a request, and a second or two, on each one. A key that
        is present but wrong fails on the real call, which already degrades
        gracefully.
        """
        return bool(self.api_key)

    def status(self) -> Tuple[bool, str]:
        if not self.api_key:
            return False, (
                "No LLM_API_KEY is set, so general questions are unavailable. "
                "Questions about this site are still answered from its own "
                "files. Get a free key at https://console.groq.com and set "
                "LLM_API_KEY."
            )
        return True, "%s ready with model '%s'." % (self.provider, self.model)

    # -- generation ---------------------------------------------------------

    def generate(self, prompt: str, system: str = "",
                 temperature: float = 0.3) -> LlmReply:
        """One question, one answer. Never raises."""
        if not self.api_key:
            return LlmReply(False, "", self.model, self.status()[1])

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }

        for attempt in (1, 2):
            reply, retryable = self._post_once(payload)
            if reply.ok or not retryable or attempt == 2:
                return reply
            time.sleep(RETRY_AFTER_SECONDS)
        return reply                      # pragma: no cover - unreachable

    def _post_once(self, payload: dict) -> Tuple[LlmReply, bool]:
        """One attempt. Returns the reply and whether retrying could help."""
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                # The one place the key is used. It is not logged, and it is
                # not put into any error message below.
                "Authorization": "Bearer " + self.api_key,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return self._http_error(exc), exc.code in RETRY_STATUSES
        except urllib.error.URLError as exc:
            return LlmReply(
                False, "", self.model,
                "Could not reach %s (%s)."
                % (self.provider, getattr(exc, "reason", exc))), True
        except TimeoutError:
            return LlmReply(False, "", self.model,
                            "%s timed out after %ds."
                            % (self.provider, self.timeout)), True
        except Exception as exc:          # pragma: no cover - defensive
            return LlmReply(False, "", self.model,
                            "The model call failed: %s" % exc), False

        try:
            text = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            return LlmReply(False, "", self.model,
                            "%s returned a response in an unexpected shape."
                            % self.provider), False

        if not text:
            return LlmReply(False, "", self.model,
                            "%s returned an empty answer." % self.provider), False
        return LlmReply(True, text, self.model), False

    def _http_error(self, exc: urllib.error.HTTPError) -> LlmReply:
        """Turn a status code into something a person can act on."""
        detail = ""
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            pass
        try:
            body = json.loads(raw.decode("utf-8", "ignore"))
            detail = str((body.get("error") or {}).get("message", ""))[:200]
        except Exception:
            # Not JSON. A content-delivery network that blocks the request
            # answers with HTML, and saying so is more use than saying
            # nothing: it points at the request rather than at the key.
            if raw.lstrip()[:1] in (b"<",):
                detail = ("The response was a web page, not JSON, so the "
                          "request was most likely blocked before it reached "
                          "the API.")

        if exc.code == 401:
            message = ("%s rejected the API key. Check LLM_API_KEY."
                       % self.provider)
        elif exc.code == 404:
            message = ("%s has no model called '%s', or this key cannot "
                       "reach it. Set LLM_MODEL to one the provider lists as "
                       "available on your plan."
                       % (self.provider, self.model))
        elif exc.code == 403:
            message = ("%s refused the request (HTTP 403). The key may lack "
                       "access to this model, or the request was blocked "
                       "before it arrived." % self.provider)
        elif exc.code == 429:
            message = ("%s is rate-limiting this key. Try again shortly."
                       % self.provider)
        elif exc.code >= 500:
            message = "%s is having trouble (HTTP %d)." % (self.provider,
                                                           exc.code)
        else:
            message = "%s returned HTTP %d." % (self.provider, exc.code)

        if detail:
            message += " " + detail
        return LlmReply(False, "", self.model, message)
