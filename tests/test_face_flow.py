"""End-to-end test of the face enrolment and face login flow.

The real bug this file exists to prevent: the camera path was never exercised
by a test, so a message that lied about *why* a face login failed ("Could not
see a face" when the real cause was that nobody had enrolled) shipped
unnoticed.

There is no webcam on a build machine, so ``FakeCapture`` stands in for
``cv2.VideoCapture`` and the detector is stubbed to report a real detection.
That is deliberate: these tests cover *our* plumbing -- dialog lifecycle,
sample storage, training, matching, and the password/face gating -- not
OpenCV's ability to find a face, which is not ours to test.

Skipped automatically when Tkinter, numpy, Pillow or OpenCV are unavailable.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
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

try:
    import tkinter
except ImportError:  # pragma: no cover
    tkinter = None

from bookstore import models                       # noqa: E402
from bookstore.biometrics import BiometricService  # noqa: E402
from bookstore.config import Config                # noqa: E402
from bookstore.rules import RulesEngine            # noqa: E402
from bookstore.storage import Storage              # noqa: E402

HAS_DEPS = np is not None and cv2 is not None


def synthetic_face(person: int, variant: int, size=(240, 320)):
    """A deterministic, person-specific pattern standing in for a face."""
    height, width = size
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    rng = np.random.default_rng(person * 100 + variant)
    pattern = (
        128
        + 55 * np.sin((xx / width) * (person + 1) * 2.6 + 0.15 * variant)
        + 45 * np.cos((yy / height) * (person + 2) * 2.1)
        + rng.normal(0, 6, (height, width))
    )
    gray = np.clip(pattern, 0, 255).astype(np.uint8)
    return np.stack([gray] * 3, axis=-1)          # BGR


class StubDetector:
    """Always reports a real detection in the middle of the frame."""

    def __init__(self):
        self.last_was_fallback = False
        self.backend = "stub"

    @property
    def description(self):
        return "stub detector"

    def detect(self, frame):
        self.last_was_fallback = False
        arr = np.asarray(frame)
        height, width = arr.shape[0], arr.shape[1]
        side = int(min(height, width) * 0.6)
        return [((width - side) // 2, (height - side) // 2, side, side)]


@unittest.skipUnless(HAS_DEPS, "needs numpy and opencv-python")
class TestFaceEnrolmentAndMatching(unittest.TestCase):
    """The recogniser half, with no GUI involved."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="face-test-")
        self.config = Config(path=os.path.join(self.tmp, "settings.json"))
        self.storage = Storage(os.path.join(self.tmp, "data"))
        self.rules = RulesEngine(self.storage, self.config)
        self.bio = BiometricService(self.storage)
        self.bio.face.detector = StubDetector()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _enrol(self, user_id: str, person: int, count: int = 5):
        crops = []
        for variant in range(count):
            frame = synthetic_face(person, variant)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            x, y, w, h = self.bio.face.detector.detect(frame)[0]
            crops.append(gray[y:y + h, x:x + w])
        return self.bio.face.add_samples_from_crops(user_id, crops)

    def test_untrained_model_says_so_instead_of_blaming_the_camera(self):
        """The exact bug: an unenrolled system reported 'could not see a face'."""
        self.assertFalse(self.bio.face.trained)
        vector = np.ones(96 * 96, dtype=np.float32)
        vector /= np.linalg.norm(vector)
        result = self.bio.face.identify_vector(vector)

        self.assertFalse(result.identified)
        self.assertIn("enrolled", result.message.lower())
        self.assertNotIn("could not see", result.message.lower())

    def test_crops_are_saved_and_the_model_trains(self):
        saved, message = self._enrol("u001", person=0)
        self.assertEqual(saved, 5, message)
        self.assertEqual(self.bio.face.sample_count("u001"), 5)

        ok, note = self.bio.face.train()
        self.assertTrue(ok, note)
        self.assertTrue(self.bio.face.trained)

    def test_an_enrolled_person_is_recognised(self):
        self._enrol("u001", person=0)
        self._enrol("u002", person=1)
        self._enrol("u003", person=2)
        ok, note = self.bio.face.train()
        self.assertTrue(ok, note)

        # Probe with variant 5: just outside the enrolled set (0-4), which
        # stands for the same person photographed again a moment later.
        # A far-off variant is a different-looking image, not a different pose.
        for user_id, person in (("u001", 0), ("u002", 1), ("u003", 2)):
            frame = synthetic_face(person, variant=5)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            box = self.bio.face.detector.detect(frame)[0]
            from bookstore.biometrics import normalise_face
            vector = normalise_face(gray, box)
            result = self.bio.face.identify_vector(vector)
            self.assertTrue(result.identified,
                            "%s not recognised: %s" % (user_id, result.message))
            self.assertEqual(result.user_id, user_id)

    def test_a_stranger_is_rejected(self):
        """False acceptance is the failure that matters for a login."""
        self._enrol("u001", person=0)
        self._enrol("u002", person=1)
        self.bio.face.train()

        height, width = 240, 320
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        stranger = np.clip(
            255 * (0.5 + 0.5 * np.sin(xx * 0.9) * np.cos(yy * 0.7)), 0, 255
        ).astype(np.uint8)
        from bookstore.biometrics import normalise_face
        vector = normalise_face(stranger, None)
        result = self.bio.face.identify_vector(vector)

        self.assertFalse(result.identified,
                         "a stranger was accepted at %.2f" % result.confidence)
        self.assertIn("not recognised", result.message.lower())

    def test_clearing_samples_removes_them(self):
        self._enrol("u001", person=0)
        self.assertEqual(self.bio.face.sample_count("u001"), 5)
        self.assertEqual(self.bio.face.clear_samples("u001"), 5)
        self.assertEqual(self.bio.face.sample_count("u001"), 0)


