"""Ollama client (spec note 2: "using an Ollama-based free one is more than
enough").

Only ``urllib`` from the standard library is used, so there is no ``requests``
dependency.  Every failure mode -- Ollama not installed, not running, model not
pulled, request timed out -- returns a readable message instead of raising, so
the chat box always says something useful.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .config import Config


@dataclass
class LlmReply:
    ok: bool
    text: str
    model: str = ""
    error: str = ""


class OllamaClient:
    def __init__(self, config: Config):
        self.cfg = config

    # -- plumbing -----------------------------------------------------------

    @property
    def base_url(self) -> str:
        return str(self.cfg.get("ollama_url")).rstrip("/")

    @property
    def model(self) -> str:
        return str(self.cfg.get("ollama_model"))

    @property
    def timeout(self) -> int:
        return int(self.cfg.get("ollama_timeout_seconds") or 60)

    def _post(self, path: str, payload: dict, timeout: Optional[int] = None) -> dict:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _get(self, path: str, timeout: int = 5) -> dict:
        request = urllib.request.Request(self.base_url + path, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    # -- health -------------------------------------------------------------

    def is_up(self, timeout: int = 3) -> bool:
        try:
            self._get("/api/tags", timeout=timeout)
            return True
        except Exception:
            return False

    def available_models(self) -> List[str]:
        try:
            payload = self._get("/api/tags")
        except Exception:
            return []
        return sorted(
            str(entry.get("name", "")) for entry in (payload.get("models") or [])
            if entry.get("name")
        )

    def status(self) -> Tuple[bool, str]:
        """Human-readable health line for the chat panel and admin page."""
        if not self.is_up():
            return False, (
                "Ollama is not reachable at %s. Start it with 'ollama serve', "
                "then pull a model with 'ollama pull %s'."
                % (self.base_url, self.model)
            )
        models = self.available_models()
        if not models:
            return False, (
                "Ollama is running but has no models. Run:  ollama pull %s" % self.model
            )
        # Ollama tags carry a ':tag' suffix, so match on the bare name too.
        wanted = self.model.split(":")[0]
        if not any(m.split(":")[0] == wanted for m in models):
            return False, (
                "Ollama is running but '%s' is not pulled. Available: %s. "
                "Run:  ollama pull %s" % (self.model, ", ".join(models), self.model)
            )
        return True, "Ollama ready at %s with model '%s'." % (self.base_url, self.model)

    # -- generation ---------------------------------------------------------

    def generate(self, prompt: str, system: str = "",
                 temperature: float = 0.3) -> LlmReply:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system

        try:
            data = self._post("/api/generate", payload)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            if exc.code == 404:
                return LlmReply(
                    False, "", self.model,
                    "Model '%s' is not available in Ollama. Run:  ollama pull %s"
                    % (self.model, self.model),
                )
            return LlmReply(False, "", self.model,
                            "Ollama returned HTTP %s. %s" % (exc.code, detail))
        except urllib.error.URLError as exc:
            return LlmReply(
                False, "", self.model,
                "Could not reach Ollama at %s (%s). Start it with 'ollama serve'."
                % (self.base_url, getattr(exc, "reason", exc)),
            )
        except TimeoutError:
            return LlmReply(False, "", self.model,
                            "Ollama timed out after %ds." % self.timeout)
        except Exception as exc:  # pragma: no cover - defensive
            return LlmReply(False, "", self.model, "Ollama call failed: %s" % exc)

        text = str(data.get("response", "")).strip()
        if not text:
            return LlmReply(False, "", self.model, "Ollama returned an empty response.")
        return LlmReply(True, text, self.model)

    def chat(self, messages: List[dict], temperature: float = 0.3) -> LlmReply:
        """Multi-turn variant, used when the chat panel keeps history."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        try:
            data = self._post("/api/chat", payload)
        except Exception as exc:
            # Fall back to the single-prompt endpoint, which older Ollama
            # builds always have.
            joined = "\n".join(
                "%s: %s" % (m.get("role", "user"), m.get("content", ""))
                for m in messages
            )
            reply = self.generate(joined, temperature=temperature)
            if reply.ok:
                return reply
            return LlmReply(False, "", self.model, str(exc))
        message = (data.get("message") or {}).get("content", "")
        text = str(message).strip()
        if not text:
            return LlmReply(False, "", self.model, "Ollama returned an empty response.")
        return LlmReply(True, text, self.model)
