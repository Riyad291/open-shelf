"""Search books by picture (spec requirement 5: "search by ... pictures
(cover page or internal figures)").

How it works
------------
Every indexed image is reduced to a fixed-length descriptor built from three
complementary cues, so the match is not fooled by one of them alone:

1.  **Colour layout** -- an HSV histogram computed per cell of a 3x3 grid, so
    the *spatial* arrangement of colour matters, not just the palette.
2.  **Perceptual hash** -- a 16x16 difference hash over the greyscale image,
    which survives rescaling and mild JPEG noise and captures structure.
3.  **Edge density** -- the mean gradient magnitude per grid cell, which
    separates a text-heavy title page from a photographic cover.

Descriptors are L2-normalised per block and compared with a weighted cosine
similarity.  The index is cached to ``data/image_index.json`` and keyed by file
mtime and size, so adding one book does not re-descriptor the whole shelf.

Dependencies: Pillow and numpy.  Both are in requirements.txt; if either is
missing, :func:`available` returns False and the GUI hides the picture-search
tab instead of crashing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

try:  # pragma: no cover - import guard
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore

try:  # pragma: no cover - import guard
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    Image = None  # type: ignore
    ImageOps = None  # type: ignore

from .storage import Storage

GRID = 3
HUE_BINS = 8
SAT_BINS = 3
VAL_BINS = 3
PHASH_SIZE = 16
WORK_SIZE = 192

W_COLOR = 0.45
W_PHASH = 0.40
W_EDGE = 0.15


def available() -> bool:
    """True when the optional imaging dependencies are importable."""
    return np is not None and Image is not None


def missing_dependency_message() -> str:
    missing = []
    if np is None:
        missing.append("numpy")
    if Image is None:
        missing.append("Pillow")
    if not missing:
        return ""
    return (
        "Picture search needs %s. Install with:  pip install %s"
        % (" and ".join(missing), " ".join(missing))
    )


@dataclass
class ImageHit:
    book: dict
    similarity: float
    matched_image: str
    kind: str  # "cover" or "figure"


# ---------------------------------------------------------------------------
# Descriptor
# ---------------------------------------------------------------------------


def _load_rgb(path: str):
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB").resize((WORK_SIZE, WORK_SIZE), Image.BILINEAR)
    return img


def _normalise(vec):
    norm = float(np.linalg.norm(vec))
    if norm <= 1e-9:
        return vec
    return vec / norm


def _color_block(img) -> "np.ndarray":
    hsv = np.asarray(img.convert("HSV"), dtype=np.float32) / 255.0
    cell = WORK_SIZE // GRID
    features: List[float] = []
    for gy in range(GRID):
        for gx in range(GRID):
            patch = hsv[gy * cell:(gy + 1) * cell, gx * cell:(gx + 1) * cell]
            h = np.clip((patch[:, :, 0] * HUE_BINS).astype(int), 0, HUE_BINS - 1)
            s = np.clip((patch[:, :, 1] * SAT_BINS).astype(int), 0, SAT_BINS - 1)
            v = np.clip((patch[:, :, 2] * VAL_BINS).astype(int), 0, VAL_BINS - 1)
            flat = (h * SAT_BINS + s) * VAL_BINS + v
            hist = np.bincount(flat.ravel(), minlength=HUE_BINS * SAT_BINS * VAL_BINS)
            hist = hist.astype(np.float32)
            total = hist.sum()
            if total:
                hist /= total
            features.extend(hist.tolist())
    return _normalise(np.asarray(features, dtype=np.float32))


def _phash_block(img) -> "np.ndarray":
    small = img.convert("L").resize((PHASH_SIZE + 1, PHASH_SIZE), Image.BILINEAR)
    arr = np.asarray(small, dtype=np.float32)
    # Difference hash: compare each pixel with its right-hand neighbour.
    bits = (arr[:, 1:] > arr[:, :-1]).astype(np.float32).ravel()
    # Map {0,1} to {-1,+1} so cosine similarity behaves like Hamming distance.
    return _normalise(bits * 2.0 - 1.0)


def _edge_block(img) -> "np.ndarray":
    gray = np.asarray(img.convert("L"), dtype=np.float32) / 255.0
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:-1] = gray[:, 2:] - gray[:, :-2]
    gy[1:-1, :] = gray[2:, :] - gray[:-2, :]
    mag = np.sqrt(gx * gx + gy * gy)
    cell = WORK_SIZE // GRID
    features: List[float] = []
    for gy_i in range(GRID):
        for gx_i in range(GRID):
            patch = mag[gy_i * cell:(gy_i + 1) * cell, gx_i * cell:(gx_i + 1) * cell]
            features.append(float(patch.mean()))
    return _normalise(np.asarray(features, dtype=np.float32))


def describe(path: str) -> Optional[Dict[str, List[float]]]:
    """Build the three-block descriptor for one image file."""
    if not available() or not path or not os.path.exists(path):
        return None
    try:
        img = _load_rgb(path)
    except Exception:
        return None
    return {
        "color": _color_block(img).tolist(),
        "phash": _phash_block(img).tolist(),
        "edge": _edge_block(img).tolist(),
    }


def similarity(a: Dict[str, Sequence[float]], b: Dict[str, Sequence[float]]) -> float:
    """Weighted cosine similarity across the three blocks, mapped to 0..1."""
    if not a or not b:
        return 0.0
    total = 0.0
    for key, weight in (("color", W_COLOR), ("phash", W_PHASH), ("edge", W_EDGE)):
        va = np.asarray(a.get(key) or [], dtype=np.float32)
        vb = np.asarray(b.get(key) or [], dtype=np.float32)
        if va.size == 0 or va.size != vb.size:
            continue
        total += weight * float(np.dot(va, vb))
    # phash cosine ranges over [-1, 1]; clamp the blend into [0, 1].
    return max(0.0, min(1.0, total))


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class ImageIndex:
    """Descriptor cache over every cover and figure in the catalogue."""

    def __init__(self, storage: Storage):
        self.db = storage
        self.cache_path = os.path.join(self.db.data_dir, "image_index.json")
        self._entries: Dict[str, dict] = {}
        self._load_cache()

    # -- cache --------------------------------------------------------------

    def _load_cache(self) -> None:
        if not os.path.exists(self.cache_path):
            return
        try:
            with open(self.cache_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._entries = data.get("entries") or {}
        except (OSError, ValueError):
            self._entries = {}

    def _save_cache(self) -> None:
        try:
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"entries": self._entries}, handle)
            os.replace(tmp, self.cache_path)
        except OSError:
            pass

    @staticmethod
    def _stamp(path: str) -> str:
        try:
            stat = os.stat(path)
            return "%d:%d" % (int(stat.st_mtime), stat.st_size)
        except OSError:
            return ""

    # -- building -----------------------------------------------------------

    def _image_records(self) -> List[Tuple[str, str, str]]:
        """Every (book_id, kind, absolute path) the index should cover."""
        records: List[Tuple[str, str, str]] = []
        for book in self.db.list_books():
            cover = self._resolve(book.get("cover_path"))
            if cover:
                records.append((book["book_id"], "cover", cover))
            for figure in book.get("figure_paths") or []:
                resolved = self._resolve(figure)
                if resolved:
                    records.append((book["book_id"], "figure", resolved))
        return records

    def _resolve(self, path: Optional[str]) -> str:
        if not path:
            return ""
        if os.path.isabs(path) and os.path.exists(path):
            return path
        project_root = os.path.dirname(self.db.data_dir)
        for candidate in (
            os.path.join(project_root, path),
            os.path.join(self.db.data_dir, path),
            os.path.join(self.db.covers_dir, os.path.basename(path)),
            os.path.join(self.db.figures_dir, os.path.basename(path)),
        ):
            if os.path.exists(candidate):
                return candidate
        return ""

    def build(self, force: bool = False, progress=None) -> int:
        """Describe any image that is new or changed.  Returns images indexed."""
        if not available():
            return 0
        if force:
            self._entries = {}

        records = self._image_records()
        done = 0
        for i, (book_id, kind, path) in enumerate(records):
            stamp = self._stamp(path)
            existing = self._entries.get(path)
            if existing and existing.get("stamp") == stamp and not force:
                continue
            descriptor = describe(path)
            if descriptor is None:
                continue
            self._entries[path] = {
                "book_id": book_id,
                "kind": kind,
                "stamp": stamp,
                "descriptor": descriptor,
            }
            done += 1
            if progress:
                progress(i + 1, len(records))

        # Forget images that have been removed from the catalogue.
        live = {path for _, _, path in records}
        for stale in set(self._entries) - live:
            del self._entries[stale]

        if done or force:
            self._save_cache()
        return done

    @property
    def size(self) -> int:
        return len(self._entries)

    # -- querying -----------------------------------------------------------

    def search(self, query_path: str, limit: int = 8,
               min_similarity: float = 0.0) -> List[ImageHit]:
        """Rank books by how well any of their images matches the query."""
        if not available():
            return []
        query = describe(query_path)
        if query is None:
            return []
        self.build()

        best_per_book: Dict[str, ImageHit] = {}
        for path, entry in self._entries.items():
            score = similarity(query, entry.get("descriptor") or {})
            if score < min_similarity:
                continue
            book = self.db.get_book(entry.get("book_id", ""))
            if not book:
                continue
            current = best_per_book.get(book["book_id"])
            if current is None or score > current.similarity:
                best_per_book[book["book_id"]] = ImageHit(
                    book=book,
                    similarity=round(score, 4),
                    matched_image=path,
                    kind=entry.get("kind", "cover"),
                )

        hits = sorted(best_per_book.values(), key=lambda h: -h.similarity)
        return hits[:limit]

    def match_descriptor(self, descriptor: Dict[str, Sequence[float]],
                         limit: int = 1) -> List[ImageHit]:
        """Match an already-computed descriptor (used by Shelf Scan crops)."""
        if not available() or not descriptor:
            return []
        hits: List[ImageHit] = []
        for path, entry in self._entries.items():
            score = similarity(descriptor, entry.get("descriptor") or {})
            book = self.db.get_book(entry.get("book_id", ""))
            if not book:
                continue
            hits.append(
                ImageHit(book=book, similarity=round(score, 4),
                         matched_image=path, kind=entry.get("kind", "cover"))
            )
        hits.sort(key=lambda h: -h.similarity)
        return hits[:limit]


def describe_pil_image(img) -> Optional[Dict[str, List[float]]]:
    """Descriptor for an in-memory PIL image (Shelf Scan crops)."""
    if not available() or img is None:
        return None
    try:
        work = img.convert("RGB").resize((WORK_SIZE, WORK_SIZE), Image.BILINEAR)
    except Exception:
        return None
    return {
        "color": _color_block(work).tolist(),
        "phash": _phash_block(work).tolist(),
        "edge": _edge_block(work).tolist(),
    }