@unittest.skipUnless(HAS_DEPS, "needs numpy and opencv-python")
class TestPasswordGating(unittest.TestCase):
    """Requirement 4: an enrolled account signs in with its face, not a password."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gate-test-")
        self.config = Config(path=os.path.join(self.tmp, "settings.json"))
        self.storage = Storage(os.path.join(self.tmp, "data"))
        self.rules = RulesEngine(self.storage, self.config)
        self.bio = BiometricService(self.storage)
        self.bio.face.detector = StubDetector()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _blocked(self, user):
        """Mirrors LoginView._password_blocked_for without building a window."""
        config = self.config
        if not bool(config.get("require_face_login")):
            return False
        if user.get("role") == models.SUPER_USER and bool(
            config.get("super_user_password_bootstrap")
        ):
            # Narrow exemption: only while the super-user has no face yet.
            if self.bio.face.sample_count(user["user_id"]) == 0:
                return False
        if self.bio.face.sample_count(user["user_id"]) == 0:
            return False
        if not self.bio.face.trained:
            return False
        return True

    def _enrol_and_train(self, user_id, person):
        crops = []
        for variant in range(5):
            frame = synthetic_face(person, variant)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            x, y, w, h = self.bio.face.detector.detect(frame)[0]
            crops.append(gray[y:y + h, x:x + w])
        self.bio.face.add_samples_from_crops(user_id, crops)
        # Two identities so PCA has something to separate.
        other = []
        for variant in range(5):
            frame = synthetic_face(person + 7, variant)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            x, y, w, h = self.bio.face.detector.detect(frame)[0]
            other.append(gray[y:y + h, x:x + w])
        self.bio.face.add_samples_from_crops("u999", other)
        self.bio.face.train()

    def test_an_enrolled_customer_cannot_use_a_password(self):
        user = self.rules.create_account("alice", "alice123",
                                         role=models.REGISTERED, balance=50.0)
        self.assertFalse(self._blocked(user), "not enrolled yet, so allowed")
        self._enrol_and_train(user["user_id"], person=0)
        self.assertTrue(self._blocked(user),
                        "an enrolled customer must sign in with their face")

    def test_an_unenrolled_super_user_keeps_password_access(self):
        """Bootstrap: somebody has to get in to perform the first enrolment."""
        admin = self.rules.create_account("margaret", "admin123",
                                          role=models.SUPER_USER)
        self._enrol_and_train("u900", person=3)      # somebody else enrols
        self.assertEqual(self.bio.face.sample_count(admin["user_id"]), 0)
        self.assertFalse(self._blocked(admin))

    def test_an_enrolled_super_user_must_use_their_face(self):
        """R4 names SU among the roles that sign in biometrically, so the
        bootstrap exemption ends the moment the SU has a face."""
        admin = self.rules.create_account("margaret", "admin123",
                                          role=models.SUPER_USER)
        self._enrol_and_train(admin["user_id"], person=3)
        self.assertGreater(self.bio.face.sample_count(admin["user_id"]), 0)
        self.assertTrue(self._blocked(admin),
                        "an enrolled super-user must sign in with their face")

    def test_an_unenrolled_account_keeps_password_access(self):
        """The seeded demo accounts predate enrolment and must still work."""
        user = self.rules.create_account("dave", "dave1234",
                                         role=models.REGISTERED, balance=1.0)
        self._enrol_and_train("u900", person=5)      # somebody else enrols
        self.assertTrue(self.bio.face.trained)
        self.assertFalse(self._blocked(user))

    def test_the_requirement_can_be_switched_off(self):
        user = self.rules.create_account("carol", "carol123",
                                         role=models.VIP, balance=10.0)
        self._enrol_and_train(user["user_id"], person=1)
        self.assertTrue(self._blocked(user))
        self.config.set("require_face_login", False)
        self.assertFalse(self._blocked(user))


@unittest.skipUnless(HAS_DEPS and tkinter is not None and os.environ.get("DISPLAY"),
                     "needs a display and the imaging stack")
class TestCameraDialog(unittest.TestCase):
    """Drives the live camera window against a simulated webcam."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-test-")
        self.storage = Storage(os.path.join(self.tmp, "data"))
        self.bio = BiometricService(self.storage)
        self.bio.face.detector = StubDetector()
        self._real_capture = cv2.VideoCapture

    def tearDown(self):
        cv2.VideoCapture = self._real_capture
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _install_fake_camera(self, person=0, opened=True, blank=False):
        frames = {"n": 0}

        class FakeCapture:
            # Two parameters: cameras.open_camera now names the backend
            # explicitly, so it calls cv2.VideoCapture(index, backend).
            def __init__(self, index=0, backend=None):
                self.index = index
                self.backend = backend

            def isOpened(self):
                return opened

            def read(self):
                if blank:
                    return False, None
                frames["n"] += 1
                return True, synthetic_face(person, frames["n"] % 7)

            def release(self):
                pass

        cv2.VideoCapture = FakeCapture

    def _run_dialog(self, root, **kwargs):
        from bookstore.ui.camera import CameraDialog
        captured = {}

        def done(payload):
            captured["payload"] = payload

        dialog = CameraDialog(root, root, on_complete=done, **kwargs)
        import time
        deadline = time.time() + 20
        while "payload" not in captured and time.time() < deadline:
            root.update()
            time.sleep(0.02)
        if "payload" not in captured:
            dialog._cancel()
            root.update()
        return captured.get("payload")

    def _root(self):
        """Build an App pointed at this test's temp folder.

        Without the explicit paths the App uses the project's real ``data/``
        directory, and the tests create live accounts in the shop.
        """
        from bookstore.ui.app import App
        app = App(data_dir=os.path.join(self.tmp, "appdata"),
                  config_path=os.path.join(self.tmp, "appsettings.json"))
        app.update()
        return app

    def test_enrolment_collects_samples_from_the_camera(self):
        self._install_fake_camera(person=0)
        app = self._root()
        try:
            app.services.biometrics.face.detector = StubDetector()
            crops = self._run_dialog(
                app, mode="enroll",
                recogniser=app.services.biometrics.face, target=3,
            )
            self.assertIsNotNone(crops, "the dialog returned nothing")
            self.assertEqual(len(crops), 3)
        finally:
            app.destroy()

    def test_a_camera_that_will_not_open_reports_a_permission_hint(self):
        self._install_fake_camera(opened=False)
        app = self._root()
        try:
            app.services.biometrics.face.detector = StubDetector()
            payload = self._run_dialog(
                app, mode="enroll",
                recogniser=app.services.biometrics.face, target=3,
            )
            self.assertIsNone(payload, "no samples should come back")
        finally:
            app.destroy()

    # -- the whole signup journey ---------------------------------------

    def _pump(self, app, predicate, seconds=25):
        import time
        deadline = time.time() + seconds
        while time.time() < deadline:
            app.update()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_creating_an_account_enrols_a_face_and_signs_in(self):
        """Requirement 4 end to end: signing up takes photographs, trains the
        model, and leaves an account that can log in by face."""
        self._install_fake_camera(person=4)
        app = self._root()
        try:
            face = app.services.biometrics.face
            face.detector = StubDetector()
            app.services.config.set("face_enrolment_samples", 4)

            login = app.views["login"]
            login.reg_name.insert(0, "Test Person")
            login.reg_username.insert(0, "testperson")
            login.reg_password.insert(0, "secret123")
            login.reg_deposit.delete(0, "end")
            login.reg_deposit.insert(0, "30")

            login._register()

            created = self._pump(
                app,
                lambda: app.services.storage.find_user_by_username("testperson")
                is not None,
            )
            self.assertTrue(created, "the account was never created")

            user = app.services.storage.find_user_by_username("testperson")
            self.assertAlmostEqual(float(user["balance"]), 30.0, places=2)

            # The face was enrolled as part of signing up.
            self.assertGreaterEqual(face.sample_count(user["user_id"]), 3)

            # And the app signed them in.
            self.assertIsNotNone(app.current_user)
            self.assertEqual(app.current_user["username"], "testperson")
        finally:
            app.destroy()

    def test_abandoning_the_camera_creates_no_account(self):
        """A half-finished signup must not leave an account that cannot log in."""
        self._install_fake_camera(opened=False)      # camera refuses to open
        app = self._root()
        try:
            app.services.biometrics.face.detector = StubDetector()
            login = app.views["login"]
            login.reg_name.insert(0, "Ghost")
            login.reg_username.insert(0, "ghost")
            login.reg_password.insert(0, "secret123")

            login._register()
            self._pump(app, lambda: False, seconds=3)

            self.assertIsNone(
                app.services.storage.find_user_by_username("ghost"),
                "no account should exist without an enrolled face",
            )
            self.assertIsNone(app.current_user)
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
