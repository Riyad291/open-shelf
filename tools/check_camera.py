"""Find out exactly why the camera is or is not working.

    python tools/check_camera.py
    python tools/check_camera.py --save test_frame.png
    python tools/check_camera.py --faces

Run this before blaming the application. It reports, in order:

  * whether the imaging libraries are installed at all;
  * every camera backend and device index it tried, and what each one did;
  * whether a frame was actually read, as opposed to the device merely
    claiming to be open -- on macOS a denied permission usually produces the
    latter, which is why "it opened" is not evidence of anything;
  * which face detector is active and whether it finds a face in a live frame;
  * the state of the enrolled face model.

The exit status is 0 when a frame was read and non-zero otherwise, so this can
be used in a script.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bookstore import cameras                      # noqa: E402


def check_face_pipeline(save_path: str = "", test_faces: bool = False) -> int:
    """Report on the detector and the trained model, optionally live."""
    try:
        from bookstore.biometrics import BiometricService, normalise_face
        from bookstore.storage import Storage
    except Exception as exc:
        print("Could not load the face pipeline: %s" % exc)
        return 1

    print()
    print("FACE PIPELINE")
    print("=" * 58)

    storage = Storage()
    service = BiometricService(storage)
    face = service.face

    print("Detector backend: %s" % face.backend_description)
    if face.backend_description.startswith("centre crop"):
        print("  ! No detector model is installed, so the system cannot")
        print("    actually find a face -- it just crops the middle of the")
        print("    frame. Fix with:  python tools/fetch_models.py")

    enrolled = face.enrolled_users()
    print("Enrolled accounts: %s" % (", ".join(enrolled) if enrolled else "none"))
    for user_id in enrolled:
        account = storage.get_user(user_id)
        name = account.get("username") if account else user_id
        print("    %-6s %-14s %d sample(s)"
              % (user_id, name, face.sample_count(user_id)))
    print("Model trained    : %s" % ("yes" if face.trained else "NO"))
    if not face.trained:
        print("  ! Nothing is enrolled, so face login cannot succeed for")
        print("    anybody. Create an account (which enrols a face), or use")
        print("    Manage store > Biometrics as the super-user.")

    if not (save_path or test_faces):
        return 0

    print()
    print("LIVE FRAME TEST")
    print("=" * 58)
    result = cameras.open_camera()
    if not result.ok:
        print("No camera, so no live test.")
        return 1

    try:
        import cv2
        frames_with_face = 0
        checked = 0
        best = None
        for _ in range(20):
            ok, frame = result.capture.read()
            if not ok or frame is None:
                continue
            checked += 1
            boxes = face.detector.detect(frame)
            real = boxes and not getattr(face.detector, "last_was_fallback", False)
            if real:
                frames_with_face += 1
                best = (frame, boxes[0])

        print("Frames examined  : %d" % checked)
        print("Frames with a detected face: %d" % frames_with_face)
        if frames_with_face == 0:
            print("  ! The camera works but no face was detected. Sit closer,")
            print("    face the camera straight on, and put the light in front")
            print("    of you rather than behind you.")
        elif face.trained and best is not None:
            frame, box = best
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            vector = normalise_face(gray, box)
            if vector is not None:
                identification = face.identify_vector(vector)
                print("Identification   : %s" % identification.message)

        if save_path and best is not None:
            frame, box = best
            x, y, w, h = box
            cv2.rectangle(frame, (x, y), (x + w, y + h), (80, 200, 120), 3)
            cv2.imwrite(save_path, frame)
            print("Saved a test frame to %s" % save_path)
        elif save_path:
            ok, frame = result.capture.read()
            if ok and frame is not None:
                cv2.imwrite(save_path, frame)
                print("Saved a test frame to %s (no face detected in it)"
                      % save_path)
    finally:
        result.release()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save", metavar="PATH", default="",
                        help="write one captured frame to this file")
    parser.add_argument("--faces", action="store_true",
                        help="also run face detection on live frames")
    args = parser.parse_args()

    report = cameras.diagnose()
    print(report)

    working = "RESULT: working" in report
    check_face_pipeline(save_path=args.save, test_faces=args.faces)

    print()
    if working:
        print("Camera verdict: WORKING.")
    else:
        print("Camera verdict: NOT WORKING — see the advice above.")
    return 0 if working else 2


if __name__ == "__main__":
    raise SystemExit(main())
