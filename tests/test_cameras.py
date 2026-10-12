"""Tests for camera opening.

These cover the two failure modes that actually broke the project on a real
machine and which no amount of unit-testing the recogniser would have caught:

1.  OpenCV choosing a backend that cannot see the webcam (the FFMPEG/
    AVFoundation mismatch on macOS).
2.  A capture that reports ``isOpened() == True`` and then never returns a
    frame, which is what a denied camera permission looks like on macOS.

``cv2.VideoCapture`` is replaced with fakes so the behaviour can be exercised
on a build machine with no camera at all.
"""

from __future__ import annotations

import os
import platform
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

from bookstore import cameras                      # noqa: E402

HAS_DEPS = np is not None and cv2 is not None


def a_frame(value: int = 120, size=(48, 64)):
    return np.full((size[0], size[1], 3), value, dtype=np.uint8)


@unittest.skipUnless(HAS_DEPS, "needs numpy and opencv-python")
class TestOpenCamera(unittest.TestCase):
    def setUp(self):
        self._real = cv2.VideoCapture
        self._real_system = platform.system

    def tearDown(self):
        cv2.VideoCapture = self._real
        platform.system = self._real_system

    # -- fakes ----------------------------------------------------------

    def install(self, behaviour):
        """behaviour(index, backend) -> ('open'|'blind'|'closed')"""
        outer = self

        class Fake:
            def __init__(self, index=0, backend=None):
                self.index = index
                self.backend = backend
                self.mode = behaviour(index, backend)
                self.reads = 0

            def isOpened(self):
                return self.mode in ("open", "blind")

            def read(self):
                self.reads += 1
                if self.mode != "open":
                    return False, None
                return True, a_frame()

            def release(self):
                pass

        cv2.VideoCapture = Fake
        return Fake

    # -- tests ----------------------------------------------------------

    def test_a_working_camera_is_found_and_proven_with_a_frame(self):
        self.install(lambda i, b: "open" if i == 0 else "closed")
        result = cameras.open_camera()
        self.assertTrue(result.ok)
        self.assertEqual(result.index, 0)
        self.assertEqual((result.height, result.width), (48, 64))
        result.release()

    def test_a_camera_that_opens_but_never_yields_a_frame_is_rejected(self):
        """The macOS denied-permission signature."""
        self.install(lambda i, b: "blind")
        result = cameras.open_camera(timeout=0.15)
        self.assertFalse(result.ok,
                         "a blind capture must not be reported as working")
        self.assertTrue(any(a.opened and not a.read_ok for a in result.attempts))

    def test_the_permission_hint_is_given_for_a_blind_camera_on_macos(self):
        platform.system = lambda: "Darwin"
        self.install(lambda i, b: "blind")
        result = cameras.open_camera(timeout=0.15)
        message = result.failure_message()
        self.assertIn("permission", message.lower())
        self.assertIn("Privacy & Security", message)
        self.assertIn("Cmd-Q", message)

    def test_a_later_index_is_found_when_the_first_is_busy(self):
        """Continuity Camera frequently occupies index 0 and then goes away."""
        self.install(lambda i, b: "open" if i == 2 else "closed")
        result = cameras.open_camera()
        self.assertTrue(result.ok)
        self.assertEqual(result.index, 2)
        result.release()

    def test_a_pinned_index_is_not_scanned_past(self):
        self.install(lambda i, b: "open" if i == 2 else "closed")
        result = cameras.open_camera(index=0)
        self.assertFalse(result.ok)
        self.assertTrue(all(a.index == 0 for a in result.attempts))

    def test_the_native_backend_is_tried_before_the_fallbacks(self):
        platform.system = lambda: "Darwin"
        order = cameras.candidate_backends()
        self.assertTrue(order, "no backends offered")
        self.assertEqual(order[0][0], "AVFOUNDATION",
                         "the native backend must be tried first")

    def test_a_backend_that_fails_does_not_stop_the_search(self):
        """The real bug: FFMPEG cannot see a Mac webcam, AVFoundation can."""
        platform.system = lambda: "Darwin"
        seen = []

        def behaviour(index, backend):
            seen.append(backend)
            avf = getattr(cv2, "CAP_AVFOUNDATION", None)
            return "open" if backend == avf else "closed"

        self.install(behaviour)
        result = cameras.open_camera()
        self.assertTrue(result.ok)
        self.assertEqual(result.backend, "AVFOUNDATION")
        result.release()

    def test_an_exception_while_opening_is_recorded_not_raised(self):
        def explode(index=0, backend=None):
            raise RuntimeError("device exploded")

        cv2.VideoCapture = explode
        result = cameras.open_camera()
        self.assertFalse(result.ok)
        self.assertTrue(result.attempts)
        self.assertIn("exploded", result.attempts[0].note)

    def test_the_platform_backend_order_is_sensible_everywhere(self):
        for system, expected_first in (("Darwin", "AVFOUNDATION"),
                                       ("Windows", "DSHOW"),
                                       ("Linux", "V4L2")):
            platform.system = lambda s=system: s
            order = cameras.candidate_backends()
            self.assertEqual(order[0][0], expected_first,
                             "wrong first backend on %s" % system)
            self.assertEqual(order[-1][0], "ANY",
                             "ANY should remain as the last resort")

    def test_diagnose_runs_and_reports_without_a_camera(self):
        self.install(lambda i, b: "closed")
        report = cameras.diagnose()
        self.assertIn("CAMERA DIAGNOSTIC", report)
        self.assertIn("no usable camera", report)

    def test_diagnose_reports_a_working_camera(self):
        self.install(lambda i, b: "open" if i == 0 else "closed")
        report = cameras.diagnose()
        self.assertIn("RESULT: working", report)


@unittest.skipUnless(HAS_DEPS, "needs numpy and opencv-python")
class TestBiometricsUsesTheHelper(unittest.TestCase):
    """The recogniser must report the helper's diagnosis, not its own guess."""

    def setUp(self):
        import shutil, tempfile
        from bookstore.biometrics import BiometricService
        from bookstore.storage import Storage
        self.tmp = tempfile.mkdtemp(prefix="cam-bio-")
        self._rm = shutil.rmtree
        self.storage = Storage(os.path.join(self.tmp, "data"))
        self.service = BiometricService(self.storage)
        self._real = cv2.VideoCapture
        self._real_system = platform.system

    def tearDown(self):
        cv2.VideoCapture = self._real
        platform.system = self._real_system
        self._rm(self.tmp, ignore_errors=True)

    def test_enrolment_reports_the_permission_problem(self):
        platform.system = lambda: "Darwin"

        class Blind:
            def __init__(self, index=0, backend=None):
                pass

            def isOpened(self):
                return True

            def read(self):
                return False, None

            def release(self):
                pass

        cv2.VideoCapture = Blind
        saved, message = self.service.face.capture_samples_from_camera("u001")
        self.assertEqual(saved, 0)
        self.assertIn("permission", message.lower())

    def test_identification_without_enrolment_says_so_before_opening_a_camera(self):
        """No camera is touched when there is nothing to match against."""
        touched = {"n": 0}

        class Counting:
            def __init__(self, index=0, backend=None):
                touched["n"] += 1

            def isOpened(self):
                return False

            def read(self):
                return False, None

            def release(self):
                pass

        cv2.VideoCapture = Counting
        result = self.service.face.identify_from_camera()
        self.assertFalse(result.identified)
        self.assertIn("enrolled", result.message.lower())
        self.assertEqual(touched["n"], 0,
                         "the camera should not be opened at all")


if __name__ == "__main__":
    unittest.main(verbosity=2)
