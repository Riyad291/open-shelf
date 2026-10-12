"""Configuration loading.

All tunable business rules live in ``config/settings.json`` so that a grader or
teammate can change a threshold without touching code.  ``Config`` is a thin
read/write wrapper around that file.

The super-user can edit the taboo word list at runtime (spec requirement 9), so
this object supports saving as well as loading.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Paths.  Everything is resolved relative to the project root so the app can be
# launched from any working directory.
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")

DATA_DIR = os.path.join(PROJECT_ROOT, "data")
KB_DIR = os.path.join(DATA_DIR, "kb")
COVERS_DIR = os.path.join(DATA_DIR, "covers")
BOOK_TEXT_DIR = os.path.join(DATA_DIR, "books")
FIGURES_DIR = os.path.join(DATA_DIR, "figures")
BIOMETRICS_DIR = os.path.join(DATA_DIR, "biometrics")
FACES_DIR = os.path.join(BIOMETRICS_DIR, "faces")
VOICES_DIR = os.path.join(BIOMETRICS_DIR, "voices")

USERS_PATH = os.path.join(DATA_DIR, "users.json")
BOOKS_PATH = os.path.join(DATA_DIR, "books.json")
REVIEWS_PATH = os.path.join(DATA_DIR, "reviews.json")
TRANSACTIONS_PATH = os.path.join(DATA_DIR, "transactions.json")
EVENTS_PATH = os.path.join(DATA_DIR, "events.json")

ALL_DIRS = [
    CONFIG_DIR,
    DATA_DIR,
    KB_DIR,
    COVERS_DIR,
    BOOK_TEXT_DIR,
    FIGURES_DIR,
    BIOMETRICS_DIR,
    FACES_DIR,
    VOICES_DIR,
]


def ensure_dirs() -> None:
    """Create every directory the app expects.  Safe to call repeatedly."""
    for path in ALL_DIRS:
        os.makedirs(path, exist_ok=True)


# ---------------------------------------------------------------------------
# Defaults.  Used when settings.json is missing a key, so an old config file
# never crashes a newer build.
# ---------------------------------------------------------------------------

DEFAULTS: Dict[str, Any] = {
    "fine_amount": 1.0,
    "vip_discount": 0.10,
    "vip_purchase_threshold": 3,
    "vip_spend_threshold": 200.0,
    "warning_limit": 2,
    # Requirement 4: a face is the login. Creating an account enrols one,
    # and an enrolled account signs in with the camera rather than a
    # password. The super-user keeps password access because somebody has
    # to be able to get in before any face exists.
    "require_face_enrolment": True,
    "require_face_login": True,
    "super_user_password_bootstrap": True,
    "face_enrolment_samples": 5,
    "purchaser_weight": 2.0,
    "normal_weight": 1.0,
    "min_ratings_for_list": 3,
    "no_order_max_avg": 2.0,
    "to_order_min_avg": 4.0,
    "max_taboo_before_block": 3,
    "spec_interpretation": {
        "allow_overdraft_purchase": False,
        "demotion_blocks_repromotion": True,
        "order_lists_use_weighted_average": True,
        "visitors_may_review": True,
    },
    "rag_min_similarity": 0.15,
    "rag_top_k": 3,
    "ollama_url": "http://localhost:11434",
    "ollama_model": "llama3.2",
    "ollama_timeout_seconds": 60,
    "recommendation_count": 3,
    "image_search_results": 8,
    "shelfscan_match_threshold": 0.55,
    "taboo_words": [],
}


class Config:
    """Mutable, file-backed settings object."""

    def __init__(self, path: str = SETTINGS_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._values: Dict[str, Any] = {}
        self.reload()

    # -- loading / saving ---------------------------------------------------

    def reload(self) -> None:
        with self._lock:
            values = dict(DEFAULTS)
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as handle:
                        on_disk = json.load(handle)
                    if isinstance(on_disk, dict):
                        # Merge the nested interpretation block rather than
                        # letting a partial file wipe the defaults.
                        nested = dict(DEFAULTS["spec_interpretation"])
                        nested.update(on_disk.get("spec_interpretation") or {})
                        values.update(on_disk)
                        values["spec_interpretation"] = nested
                except (OSError, ValueError):
                    # A corrupt settings file should not stop the store from
                    # opening; fall back to defaults and carry on.
                    pass
            self._values = values

    def save(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            payload = {k: v for k, v in self._values.items() if not k.startswith("_")}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)

    # -- access -------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key in self._values:
                return self._values[key]
            if key in DEFAULTS:
                return DEFAULTS[key]
            return default

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._values[key] = value

    def flag(self, name: str) -> bool:
        """Read one of the spec-interpretation switches."""
        block = self.get("spec_interpretation") or {}
        if name in block:
            return bool(block[name])
        return bool(DEFAULTS["spec_interpretation"].get(name, False))

    # -- taboo words (edited by the SU at runtime) --------------------------

    @property
    def taboo_words(self) -> List[str]:
        words = self.get("taboo_words") or []
        return [str(w).strip().lower() for w in words if str(w).strip()]

    def set_taboo_words(self, words: List[str]) -> None:
        cleaned: List[str] = []
        seen = set()
        for word in words:
            token = str(word).strip().lower()
            if token and token not in seen:
                seen.add(token)
                cleaned.append(token)
        self.set("taboo_words", cleaned)
        self.save()

    def add_taboo_word(self, word: str) -> bool:
        token = str(word).strip().lower()
        if not token or token in self.taboo_words:
            return False
        self.set_taboo_words(self.taboo_words + [token])
        return True

    def remove_taboo_word(self, word: str) -> bool:
        token = str(word).strip().lower()
        current = self.taboo_words
        if token not in current:
            return False
        self.set_taboo_words([w for w in current if w != token])
        return True

    # -- convenience accessors used all over the business rules -------------

    @property
    def fine_amount(self) -> float:
        return float(self.get("fine_amount"))

    @property
    def vip_discount(self) -> float:
        return float(self.get("vip_discount"))

    @property
    def vip_purchase_threshold(self) -> int:
        return int(self.get("vip_purchase_threshold"))

    @property
    def vip_spend_threshold(self) -> float:
        return float(self.get("vip_spend_threshold"))

    @property
    def warning_limit(self) -> int:
        return int(self.get("warning_limit"))

    @property
    def purchaser_weight(self) -> float:
        return float(self.get("purchaser_weight"))

    @property
    def normal_weight(self) -> float:
        return float(self.get("normal_weight"))

    @property
    def min_ratings_for_list(self) -> int:
        return int(self.get("min_ratings_for_list"))

    @property
    def no_order_max_avg(self) -> float:
        return float(self.get("no_order_max_avg"))

    @property
    def to_order_min_avg(self) -> float:
        return float(self.get("to_order_min_avg"))

    @property
    def max_taboo_before_block(self) -> int:
        return int(self.get("max_taboo_before_block"))

    @property
    def recommendation_count(self) -> int:
        return int(self.get("recommendation_count"))


# A single shared instance is convenient for the GUI; tests build their own.
_shared: Config | None = None


def shared_config() -> Config:
    global _shared
    if _shared is None:
        _shared = Config()
    return _shared
