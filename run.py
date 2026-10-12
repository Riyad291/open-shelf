#!/usr/bin/env python3
"""Launch the smart bookstore.

    python run.py

If this is the first run, seed the demo data first:

    python tools/seed.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print(
            "Tkinter is not available for this Python.\n\n"
            "  Ubuntu/Debian : sudo apt install python3-tk\n"
            "  Fedora        : sudo dnf install python3-tkinter\n"
            "  macOS (brew)  : brew install python-tk\n"
            "  Windows       : reinstall Python with the 'tcl/tk' option ticked\n",
            file=sys.stderr,
        )
        return 2

    from bookstore.config import ensure_dirs
    from bookstore.storage import Storage

    ensure_dirs()
    storage = Storage()
    if not storage.users and not storage.books:
        print(
            "The store is empty. Populate it with demo data first:\n\n"
            "    python tools/seed.py\n\n"
            "Starting anyway -- you can add books from the Manage store tab "
            "once you create a super-user account.\n"
        )

    from bookstore.ui.app import App

    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
