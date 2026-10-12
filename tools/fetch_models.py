"""Download the optional detector models into ``data/models/``.

    python tools/fetch_models.py            # YuNet + Haar cascade (small, fast)
    python tools/fetch_models.py --yolo     # also YOLOv8n for Shelf Scan
    python tools/fetch_models.py --all

Nothing here is required: without any model the face detector falls back to a
centre crop and Shelf Scan uses its own edge algorithm.  These just make
detection better.

Sizes: YuNet ~230 KB, Haar cascade ~930 KB, YOLOv8n ~6 MB.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bookstore.config import DATA_DIR  # noqa: E402

MODELS_DIR = os.path.join(DATA_DIR, "models")

# GitHub serves opencv_zoo's weights through Git LFS, so the media host is the
# one that returns actual bytes rather than a pointer file.
YUNET = (
    "face_detection_yunet.onnx",
    [
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
        "models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "https://raw.githubusercontent.com/opencv/opencv_zoo/main/"
        "models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    ],
    "YuNet DNN face detector",
)

HAAR = (
    "haarcascade_frontalface_default.xml",
    [
        "https://raw.githubusercontent.com/opencv/opencv/4.x/data/"
        "haarcascades/haarcascade_frontalface_default.xml",
    ],
    "Haar cascade face detector (fallback for OpenCV 4.x)",
)

YOLO = (
    "yolov8n.pt",
    [
        "https://github.com/ultralytics/assets/releases/download/v8.2.0/yolov8n.pt",
        "https://github.com/ultralytics/assets/releases/download/v8.1.0/yolov8n.pt",
    ],
    "YOLOv8n (COCO) -- used by Shelf Scan to detect books",
)

YOLO_FACE = (
    "yolov8n-face.pt",
    [
        "https://github.com/akanametov/yolo-face/releases/download/v0.0.0/"
        "yolov8n-face.pt",
    ],
    "YOLOv8n-face -- used for face detection when ultralytics is installed",
)


def download(name: str, urls, description: str, minimum_bytes: int = 2000) -> bool:
    target = os.path.join(MODELS_DIR, name)
    if os.path.exists(target) and os.path.getsize(target) > minimum_bytes:
        print("  already present : %s (%d KB)"
              % (name, os.path.getsize(target) // 1024))
        return True

    os.makedirs(MODELS_DIR, exist_ok=True)
    for url in urls:
        try:
            print("  downloading     : %s" % name, end="", flush=True)
            request = urllib.request.Request(
                url, headers={"User-Agent": "smart-bookstore-coursework/1.0"}
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
            if len(payload) < minimum_bytes:
                print("  -> too small, trying the next mirror")
                continue
            # A Git LFS pointer is a small text file, not the model.
            if payload[:40].startswith(b"version https://git-lfs"):
                print("  -> got an LFS pointer, trying the next mirror")
                continue
            with open(target, "wb") as handle:
                handle.write(payload)
            print("  -> ok (%d KB)" % (len(payload) // 1024))
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            print("  -> failed (%s)" % exc)
            continue

    print("  COULD NOT FETCH : %s -- %s" % (name, description))
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yolo", action="store_true",
                        help="also fetch YOLOv8n for Shelf Scan segmentation")
    parser.add_argument("--yolo-face", action="store_true",
                        help="also fetch YOLOv8n-face for face detection")
    parser.add_argument("--all", action="store_true", help="fetch everything")
    args = parser.parse_args()

    print("Fetching detector models into %s\n" % MODELS_DIR)

    wanted = [YUNET, HAAR]
    if args.yolo or args.all:
        wanted.append(YOLO)
    if args.yolo_face or args.all:
        wanted.append(YOLO_FACE)

    results = []
    for name, urls, description in wanted:
        print("%s:" % description)
        results.append(download(name, urls, description))
        print()

    if args.yolo or args.yolo_face or args.all:
        try:
            import ultralytics  # noqa: F401
        except ImportError:
            print("Note: the YOLO weights are only used when the 'ultralytics' "
                  "package is installed.\n      pip install ultralytics\n")

    ok = sum(1 for r in results if r)
    print("Fetched %d of %d model(s)." % (ok, len(results)))
    if ok < len(results):
        print("The app still runs without the missing ones -- it falls back to "
              "a simpler detector and says so on the Biometrics page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
