"""Opening a camera reliably across platforms.

Why this module exists
----------------------
``cv2.VideoCapture(0)`` looks like it should just work.  It does not, for two
reasons that bit this project on a real machine:

1.  **OpenCV picks the backend itself, and picks badly.**  On macOS it can
    select the FFMPEG backend, which cannot enumerate an AVFoundation webcam at
    all.  The symptom is a log line reading ``open VIDEOIO/FFMPEG: Failed list
    devices for backend avfoundation`` followed by ``camera failed to properly
    initialize!`` -- which reads like a broken camera but is really a backend
    mismatch.  Naming the platform's native backend explicitly fixes it.

2.  **``isOpened()`` lies.**  On macOS, a capture whose camera permission has
    been denied frequently reports ``isOpened() == True`` and then returns
    nothing from every ``read()``.  Checking ``isOpened()`` alone therefore
    reports success and the application hangs on a black frame.  The only
    trustworthy test is to actually read a frame.

So every camera in this project is opened through :func:`open_camera`, which
tries the native backend first, falls back through the others, scans a few
device indices, and proves the result by reading a frame before returning it.
"""

from __future__ import annotations

import platform
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

try:  # pragma: no cover - import guard
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore

#: How many indices to scan when no explicit one is given.  A Mac with
#: Continuity Camera enabled often exposes the iPhone at 0 and the built-in
#: camera at 1, so scanning past 0 matters.
MAX_INDEX = 4

#: Frames to discard before trusting the stream.  Most webcams return a few
#: empty or black frames while the sensor warms up.
WARMUP_FRAMES = 5

#: Seconds to keep trying to read a first frame before giving up on a device.
READ_TIMEOUT = 1.5

#: After this many devices have opened but produced nothing, stop scanning.
#: That pattern means the cause is systemic -- almost always a denied camera
#: permission -- not specific to one device, so trying the rest only makes the
#: user wait longer for the same answer.
BLIND_ATTEMPTS_BEFORE_GIVING_UP = 2


@dataclass
class CameraAttempt:
    backend: str
    index: int
    opened: bool
    read_ok: bool
    note: str = ""

    def __str__(self) -> str:
        state = "read OK" if self.read_ok else (
            "opened but no frame" if self.opened else "did not open")
        return "index %d via %-14s %s%s" % (
            self.index, self.backend, state,
            ("  (%s)" % self.note) if self.note else "")


@dataclass
class CameraResult:
    capture: object = None
    backend: str = ""
    index: int = -1
    width: int = 0
    height: int = 0
    attempts: List[CameraAttempt] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.capture is not None

    @property
    def description(self) -> str:
        if not self.ok:
            return "no working camera"
        return "camera %d via %s at %dx%d" % (
            self.index, self.backend, self.width, self.height)

    def release(self) -> None:
        if self.capture is not None:
            try:
                self.capture.release()
            except Exception:
                pass
            self.capture = None

    def failure_message(self) -> str:
        """A message that names the likely cause rather than guessing."""
        if self.ok:
            return ""

        opened_but_blind = any(a.opened and not a.read_ok for a in self.attempts)
        system = platform.system()

        if opened_but_blind:
            if system == "Darwin":
                return (
                    "The camera was found but returned no image, which on macOS "
                    "almost always means camera permission is denied.\n\n"
                    "Open System Settings > Privacy & Security > Camera and "
                    "switch on the app you launched this from (Terminal, iTerm "
                    "or your editor). Then quit that app completely with "
                    "Cmd-Q -- closing the window is not enough -- and reopen "
                    "it.\n\n"
                    "If it is already switched on, something else is holding "
                    "the camera: quit Zoom, Teams, FaceTime and Photo Booth."
                )
            return (
                "The camera was found but returned no image. Another "
                "application may be using it, or this user may not have "
                "permission to read from it."
            )

        if system == "Darwin":
            return (
                "No camera could be opened.\n\n"
                "If this Mac has a built-in camera, grant camera access to the "
                "app you launched from in System Settings > Privacy & Security "
                "> Camera, then quit it with Cmd-Q and reopen it.\n\n"
                "If you use Continuity Camera, an iPhone that goes to sleep can "
                "take the camera with it -- move the phone away or turn "
                "Continuity Camera off on it, then try again."
            )
        return (
            "No camera could be opened. Check that one is connected and that "
            "no other application is using it."
        )


def available() -> bool:
    return cv2 is not None


