"""Process-wide objects that are expensive to build and safe to share.

The retriever indexes the knowledge base, the image index describes every
cover, and the face recogniser loads a model. Rebuilding those per request
would make every request pay for them. None of them hold a database session.
"""

from __future__ import annotations

import threading
from typing import Optional

from ..config import Config
from ..storage import Storage


#: Reentrant on purpose. These builders call one another -- retriever(),
#: images() and face_recogniser() each need files() -- so a plain Lock deadlocks
#: the moment one of them runs: the outer call holds it and the inner call
#: waits for it, in the same thread, forever. The symptom was a /api/health
#: request that never returned and a server with an empty log.
_lock = threading.RLock()
_state: dict = {}


def configure(config_path: Optional[str] = None,
              data_dir: Optional[str] = None) -> None:
    with _lock:
        _state.clear()
        _state["config_path"] = config_path
        _state["data_dir"] = data_dir


def config() -> Config:
    with _lock:
        if "config" not in _state:
            path = _state.get("config_path")
            _state["config"] = Config(path) if path else Config()
        return _state["config"]


def files() -> Storage:
    """Storage is no longer the system of record -- the database is. It is
    kept only for the paths it knows: covers, figures, book texts, the
    knowledge base and biometric samples, all of which are files."""
    with _lock:
        if "files" not in _state:
            data_dir = _state.get("data_dir")
            _state["files"] = Storage(data_dir) if data_dir else Storage()
        return _state["files"]


def retriever():
    from ..rag import Retriever
    with _lock:
        if "retriever" not in _state:
            _state["retriever"] = Retriever(files())
        return _state["retriever"]


def llm():
    from ..llm import OllamaClient
    with _lock:
        if "llm" not in _state:
            _state["llm"] = OllamaClient(config())
        return _state["llm"]


def images():
    from ..imagesearch import ImageIndex
    with _lock:
        if "images" not in _state:
            _state["images"] = ImageIndex(files())
        return _state["images"]


def face_recogniser():
    """The process's one face recogniser, built on first use.

    One instance so that the trained eigenface model is loaded from disk
    once, not on every sign-in.
    """
    from ..biometrics import FaceRecogniser
    with _lock:
        if "faces" not in _state:
            _state["faces"] = FaceRecogniser(files())
        return _state["faces"]
