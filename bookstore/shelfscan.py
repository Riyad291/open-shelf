"""Shelf Scan -- the team's creative feature (feature 14).

The idea
--------
A customer photographs their own bookshelf, or a shelf in the shop, and uploads
it.  The system segments the photo into individual books, matches each one
against the catalogue's cover index, and then does three things no other part of
the app does:

1.  **Tells them what it recognised** -- which of the books on that shelf the
    shop carries, with a confidence per book.
2.  **Builds a reading profile from a photograph.**  A brand-new customer has no
    browsing history, so requirement 2's personalised recommendations have
    nothing to work from.  One photo of their shelf solves the cold-start
    problem instantly: the books they already own are a far stronger taste
    signal than a few clicks.
3.  **Recommends what to read next** from that profile, and flags anything on the
    shelf the shop would buy back.

Why this is a genuine addition rather than a reskin of picture search: picture
search answers "which book is *this*?" for one image.  Shelf Scan answers "who
is this reader?" from many books at once, and feeds the answer back into the
recommender.  It reuses the detection stack the spec already demands (YOLO) and
the cover descriptors from ``imagesearch``, so it adds capability without adding
dependencies.

Segmentation
------------
Two routes, chosen automatically:

*   **YOLO** (``ultralytics``) when a general COCO model is available -- COCO
    class 73 is literally "book", so this gives proper boxes.
*   **Vertical-edge projection** otherwise, with no model at all.  Spines on a
    shelf are separated by strong vertical edges, so we take the column-wise
    sum of the horizontal gradient, smooth it, and cut at the peaks.  For
    face-out covers the same projection finds the gaps between covers.  This is
    classical CV and runs in milliseconds.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

try:  # pragma: no cover
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore

try:  # pragma: no cover
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    Image = None  # type: ignore
    ImageOps = None  # type: ignore

from . import imagesearch
from .imagesearch import ImageIndex
from .recommend import Recommendation, Recommender
from .ports import RatingRules

#: Minimum width of a detected region, as a fraction of image width.  Narrower
#: slivers are noise, not books.
MIN_REGION_WIDTH_RATIO = 0.035
MAX_REGIONS = 24
WORK_WIDTH = 900


@dataclass
class ShelfMatch:
    region: Tuple[int, int, int, int]      # x, y, w, h in the original image
    book: Optional[dict]
    similarity: float
    matched_image: str = ""

    @property
    def recognised(self) -> bool:
        return self.book is not None


@dataclass
class ShelfScanResult:
    ok: bool
    message: str
    backend: str = ""
    regions_found: int = 0
    matches: List[ShelfMatch] = field(default_factory=list)
    recommendations: List[Recommendation] = field(default_factory=list)
    profile_categories: List[Tuple[str, int]] = field(default_factory=list)
    buyback_candidates: List[dict] = field(default_factory=list)

    @property
    def recognised(self) -> List[ShelfMatch]:
        return [m for m in self.matches if m.recognised]

    def summary(self) -> str:
        """Human-readable report, printed straight into the GUI."""
        if not self.ok:
            return self.message

        lines = [
            "SHELF SCAN",
            "Segmentation: %s" % self.backend,
            "Found %d book-shaped region(s); recognised %d from our catalogue."
            % (self.regions_found, len(self.recognised)),
            "",
        ]

        if self.recognised:
            lines.append("Recognised on your shelf:")
            for match in self.recognised:
                book = match.book or {}
                lines.append(
                    "  %-36s %-22s  %.0f%% confident"
                    % (str(book.get("title"))[:36], str(book.get("author"))[:22],
                       match.similarity * 100)
                )
            lines.append("")
        else:
            lines.append(
                "None of the books on this shelf are in our catalogue -- or the "
                "photo is too small or too dim to match. Try a straight-on shot "
                "in good light, filling the frame with one shelf."
            )
            lines.append("")

        if self.profile_categories:
            profile = ", ".join(
                "%s x%d" % (category, count) for category, count in self.profile_categories
            )
            lines.append("Your shelf reads as: %s" % profile)
            lines.append("")

        if self.recommendations:
            lines.append("Based on that shelf, you might like:")
            for i, rec in enumerate(self.recommendations, 1):
                lines.append(
                    "  %d. %s by %s -- $%.2f"
                    % (i, rec.book.get("title"), rec.book.get("author"),
                       float(rec.book.get("price", 0.0)))
                )
                lines.append("     because %s" % rec.reason)
            lines.append("")

        if self.buyback_candidates:
            lines.append(
                "We would buy these back from you (they are on our to-order list):"
            )
            for book in self.buyback_candidates:
                lines.append("  %s by %s" % (book.get("title"), book.get("author")))

        return "\n".join(lines)


class ShelfScanner:
    def __init__(self, image_index: ImageIndex, recommender: Recommender,
                 rules: RatingRules, models_dir: Optional[str] = None,
                 config=None):
        self.index = image_index
        self.recommender = recommender
        self.rules = rules
        # Take the app's own Config rather than loading a second one, so a
        # threshold the super-user changes at runtime is actually honoured.
        self.config = config if config is not None else rules.cfg
        self.models_dir = models_dir or os.path.join(
            self.index.db.data_dir, "models"
        )
        self._yolo = None
        self._yolo_tried = False

    # -- availability -------------------------------------------------------

    @staticmethod
    def available() -> bool:
        return imagesearch.available()

    @staticmethod
    def dependency_message() -> str:
        return imagesearch.missing_dependency_message() or ""

    # -- YOLO ---------------------------------------------------------------

    def _load_yolo(self):
        """Load a general-purpose YOLO model if one is present."""
        if self._yolo_tried:
            return self._yolo
        self._yolo_tried = True
        for name in ("yolov8n.pt", "yolov8s.pt", "yolo11n.pt"):
            path = os.path.join(self.models_dir, name)
            if not os.path.exists(path):
                continue
            try:
                from ultralytics import YOLO  # type: ignore
                self._yolo = YOLO(path)
                return self._yolo
            except Exception:
                self._yolo = None
        return self._yolo

    def _detect_yolo(self, pil_image) -> List[Tuple[int, int, int, int]]:
        model = self._load_yolo()
        if model is None:
            return []
        try:
            results = model.predict(np.asarray(pil_image), verbose=False)
        except Exception:
            return []
        boxes: List[Tuple[int, int, int, int]] = []
        for result in results:
            names = getattr(result, "names", {}) or {}
            for box in getattr(result, "boxes", []) or []:
                try:
                    cls = int(box.cls[0])
                except Exception:
                    continue
                label = str(names.get(cls, "")).lower()
                if label and label != "book":
                    continue
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
                if x2 > x1 and y2 > y1:
                    boxes.append((x1, y1, x2 - x1, y2 - y1))
        return boxes

    # -- classical segmentation --------------------------------------------

    #: Typical width/height of a face-out book cover.  Used to estimate how
    #: many books a wide block of detail contains.
    COVER_ASPECT = 0.66
    #: Typical width/height of a spine-out book.
    SPINE_ASPECT = 0.12

    @staticmethod
    def _column_detail(gray) -> "np.ndarray":
        """Per-column 'how much is going on here' profile.

        Shelf backing and the gaps between books are flat, so their columns
        have low vertical variance *and* low gradient energy.  A printed cover
        or spine has both.  Combining the two cues is more robust than either
        alone on a dim photo.
        """
        variance = gray.std(axis=0)
        grad = np.abs(np.diff(gray, axis=0)).mean(axis=0)
        grad = np.append(grad, grad[-1] if grad.size else 0.0)[: variance.size]
        detail = 0.6 * variance + 0.4 * grad
        # Smooth lightly so a single flat row inside a cover does not cut it.
        window = max(3, gray.shape[1] // 120)
        kernel = np.ones(window, dtype=np.float32) / window
        return np.convolve(detail, kernel, mode="same")

    @classmethod
    def _foreground_blocks(cls, gray) -> List[Tuple[int, int]]:
        """Contiguous column ranges that contain a book rather than background."""
        detail = cls._column_detail(gray)
        if detail.size == 0:
            return []

        # Otsu-style split of the detail profile into background and foreground.
        low, high = float(detail.min()), float(detail.max())
        if high - low < 1e-4:
            return [(0, gray.shape[1])]
        best_threshold, best_variance = low, -1.0
        for step in range(1, 32):
            candidate = low + (high - low) * step / 32.0
            background = detail[detail <= candidate]
            foreground = detail[detail > candidate]
            if background.size < 2 or foreground.size < 2:
                continue
            weight = background.size * foreground.size
            between = weight * (float(background.mean()) - float(foreground.mean())) ** 2
            if between > best_variance:
                best_variance, best_threshold = between, candidate

        mask = detail > best_threshold
        # Close one-column holes so speckle does not fragment a cover.
        for i in range(1, mask.size - 1):
            if not mask[i] and mask[i - 1] and mask[i + 1]:
                mask[i] = True

        blocks: List[Tuple[int, int]] = []
        start = None
        for i, on in enumerate(mask):
            if on and start is None:
                start = i
            elif not on and start is not None:
                blocks.append((start, i))
                start = None
        if start is not None:
            blocks.append((start, mask.size))

        min_width = max(int(gray.shape[1] * MIN_REGION_WIDTH_RATIO), 6)
        blocks = [(a, b) for a, b in blocks if (b - a) >= min_width]
        return blocks or [(0, gray.shape[1])]

    @classmethod
    def _split_block(cls, gray, left: int, right: int) -> List[Tuple[int, int]]:
        """Subdivide one block of detail into individual books.

        The number of books is estimated from the block's aspect ratio against
        a typical cover, then the strongest internal vertical edges are used as
        the cuts.  Estimating the *count* first is what stops cover artwork from
        being mistaken for a book boundary: we only ever take the ``n - 1``
        strongest candidates, however many edges the artwork contains.
        """
        height = gray.shape[0]
        width = right - left
        if width <= 0 or height <= 0:
            return []

        expected_cover = max(1.0, cls.COVER_ASPECT * height)
        expected_spine = max(1.0, cls.SPINE_ASPECT * height)

        # Decide whether this block looks face-out or spine-out, then estimate
        # how many books it holds.
        if width <= expected_cover * 1.35:
            return [(left, right)]            # one face-out cover
        count_cover = int(round(width / expected_cover))
        if width / height < 0.35:
            count = max(1, int(round(width / expected_spine)))
        else:
            count = max(1, count_cover)
        count = min(count, MAX_REGIONS)
        if count <= 1:
            return [(left, right)]

        # Candidate cuts: local maxima of the horizontal gradient inside the
        # block, ranked by strength.
        block = gray[:, left:right]
        grad = np.abs(np.diff(block, axis=1)).sum(axis=0)
        if grad.size == 0:
            return [(left, right)]
        window = max(3, width // 60)
        kernel = np.ones(window, dtype=np.float32) / window
        smooth = np.convolve(grad, kernel, mode="same")

        min_gap = max(int(expected_cover * 0.45), 6)
        candidates = []
        for x in range(1, smooth.size - 1):
            if smooth[x] >= smooth[x - 1] and smooth[x] >= smooth[x + 1]:
                candidates.append((float(smooth[x]), x))
        candidates.sort(reverse=True)

        chosen: List[int] = []
        for _, x in candidates:
            if len(chosen) >= count - 1:
                break
            if all(abs(x - c) >= min_gap for c in chosen) and \
                    min(x, width - x) >= min_gap // 2:
                chosen.append(x)

        if not chosen:
            # No usable edge: fall back to an even split, which is still better
            # than handing one wide strip to the matcher.
            step = width / float(count)
            return [
                (left + int(i * step), left + int((i + 1) * step))
                for i in range(count)
            ]

        cuts = [0] + sorted(chosen) + [width]
        return [(left + a, left + b) for a, b in zip(cuts, cuts[1:]) if b > a]

    @classmethod
    def _detect_by_edges(cls, pil_image) -> List[Tuple[int, int, int, int]]:
        """Segment a shelf photo into individual books without any model.

        Two stages: separate books from shelf background by per-column detail,
        then subdivide each block of detail using the expected cover aspect
        ratio.  Works for face-out covers and for packed spines, and treats a
        plain single-book photo as one region.
        """
        gray = np.asarray(pil_image.convert("L"), dtype=np.float32) / 255.0
        height, width = gray.shape
        if width < 40 or height < 20:
            return [(0, 0, width, height)]

        regions: List[Tuple[int, int, int, int]] = []
        for left, right in cls._foreground_blocks(gray):
            for a, b in cls._split_block(gray, left, right):
                if b - a >= max(int(width * MIN_REGION_WIDTH_RATIO), 6):
                    regions.append((a, 0, b - a, height))

        if not regions:
            return [(0, 0, width, height)]
        return regions[:MAX_REGIONS]

    # -- main entry point ---------------------------------------------------

    def scan(self, image_path: str, user: Optional[dict] = None,
             recommend_count: int = 3) -> ShelfScanResult:
        if not self.available():
            return ShelfScanResult(False, self.dependency_message()
                                   or "Shelf Scan needs numpy and Pillow.")
        if not image_path or not os.path.exists(image_path):
            return ShelfScanResult(False, "Could not find that image file.")

        try:
            original = Image.open(image_path)
            original = ImageOps.exif_transpose(original).convert("RGB")
        except Exception:
            return ShelfScanResult(False, "Could not read that image.")

        # Work at a fixed width so the segmentation thresholds behave
        # consistently regardless of camera resolution.
        scale = 1.0
        work = original
        if original.width > WORK_WIDTH:
            scale = WORK_WIDTH / float(original.width)
            work = original.resize(
                (WORK_WIDTH, max(1, int(original.height * scale))), Image.BILINEAR
            )

        regions = self._detect_yolo(work)
        backend = "YOLO (ultralytics, COCO 'book' class)"
        if not regions:
            regions = self._detect_by_edges(work)
            backend = "vertical-edge projection (no YOLO weights installed)"

        if not regions:
            return ShelfScanResult(
                False, "Could not find any book-shaped regions in that photo.",
                backend=backend,
            )

        # Make sure the index is current before matching.
        self.index.build()
        if self.index.size == 0:
            return ShelfScanResult(
                False,
                "The cover index is empty, so there is nothing to match against. "
                "Ask the super-user to add books with cover images.",
                backend=backend,
            )

        try:
            threshold = float(
                self.config.get("shelfscan_match_threshold") or 0.55
            )
        except (AttributeError, TypeError, ValueError):
            threshold = 0.55

        matches: List[ShelfMatch] = []
        seen_books = set()
        for (x, y, w, h) in regions[:MAX_REGIONS]:
            crop = work.crop((x, y, x + w, y + h))
            if crop.width < 8 or crop.height < 8:
                continue
            descriptor = imagesearch.describe_pil_image(crop)
            if not descriptor:
                continue
            hits = self.index.match_descriptor(descriptor, limit=1)
            # Map the region back to original-image coordinates.
            inv = 1.0 / scale if scale else 1.0
            orig_region = (int(x * inv), int(y * inv), int(w * inv), int(h * inv))

            if hits and hits[0].similarity >= threshold:
                hit = hits[0]
                book_id = hit.book["book_id"]
                if book_id in seen_books:
                    # The same title detected twice is one book, not two.
                    continue
                seen_books.add(book_id)
                matches.append(
                    ShelfMatch(orig_region, hit.book, hit.similarity, hit.matched_image)
                )
            else:
                best = hits[0].similarity if hits else 0.0
                matches.append(ShelfMatch(orig_region, None, best))

        recognised = [m for m in matches if m.recognised]
        book_ids = [m.book["book_id"] for m in recognised if m.book]

        # Taste profile and recommendations.
        from collections import Counter
        categories: Counter = Counter()
        for match in recognised:
            if match.book:
                categories[(match.book.get("category") or "").lower()] += 1

        recommendations: List[Recommendation] = []
        if book_ids:
            exclude = set(book_ids)
            if user:
                exclude |= {
                    e.get("book_id") for e in (user.get("purchase_history") or [])
                }
            recommendations = self.recommender.similar_to(
                book_ids, count=recommend_count, exclude=exclude
            )

        buyback = []
        for match in recognised:
            if match.book and self.rules.list_status(match.book["book_id"]) == "to-order":
                buyback.append(match.book)

        return ShelfScanResult(
            ok=True,
            message="Scanned %d region(s)." % len(matches),
            backend=backend,
            regions_found=len(matches),
            matches=matches,
            recommendations=recommendations,
            profile_categories=categories.most_common(),
            buyback_candidates=buyback,
        )

    # -- annotated preview --------------------------------------------------

    def annotate(self, image_path: str, result: ShelfScanResult,
                 out_path: str) -> bool:
        """Draw the detected regions and matched titles onto a copy of the
        photo, so the GUI can show the user what the scanner saw."""
        if not self.available() or not result.ok:
            return False
        try:
            from PIL import ImageDraw, ImageFont
            img = ImageOps.exif_transpose(Image.open(image_path)).convert("RGB")
        except Exception:
            return False

        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None

        for match in result.matches:
            x, y, w, h = match.region
            colour = (64, 200, 120) if match.recognised else (210, 80, 80)
            width = max(2, img.width // 400)
            draw.rectangle([x, y, x + w, y + h], outline=colour, width=width)
            label = (
                "%s (%.0f%%)" % (str(match.book.get("title"))[:24], match.similarity * 100)
                if match.recognised else "no match"
            )
            text_y = max(0, y + 4)
            draw.rectangle([x, text_y, x + min(w, 210), text_y + 14], fill=colour)
            if font is not None:
                draw.text((x + 3, text_y + 2), label, fill=(16, 16, 16), font=font)

        try:
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            img.save(out_path)
            return True
        except Exception:
            return False
