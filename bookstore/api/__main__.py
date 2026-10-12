"""Run the HTTP server.

    python -m bookstore.api
    python -m bookstore.api --host 0.0.0.0 --port 8080 --reload
"""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true",
                        help="restart on code changes (development)")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("The web server needs uvicorn and fastapi:\n\n"
              "    pip install -r requirements.txt\n")
        return 2

    print("Smart Bookstore")
    print("  web app  : http://%s:%d/app/" % (args.host, args.port))
    print("  API docs : http://%s:%d/docs" % (args.host, args.port))
    print()

    uvicorn.run("bookstore.api.app:app", host=args.host, port=args.port,
                reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