def candidate_backends() -> List[Tuple[str, int]]:
    """Backends to try, native one first.

    Naming the platform's own backend is the whole point: left to itself,
    OpenCV may choose one that cannot see a webcam.
    """
    if cv2 is None:
        return []

    def flag(name: str):
        return getattr(cv2, name, None)

    system = platform.system()
    if system == "Darwin":
        order = ["CAP_AVFOUNDATION", "CAP_ANY"]
    elif system == "Windows":
        order = ["CAP_DSHOW", "CAP_MSMF", "CAP_ANY"]
    else:
        order = ["CAP_V4L2", "CAP_V4L", "CAP_GSTREAMER", "CAP_ANY"]

    out = []
    for name in order:
        value = flag(name)
        if value is not None:
            out.append((name.replace("CAP_", ""), int(value)))
    return out


def _read_a_real_frame(capture, timeout: float = READ_TIMEOUT):
    """Return a frame, or None. Discards warm-up frames and all-black frames.

    ``isOpened()`` is not evidence; a frame is.
    """
    deadline = time.time() + timeout
    seen = 0
    while time.time() < deadline:
        ok, frame = capture.read()
        if ok and frame is not None and getattr(frame, "size", 0) > 0:
            seen += 1
            # The first frames out of a webcam are often blank; accept one
            # once we have seen a few, or immediately if it has any content.
            if seen >= WARMUP_FRAMES or float(frame.max()) > 8:
                return frame
        time.sleep(0.03)
    return None


def open_camera(index: Optional[int] = None,
                timeout: float = READ_TIMEOUT) -> CameraResult:
    """Open the first camera that actually produces a frame.

    ``index`` pins a specific device; leaving it unset scans the first few.
    The returned capture has already produced at least one frame, so a caller
    that gets ``ok`` can rely on it.
    """
    result = CameraResult()
    if cv2 is None:
        result.attempts.append(
            CameraAttempt("none", -1, False, False, "opencv-python not installed"))
        return result

    indices = [index] if index is not None else list(range(MAX_INDEX))
    blind = 0

    for backend_name, backend_flag in candidate_backends():
        for device_index in indices:
            if blind >= BLIND_ATTEMPTS_BEFORE_GIVING_UP:
                return result
            capture = None
            try:
                capture = cv2.VideoCapture(device_index, backend_flag)
                opened = bool(capture.isOpened())
                if not opened:
                    result.attempts.append(
                        CameraAttempt(backend_name, device_index, False, False))
                    capture.release()
                    continue

                frame = _read_a_real_frame(capture, timeout)
                if frame is None:
                    blind += 1
                    result.attempts.append(
                        CameraAttempt(backend_name, device_index, True, False))
                    capture.release()
                    continue

                result.capture = capture
                result.backend = backend_name
                result.index = device_index
                result.height, result.width = frame.shape[:2]
                result.attempts.append(
                    CameraAttempt(backend_name, device_index, True, True))
                return result
            except Exception as exc:                     # pragma: no cover
                result.attempts.append(
                    CameraAttempt(backend_name, device_index, False, False,
                                  str(exc)[:70]))
                if capture is not None:
                    try:
                        capture.release()
                    except Exception:
                        pass

    return result


def diagnose() -> str:
    """A full report of what was tried and what happened, for the user."""
    lines = []
    lines.append("CAMERA DIAGNOSTIC")
    lines.append("=" * 58)
    lines.append("Platform        : %s %s" % (platform.system(),
                                              platform.release()))
    lines.append("Python          : %s" % sys.version.split()[0])

    if cv2 is None:
        lines.append("OpenCV          : NOT INSTALLED")
        lines.append("")
        lines.append("Install it with:  pip install opencv-python")
        return "\n".join(lines)

    lines.append("OpenCV          : %s" % cv2.__version__)
    backends = candidate_backends()
    lines.append("Backends tried  : %s"
                 % ", ".join(name for name, _ in backends))
    lines.append("")

    result = open_camera()
    lines.append("Attempts:")
    for attempt in result.attempts:
        lines.append("  " + str(attempt))
    lines.append("")

    if result.ok:
        lines.append("RESULT: working. %s" % result.description)
        lines.append("")
        lines.append("Face enrolment and face login should work.")
        result.release()
    else:
        lines.append("RESULT: no usable camera.")
        lines.append("")
        lines.append(result.failure_message())

    return "\n".join(lines)
