"""Live camera window used for face enrolment and face login.

Without this, the biometric steps happen invisibly: the app opens the webcam,
grabs frames and closes it again, so there is nothing on screen and no way to
tell a permissions problem from a lighting problem from an unenrolled account.
This dialog shows the camera feed with the detected face boxed, so the user can
see themselves, see that a face was found, and see the match confidence climb.

Threading model
---------------
OpenCV's ``read()`` blocks, so the capture runs on a worker thread that keeps
only the newest frame.  Tk is not thread-safe, so the worker never touches a
widget: the UI pulls the latest frame on a timer through ``after``.  Closing the
dialog sets a stop flag the worker checks, and the camera is always released in
a ``finally``.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional, Tuple

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore

try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:  # pragma: no cover
    Image = None  # type: ignore

from .. import cameras
from ..biometrics import normalise_face
from .widgets import Theme

PREVIEW_WIDTH = 520
#: Seconds between automatic enrolment captures, so the samples differ slightly.
CAPTURE_INTERVAL = 0.7
#: Seconds between identification attempts while the preview runs.
IDENTIFY_INTERVAL = 0.35


def camera_available() -> bool:
    return cv2 is not None and np is not None and Image is not None


def dependency_message() -> str:
    missing = []
    if cv2 is None:
        missing.append("opencv-python")
    if np is None:
        missing.append("numpy")
    if Image is None:
        missing.append("Pillow")
    if not missing:
        return ""
    return "The camera needs %s. Install with:  pip install %s" % (
        " and ".join(missing), " ".join(missing),
    )


PERMISSION_HINT = (
    "Could not open the camera.\n\n"
    "On macOS, allow camera access for Terminal in System Settings -> "
    "Privacy & Security -> Camera, then fully quit Terminal (Cmd-Q) and "
    "reopen it.\n\n"
    "Also check that no other app (Zoom, Photo Booth, FaceTime) is using "
    "the camera."
)


class CameraDialog(tk.Toplevel):
    """Modal camera window.

    Two modes:

    ``enroll``
        Collects ``target`` normalised face crops, capturing automatically
        whenever a face is visible.  ``on_complete`` receives the list of
        greyscale crops.

    ``identify``
        Repeatedly runs the recogniser against the live frame and closes as
        soon as it matches.  ``on_complete`` receives the ``Identification``.
    """

    def __init__(self, parent, app, mode: str = "identify",
                 recogniser=None, target: int = 5,
                 title: str = "Camera",
                 instruction: str = "",
                 on_complete: Optional[Callable] = None,
                 camera_index: Optional[int] = None):
        super().__init__(parent)
        self.app = app
        self.mode = mode
        self.recogniser = recogniser
        self.target = max(1, int(target))
        self.on_complete = on_complete
        self.camera_index = camera_index

        self.title(title)
        self.configure(bg=Theme.BG)
        self.resizable(False, False)
        self.transient(parent)

        # Worker state.  Only the worker writes _frame; only the UI reads it.
        self._lock = threading.Lock()
        self._frame = None
        self._stopping = False
        self._worker: Optional[threading.Thread] = None
        self._error: Optional[str] = None
        self._camera_description = ""

        self._samples: List = []
        self._last_capture = 0.0
        self._last_identify = 0.0
        self._result = None
        self._finished = False
        self._photo = None          # keep a reference or Tk drops the image

        self._build(instruction)
        self._start()

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())
        self.after(60, self._tick)

        # Centre on the parent.
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        self.geometry("+%d+%d" % (max(0, x), max(0, y)))

        try:
            self.grab_set()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self, instruction: str) -> None:
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=18, pady=16)

        if instruction:
            ttk.Label(wrap, text=instruction, style="Muted.TLabel",
                      wraplength=PREVIEW_WIDTH, justify="left"
                      ).pack(anchor="w", pady=(0, 10))

        self.preview = tk.Label(wrap, bg="#15130f", width=PREVIEW_WIDTH,
                                height=int(PREVIEW_WIDTH * 0.75))
        self.preview.pack()

        self.status = ttk.Label(wrap, text="Starting the camera...",
                                style="TLabel",
                                font=(self.app.fonts["sans"], 10, "bold"),
                                wraplength=PREVIEW_WIDTH, justify="left")
        self.status.pack(anchor="w", pady=(12, 4))

        self.detail = ttk.Label(wrap, text="", style="Muted.TLabel",
                                wraplength=PREVIEW_WIDTH, justify="left")
        self.detail.pack(anchor="w")

        if self.mode == "enroll":
            self.progress = ttk.Progressbar(wrap, length=PREVIEW_WIDTH,
                                            maximum=self.target, value=0)
            self.progress.pack(pady=(10, 0))

        buttons = ttk.Frame(wrap)
        buttons.pack(fill="x", pady=(14, 0))
        self.cancel_button = ttk.Button(buttons, text="Cancel",
                                        command=self._cancel)
        self.cancel_button.pack(side="right")

    # ------------------------------------------------------------------
    # Capture thread
    # ------------------------------------------------------------------

    def _start(self) -> None:
        if not camera_available():
            self._error = dependency_message()
            return
        self._worker = threading.Thread(target=self._capture_loop, daemon=True)
        self._worker.start()

    def _capture_loop(self) -> None:
        capture = None
        try:
            # cameras.open_camera names the platform's native backend and
            # proves the device delivers a frame before handing it back, so a
            # denied permission is reported as such instead of appearing as a
            # frozen black preview.
            opened = cameras.open_camera(self.camera_index)
            if not opened.ok:
                self._error = opened.failure_message()
                return
            capture = opened.capture
            self._camera_description = opened.description
            blank_frames = 0
            while not self._stopping:
                ok, frame = capture.read()
                if not ok or frame is None:
                    blank_frames += 1
                    if blank_frames > 30:
                        self._error = (
                            "The camera opened but returned no image. Another "
                            "app may be using it, or permission is denied."
                        )
                        return
                    time.sleep(0.03)
                    continue
                blank_frames = 0
                with self._lock:
                    self._frame = frame
                time.sleep(0.02)
        except Exception as exc:  # pragma: no cover - hardware dependent
            self._error = "Camera error: %s" % exc
        finally:
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    pass

    def _latest_frame(self):
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    # ------------------------------------------------------------------
    # UI timer
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        if self._finished:
            return

        if self._error:
            self.status.configure(text="Camera unavailable", foreground=Theme.BAD)
            self.detail.configure(text=self._error)
            self.cancel_button.configure(text="Close")
            self._finish(None)
            return

        frame = self._latest_frame()
        if frame is None:
            self.after(60, self._tick)
            return

        boxes = []
        detector = getattr(self.recogniser, "detector", None)
        if detector is not None:
            try:
                boxes = detector.detect(frame)
            except Exception:
                boxes = []

        # The detector returns a centre box when it finds nothing, so a box on
        # its own is not evidence of a face.  Only a genuine detection counts --
        # otherwise the preview would cheerfully enrol a photograph of a wall.
        face_box = boxes[0] if boxes else None
        fallback = bool(getattr(detector, "last_was_fallback", False))
        looks_real = bool(face_box) and not fallback

        self._render(frame, face_box, looks_real)

        if self.mode == "enroll":
            self._step_enroll(frame, face_box, looks_real)
        else:
            self._step_identify(frame, face_box, looks_real)

        if not self._finished:
            self.after(45, self._tick)

    def _render(self, frame, face_box, looks_real: bool) -> None:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)

        # Mirror it: people expect a camera preview to behave like a mirror.
        image = image.transpose(Image.FLIP_LEFT_RIGHT)

        scale = PREVIEW_WIDTH / float(image.width)
        new_size = (PREVIEW_WIDTH, max(1, int(image.height * scale)))
        image = image.resize(new_size, Image.BILINEAR)

        if face_box is not None:
            x, y, w, h = face_box
            # Flip the box horizontally to match the mirrored image.
            x_mirrored = frame.shape[1] - x - w
            box = [x_mirrored * scale, y * scale,
                   (x_mirrored + w) * scale, (y + h) * scale]
            draw = ImageDraw.Draw(image)
            colour = (86, 200, 130) if looks_real else (190, 150, 60)
            draw.rectangle(box, outline=colour, width=3)

        self._photo = ImageTk.PhotoImage(image)
        self.preview.configure(image=self._photo, width=new_size[0],
                               height=new_size[1])

    # ------------------------------------------------------------------
    # Enrolment
    # ------------------------------------------------------------------

    def _step_enroll(self, frame, face_box, looks_real: bool) -> None:
        done = len(self._samples)
        self.progress.configure(value=done)

        if done >= self.target:
            self.status.configure(text="Captured %d samples." % done,
                                  foreground=Theme.GOOD)
            self._finish(list(self._samples))
            return

        if not looks_real:
            self.status.configure(text="Looking for your face...",
                                  foreground=Theme.WARN)
            self.detail.configure(
                text="Face the camera straight on, fill the frame, and keep "
                     "the light in front of you rather than behind."
            )
            return

        now = time.time()
        self.status.configure(
            text="Hold still - capturing %d of %d" % (done + 1, self.target),
            foreground=Theme.GOOD,
        )
        self.detail.configure(
            text="Move your head a little between shots so the samples differ."
        )
        if now - self._last_capture < CAPTURE_INTERVAL:
            return

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        vec = normalise_face(gray, face_box)
        if vec is None:
            return

        x, y, w, h = face_box
        crop = gray[max(0, y):y + h, max(0, x):x + w]
        if crop.size == 0:
            return
        self._samples.append(crop)
        self._last_capture = now

    # ------------------------------------------------------------------
    # Identification
    # ------------------------------------------------------------------

    def _step_identify(self, frame, face_box, looks_real: bool) -> None:
        if self.recogniser is None:
            self._finish(None)
            return

        if not getattr(self.recogniser, "trained", False):
            self.status.configure(text="No face is enrolled yet",
                                  foreground=Theme.BAD)
            self.detail.configure(
                text="There is nothing to match against. Create an account "
                     "(which enrols your face) or ask the super-user to enrol "
                     "you."
            )
            return

        if not looks_real:
            self.status.configure(text="Looking for your face...",
                                  foreground=Theme.WARN)
            self.detail.configure(
                text="Face the camera straight on and make sure your face is "
                     "well lit."
            )
            return

        now = time.time()
        if now - self._last_identify < IDENTIFY_INTERVAL:
            return
        self._last_identify = now

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        vec = normalise_face(gray, face_box)
        if vec is None:
            return

        result = self.recogniser.identify_vector(vec)
        if result.identified:
            self.status.configure(text=result.message, foreground=Theme.GOOD)
            self.detail.configure(text="Signing you in...")
            self._result = result
            self.after(500, lambda: self._finish(result))
            return

        confidence = max(0.0, result.confidence)
        self.status.configure(text="Checking... best match %.0f%%"
                                   % (confidence * 100),
                              foreground=Theme.WARN)
        self.detail.configure(
            text="Keep looking at the camera. If this stays low, you may not "
                 "be enrolled on this account."
        )
        self._result = result

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _stop_worker(self) -> None:
        self._stopping = True
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=1.5)

    def _finish(self, payload) -> None:
        if self._finished:
            return
        self._finished = True
        self._stop_worker()
        callback = self.on_complete
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        if callback:
            callback(payload)

    def _cancel(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._stop_worker()
        callback = self.on_complete
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        if callback:
            # In identify mode hand back whatever the last attempt said, so the
            # caller can explain why it did not match.
            callback(self._result if self.mode == "identify" else None)


def enroll_face(parent, app, recogniser, target: int = 5,
                on_complete: Optional[Callable] = None,
                person: str = "") -> Optional[CameraDialog]:
    """Open the camera and collect ``target`` face crops."""
    if not camera_available():
        return None
    return CameraDialog(
        parent, app, mode="enroll", recogniser=recogniser, target=target,
        title="Enrol a face" + (" - %s" % person if person else ""),
        instruction=(
            "Look straight at the camera. %d photographs will be taken "
            "automatically, about once a second. Move your head slightly "
            "between them so the samples are not identical." % target
        ),
        on_complete=on_complete,
    )


def identify_face(parent, app, recogniser,
                  on_complete: Optional[Callable] = None) -> Optional[CameraDialog]:
    """Open the camera and try to recognise whoever is in front of it."""
    if not camera_available():
        return None
    return CameraDialog(
        parent, app, mode="identify", recogniser=recogniser,
        title="Sign in with your face",
        instruction="Look at the camera. You will be signed in as soon as you "
                    "are recognised.",
        on_complete=on_complete,
    )
