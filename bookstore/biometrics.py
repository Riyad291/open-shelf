"""Face recognition: eigenfaces, written here rather than imported.

A demonstration, not an authentication system. See "What this is not" at the
bottom of this docstring before relying on it for anything.

Face pipeline
-------------
``detect`` -> ``align/normalise`` -> ``PCA (eigenfaces)`` -> ``1-NN + threshold``

*Detection* tries four backends in order and reports which one it used, so the
app works on a machine with no model files at all:

1. **YOLO** via ``ultralytics`` -- used when a YOLO face weight file is present
   (``data/models/yolov8n-face.pt``). The most accurate of the four.
2. **YuNet** -- OpenCV's bundled DNN face detector, when
   ``data/models/face_detection_yunet.onnx`` is present.
3. **Haar cascade** -- classic OpenCV, when a cascade XML is available.
4. **Centre crop** -- no detector at all: assume the subject fills the frame.
   Enrollment and login still function, just less robustly.

*Classification* is deliberately ours rather than ``cv2.face.LBPHFaceRecognizer``,
because the ``face`` module lives in opencv-contrib and was dropped from
OpenCV 5.  Eigenfaces in numpy is the textbook approach, needs no extra wheel,
and is genuinely trained on the enrolled samples: we mean-centre the gallery,
take the top-``k`` principal components by SVD, project every sample, and
classify a probe by nearest neighbour in that subspace with a distance
threshold that lets the model say "I don't know you".

Images arrive as uploads from the browser. The server never opens a camera:
a deployed server has none, and the camera is the visitor's.

Every public function degrades to a clear message rather than an exception when
a dependency is missing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

try:  # pragma: no cover
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore

try:  # pragma: no cover
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore

try:  # pragma: no cover
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore

from .datafiles import DataFiles

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

FACE_SIZE = 96           # normalised face crop is FACE_SIZE x FACE_SIZE
FACE_PCA_COMPONENTS = 24
FACE_MATCH_THRESHOLD = 0.62   # cosine similarity in eigenspace; higher = stricter
FACE_MIN_SAMPLES = 3          # per user, to train anything meaningful


MODELS_DIR_NAME = "models"
YOLO_FACE_WEIGHTS = "yolov8n-face.pt"
YUNET_ONNX = "face_detection_yunet.onnx"
HAAR_XML = "haarcascade_frontalface_default.xml"


@dataclass
class Identification:
    """Result of trying to identify a biometric sample."""

    ok: bool
    user_id: Optional[str] = None
    confidence: float = 0.0
    message: str = ""
    backend: str = ""

    @property
    def identified(self) -> bool:
        return self.ok and self.user_id is not None


@dataclass
class CaptureResult:
    ok: bool
    path: str = ""
    message: str = ""


# ===========================================================================
# Face
# ===========================================================================


class FaceDetector:
    """Face detection with a four-step fallback chain."""

    def __init__(self, models_dir: str):
        self.models_dir = models_dir
        self.backend = "none"
        #: True when the last detect() call found nothing and returned a
        #: centre crop instead of a real detection.
        self.last_was_fallback = False
        self._yolo = None
        self._yunet = None
        self._haar = None
        self._init_backend()

    # -- setup --------------------------------------------------------------

    def _model_path(self, name: str) -> str:
        return os.path.join(self.models_dir, name)

    def _init_backend(self) -> None:
        if self._try_yolo():
            self.backend = "yolo"
        elif self._try_yunet():
            self.backend = "yunet"
        elif self._try_haar():
            self.backend = "haar"
        else:
            self.backend = "centre-crop"

    def _try_yolo(self) -> bool:
        weights = self._model_path(YOLO_FACE_WEIGHTS)
        if not os.path.exists(weights):
            return False
        try:
            from ultralytics import YOLO  # type: ignore
            self._yolo = YOLO(weights)
            return True
        except Exception:
            self._yolo = None
            return False

    def _try_yunet(self) -> bool:
        if cv2 is None or not hasattr(cv2, "FaceDetectorYN"):
            return False
        onnx = self._model_path(YUNET_ONNX)
        if not os.path.exists(onnx):
            return False
        try:
            self._yunet = cv2.FaceDetectorYN.create(onnx, "", (320, 320), 0.7, 0.3, 5000)
            return True
        except Exception:
            self._yunet = None
            return False

    def _try_haar(self) -> bool:
        # OpenCV 5 dropped CascadeClassifier from the main module, so this
        # backend only exists on 4.x.  Check before touching the attribute.
        if cv2 is None or not hasattr(cv2, "CascadeClassifier"):
            return False
        candidates = [self._model_path(HAAR_XML)]
        data_dir = getattr(getattr(cv2, "data", None), "haarcascades", None)
        if data_dir:
            candidates.append(os.path.join(data_dir, HAAR_XML))
        for path in candidates:
            if path and os.path.exists(path):
                try:
                    classifier = cv2.CascadeClassifier(path)
                    if not classifier.empty():
                        self._haar = classifier
                        return True
                except Exception:
                    continue
        return False

    # -- detection ----------------------------------------------------------

    def detect(self, gray_or_bgr) -> List[Tuple[int, int, int, int]]:
        """Return face boxes as ``(x, y, w, h)``, largest first.

        When no backend finds anything we still return a centre box so the
        pipeline keeps working on a machine with no detector model -- but
        ``last_was_fallback`` is set, so callers that care (the live preview,
        which must not tell the user a wall is their face) can tell the
        difference.
        """
        self.last_was_fallback = False
        if np is None:
            self.last_was_fallback = True
            return []
        arr = np.asarray(gray_or_bgr)
        if arr.ndim == 2:
            bgr = np.stack([arr] * 3, axis=-1) if cv2 is None else cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
            gray = arr
        else:
            bgr = arr
            gray = (
                cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
                if cv2 is not None
                else arr.mean(axis=2).astype(np.uint8)
            )

        boxes: List[Tuple[int, int, int, int]] = []
        try:
            if self.backend == "yolo" and self._yolo is not None:
                boxes = self._detect_yolo(bgr)
            elif self.backend == "yunet" and self._yunet is not None:
                boxes = self._detect_yunet(bgr)
            elif self.backend == "haar" and self._haar is not None:
                boxes = self._detect_haar(gray)
        except Exception:
            boxes = []

        if not boxes:
            self.last_was_fallback = True
            boxes = [self._centre_box(gray.shape)]

        boxes.sort(key=lambda b: -(b[2] * b[3]))
        return boxes

    def _detect_yolo(self, bgr) -> List[Tuple[int, int, int, int]]:
        results = self._yolo.predict(bgr, verbose=False)
        out: List[Tuple[int, int, int, int]] = []
        for result in results:
            for box in getattr(result, "boxes", []) or []:
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
                out.append((x1, y1, max(1, x2 - x1), max(1, y2 - y1)))
        return out

    def _detect_yunet(self, bgr) -> List[Tuple[int, int, int, int]]:
        height, width = bgr.shape[:2]
        self._yunet.setInputSize((width, height))
        _, faces = self._yunet.detect(bgr)
        out: List[Tuple[int, int, int, int]] = []
        if faces is None:
            return out
        for face in faces:
            x, y, w, h = [int(v) for v in face[:4]]
            out.append((max(0, x), max(0, y), max(1, w), max(1, h)))
        return out

    def _detect_haar(self, gray) -> List[Tuple[int, int, int, int]]:
        found = self._haar.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                            minSize=(60, 60))
        return [(int(x), int(y), int(w), int(h)) for (x, y, w, h) in found]

    @staticmethod
    def _centre_box(shape) -> Tuple[int, int, int, int]:
        height, width = shape[0], shape[1]
        side = int(min(height, width) * 0.7)
        return ((width - side) // 2, (height - side) // 2, side, side)

    @property
    def description(self) -> str:
        return {
            "yolo": "YOLO (ultralytics)",
            "yunet": "YuNet DNN (OpenCV)",
            "haar": "Haar cascade (OpenCV)",
            "centre-crop": "centre crop (no detector model installed)",
        }.get(self.backend, self.backend)


def _load_gray_array(path: str):
    """Read an image as a greyscale numpy array, via OpenCV or Pillow."""
    if np is None:
        return None
    if cv2 is not None:
        arr = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if arr is not None:
            return arr
    if Image is not None:
        try:
            with Image.open(path) as img:
                return np.asarray(img.convert("L"), dtype=np.uint8)
        except Exception:
            return None
    return None


def _equalise(arr):
    """Histogram equalisation, so lighting differences matter less."""
    if cv2 is not None:
        return cv2.equalizeHist(arr)
    hist, _ = np.histogram(arr.ravel(), bins=256, range=(0, 256))
    cdf = hist.cumsum().astype(np.float32)
    if cdf[-1] <= 0:
        return arr
    cdf = 255.0 * cdf / cdf[-1]
    return np.interp(arr.ravel(), np.arange(256), cdf).reshape(arr.shape).astype(np.uint8)


def _resize_gray(arr, size: int):
    if cv2 is not None:
        return cv2.resize(arr, (size, size), interpolation=cv2.INTER_AREA)
    if Image is not None:
        img = Image.fromarray(arr).resize((size, size), Image.BILINEAR)
        return np.asarray(img, dtype=np.uint8)
    # Nearest-neighbour fallback in pure numpy.
    ys = (np.linspace(0, arr.shape[0] - 1, size)).astype(int)
    xs = (np.linspace(0, arr.shape[1] - 1, size)).astype(int)
    return arr[np.ix_(ys, xs)]


def normalise_face(gray, box: Optional[Tuple[int, int, int, int]] = None):
    """Crop to the face box, equalise, resize, and flatten to a unit vector."""
    if np is None:
        return None
    if box is not None:
        x, y, w, h = box
        # Pad the box by 10% so the crop is not cut at the chin/hairline.
        #
        # 10% is measured, not guessed. A square centre-aligned crop was tried
        # on the theory that resizing a non-square detector box to a square
        # distorts the face differently each time; it scored worse. Measured
        # on one enrolled person and two held-out strangers, owner confidence
        # against the best stranger:
        #
        #     box + 0%  pad      0.979 / 0.830   separation 0.149
        #     box + 10% pad      0.696 / 0.476   separation 0.219  <- this
        #     box + 20% pad      0.454 / 0.464   owner locked out
        #     square, margin 8%  0.728 / 0.582   separation 0.146
        #     square, margin 18% 0.497 / 0.446   owner locked out
        #
        # Separation is what matters, not absolute confidence: a tight crop
        # raises both numbers together and makes strangers harder to reject.
        #
        # Caveat worth keeping in mind: that was measured on synthetic drawn
        # faces, which are far more alike than real ones. It says 10% is not
        # an arbitrary choice; it does not prove an accuracy figure for real
        # faces, and no such figure is claimed anywhere in this project.
        pad_x, pad_y = int(w * 0.1), int(h * 0.1)
        x0 = max(0, x - pad_x)
        y0 = max(0, y - pad_y)
        x1 = min(gray.shape[1], x + w + pad_x)
        y1 = min(gray.shape[0], y + h + pad_y)
        if x1 > x0 and y1 > y0:
            gray = gray[y0:y1, x0:x1]
    if gray.size == 0:
        return None
    crop = _resize_gray(gray, FACE_SIZE)
    crop = _equalise(crop)
    vec = crop.astype(np.float32).ravel()
    vec -= vec.mean()
    norm = float(np.linalg.norm(vec))
    if norm < 1e-6:
        return None
    return vec / norm


class FaceRecogniser:
    """Eigenface gallery trained on the enrolled samples."""

    def __init__(self, files: DataFiles):
        self.files = files
        self.models_dir = os.path.join(self.files.data_dir, MODELS_DIR_NAME)
        os.makedirs(self.models_dir, exist_ok=True)
        self.detector = FaceDetector(self.models_dir) if np is not None else None
        self.model_path = os.path.join(self.files.data_dir, "biometrics", "face_model.json")
        self._mean = None
        self._basis = None
        self._gallery: List[Tuple[str, "np.ndarray"]] = []
        self._load_model()

    # -- availability -------------------------------------------------------

    @staticmethod
    def available() -> bool:
        return np is not None and (cv2 is not None or Image is not None)

    @staticmethod
    def dependency_message() -> str:
        missing = []
        if np is None:
            missing.append("numpy")
        if cv2 is None and Image is None:
            missing.append("opencv-python (or Pillow)")
        if not missing:
            return ""
        return "Face login needs %s. Install with:  pip install %s" % (
            " and ".join(missing), " ".join(missing),
        )

    @property
    def backend_description(self) -> str:
        if self.detector is None:
            return "unavailable"
        return self.detector.description

    @property
    def trained(self) -> bool:
        return self._basis is not None and bool(self._gallery)

    # -- enrollment ---------------------------------------------------------

    def user_dir(self, user_id: str) -> str:
        path = os.path.join(self.files.faces_dir, user_id)
        os.makedirs(path, exist_ok=True)
        return path

    def sample_paths(self, user_id: str) -> List[str]:
        folder = os.path.join(self.files.faces_dir, user_id)
        if not os.path.isdir(folder):
            return []
        return sorted(
            os.path.join(folder, n)
            for n in os.listdir(folder)
            if n.lower().endswith((".png", ".jpg", ".jpeg"))
        )

    def sample_count(self, user_id: str) -> int:
        return len(self.sample_paths(user_id))

    def enrolled_users(self) -> List[str]:
        if not os.path.isdir(self.files.faces_dir):
            return []
        return sorted(
            name for name in os.listdir(self.files.faces_dir)
            if os.path.isdir(os.path.join(self.files.faces_dir, name))
            and self.sample_count(name) > 0
        )

    def add_sample_from_file(self, user_id: str, image_path: str) -> CaptureResult:
        """Enroll one still image for a user."""
        if not self.available():
            return CaptureResult(False, message=self.dependency_message())
        gray = _load_gray_array(image_path)
        if gray is None:
            return CaptureResult(False, message="Could not read that image file.")
        boxes = self.detector.detect(gray)
        vec = normalise_face(gray, boxes[0] if boxes else None)
        if vec is None:
            return CaptureResult(False, message="No usable face found in that image.")

        folder = self.user_dir(user_id)
        index = self.sample_count(user_id) + 1
        out_path = os.path.join(folder, "sample_%02d.png" % index)
        x, y, w, h = boxes[0]
        crop = gray[max(0, y):y + h, max(0, x):x + w]
        if crop.size == 0:
            crop = gray
        crop = _equalise(_resize_gray(crop, FACE_SIZE))
        if not _write_gray(out_path, crop):
            return CaptureResult(False, message="Could not save the face sample.")
        return CaptureResult(True, path=out_path,
                             message="Sample %d saved (detector: %s)."
                                     % (index, self.detector.description))

    def add_samples_from_crops(self, user_id: str, crops) -> Tuple[int, str]:
        """Store already-cropped greyscale faces collected by the camera dialog.

        The live preview has already detected and cropped each face, so there is
        no point re-running the detector over them here.
        """
        if not self.available():
            return 0, self.dependency_message()
        folder = self.user_dir(user_id)
        saved = 0
        for crop in crops or []:
            try:
                normalised = _equalise(_resize_gray(crop, FACE_SIZE))
            except Exception:
                continue
            index = self.sample_count(user_id) + 1
            path = os.path.join(folder, "sample_%02d.png" % index)
            if _write_gray(path, normalised):
                saved += 1
        if saved == 0:
            return 0, "Could not save any face samples."
        return saved, "Saved %d face sample(s)." % saved

    def clear_samples(self, user_id: str) -> int:
        removed = 0
        for path in self.sample_paths(user_id):
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
        return removed

    # -- training -----------------------------------------------------------

    def train(self) -> Tuple[bool, str]:
        """Fit the eigenface subspace over every enrolled sample."""
        if not self.available():
            return False, self.dependency_message()

        vectors: List["np.ndarray"] = []
        labels: List[str] = []
        skipped: List[str] = []
        for user_id in self.enrolled_users():
            paths = self.sample_paths(user_id)
            if len(paths) < FACE_MIN_SAMPLES:
                skipped.append("%s (%d/%d samples)" % (user_id, len(paths), FACE_MIN_SAMPLES))
                continue
            for path in paths:
                gray = _load_gray_array(path)
                if gray is None:
                    continue
                # Stored samples are already cropped, so skip re-detection.
                vec = normalise_face(gray, None)
                if vec is not None:
                    vectors.append(vec)
                    labels.append(user_id)

        if len(set(labels)) < 1 or len(vectors) < 2:
            message = "Not enough enrolled face samples to train."
            if skipped:
                message += " Needs at least %d per user; short: %s." % (
                    FACE_MIN_SAMPLES, ", ".join(skipped),
                )
            return False, message

        matrix = np.vstack(vectors)
        mean = matrix.mean(axis=0)
        centred = matrix - mean

        # Economy SVD: rows are samples, so Vt rows are the principal axes.
        components = int(min(FACE_PCA_COMPONENTS, min(centred.shape) - 1))
        components = max(1, components)
        try:
            _, _, vt = np.linalg.svd(centred, full_matrices=False)
        except np.linalg.LinAlgError:
            return False, "Face training failed (singular value decomposition error)."
        basis = vt[:components]

        projected = centred @ basis.T
        norms = np.linalg.norm(projected, axis=1, keepdims=True)
        norms[norms < 1e-9] = 1.0
        projected = projected / norms

        self._mean = mean
        self._basis = basis
        self._gallery = [(labels[i], projected[i]) for i in range(len(labels))]
        self._save_model()

        note = "Trained on %d samples from %d user(s) using %d components." % (
            len(vectors), len(set(labels)), components,
        )
        if skipped:
            note += " Skipped: %s." % ", ".join(skipped)
        return True, note

    def _save_model(self) -> None:
        if self._basis is None or self._mean is None:
            return
        payload = {
            "mean": self._mean.tolist(),
            "basis": self._basis.tolist(),
            "gallery": [
                {"user_id": uid, "vector": vec.tolist()} for uid, vec in self._gallery
            ],
            "face_size": FACE_SIZE,
        }
        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        tmp = self.model_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, self.model_path)
        except OSError:
            pass

    def _load_model(self) -> None:
        if np is None or not os.path.exists(self.model_path):
            return
        try:
            with open(self.model_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if int(payload.get("face_size", FACE_SIZE)) != FACE_SIZE:
                return  # retrain needed after a size change
            self._mean = np.asarray(payload["mean"], dtype=np.float32)
            self._basis = np.asarray(payload["basis"], dtype=np.float32)
            self._gallery = [
                (entry["user_id"], np.asarray(entry["vector"], dtype=np.float32))
                for entry in payload.get("gallery", [])
            ]
        except (OSError, ValueError, KeyError):
            self._mean = None
            self._basis = None
            self._gallery = []

    # -- identification -----------------------------------------------------

    def _project(self, vec):
        centred = vec - self._mean
        projected = centred @ self._basis.T
        norm = float(np.linalg.norm(projected))
        if norm < 1e-9:
            return None
        return projected / norm

    def identify_vector(self, vec) -> Identification:
        if not self.trained:
            return Identification(
                False, confidence=-1.0,
                message="No face has been enrolled on this system yet, so there "
                        "is nothing to match against.",
                backend=self.backend_description,
            )
        projected = self._project(vec)
        if projected is None:
            return Identification(False, message="Could not describe that face.",
                                  backend=self.backend_description)

        best_user, best_score = None, -1.0
        for user_id, gallery_vec in self._gallery:
            score = float(np.dot(projected, gallery_vec))
            if score > best_score:
                best_score, best_user = score, user_id

        if best_user is None or best_score < FACE_MATCH_THRESHOLD:
            return Identification(
                False, confidence=max(0.0, best_score),
                message="Face not recognised (best match %.0f%%, need %.0f%%)."
                        % (max(0.0, best_score) * 100, FACE_MATCH_THRESHOLD * 100),
                backend=self.backend_description,
            )
        return Identification(
            True, user_id=best_user, confidence=best_score,
            message="Face matched with %.0f%% confidence." % (best_score * 100),
            backend=self.backend_description,
        )

    def identify_image(self, image_path: str) -> Identification:
        if not self.available():
            return Identification(False, message=self.dependency_message())
        gray = _load_gray_array(image_path)
        if gray is None:
            return Identification(False, message="Could not read that image.")
        boxes = self.detector.detect(gray)
        vec = normalise_face(gray, boxes[0] if boxes else None)
        if vec is None:
            return Identification(False, message="No usable face found in that image.",
                                  backend=self.backend_description)
        return self.identify_vector(vec)

def _write_gray(path: str, arr) -> bool:
    try:
        if cv2 is not None:
            return bool(cv2.imwrite(path, arr))
        if Image is not None:
            Image.fromarray(arr).save(path)
            return True
    except Exception:
        return False
    return False


# ===========================================================================
# Voice
# ===========================================================================
