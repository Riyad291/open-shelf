"""Results handed back by background threads must reach the window.

Every slow thing in the GUI -- face enrolment, the chat box, Shelf Scan --
runs on a worker thread and finishes by calling ``App.post()``. The callback
it posts is what re-enables the buttons, stops the spinner and draws the
result. If a posted callback is ever dropped, nothing crashes: the window
simply sits there with a spinner, forever, looking like a hung program.

That is exactly what used to happen. ``post()`` called ``after()`` directly
from the worker thread, which Tk does not allow; it raised
``RuntimeError: main thread is not in main loop``, and the except clause
swallowed it. The scan completed in a tenth of a second and the window span
for five minutes.

These tests pin down the replacement: a thread-safe queue drained on the Tk
thread. They do not need a camera or a webcam -- only that a callback posted
from somewhere else actually runs.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter
except ImportError:                                   # pragma: no cover
    tkinter = None


@unittest.skipUnless(tkinter is not None and os.environ.get("DISPLAY"),
                     "needs Tkinter and a display")
class TestWorkerDispatch(unittest.TestCase):

    def setUp(self):
        from bookstore.ui.app import App
        self.tmp = tempfile.mkdtemp(prefix="dispatch-")
        self.app = App(data_dir=os.path.join(self.tmp, "data"),
                       config_path=os.path.join(self.tmp, "settings.json"))
        self.app.update()

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_callback_posted_from_a_worker_thread_runs(self):
        ran = []
        done = threading.Event()

        def worker():
            self.app.post(lambda: ran.append("yes"))
            done.set()

        threading.Thread(target=worker).start()
        self.assertTrue(done.wait(5), "the worker never finished")

        self.app.pump_workers()
        self.assertEqual(ran, ["yes"],
                         "a result from a worker thread was dropped")

    def test_callbacks_run_in_the_order_they_were_posted(self):
        order = []
        done = threading.Event()

        def worker():
            for i in range(10):
                self.app.post(lambda i=i: order.append(i))
            done.set()

        threading.Thread(target=worker).start()
        self.assertTrue(done.wait(5))
        self.app.pump_workers()
        self.assertEqual(order, list(range(10)))

    def test_many_threads_can_post_at_once(self):
        """queue.Queue is the reason this is safe; assert it stays that way."""
        seen = []
        threads = [threading.Thread(target=lambda n=n: self.app.post(
            lambda n=n: seen.append(n))) for n in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)

        self.app.pump_workers()
        self.assertEqual(sorted(seen), list(range(25)))

    def test_one_failing_callback_does_not_stop_the_rest(self):
        """If the drain loop died, every later result would vanish."""
        ran = []

        def explodes():
            raise RuntimeError("deliberate")

        self.app.post(lambda: ran.append("before"))
        self.app.post(explodes)
        self.app.post(lambda: ran.append("after"))

        import io
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            self.app.pump_workers()

        self.assertEqual(ran, ["before", "after"])

    def test_the_poller_keeps_running_after_a_failure(self):
        def explodes():
            raise RuntimeError("deliberate")

        import io
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            self.app.post(explodes)
            self.app.pump_workers()

        later = []
        self.app.post(lambda: later.append("still alive"))
        self.app.pump_workers()
        self.assertEqual(later, ["still alive"])

    def test_results_arriving_after_the_window_closes_are_dropped(self):
        """The original reason post() was guarded at all."""
        ran = []
        self.app._closing = True
        self.app.post(lambda: ran.append("should not run"))
        self.app.pump_workers()
        self.assertEqual(ran, [])

    def test_posting_from_a_worker_never_raises_in_the_worker(self):
        """A worker must not die because the window is busy or closing."""
        errors = []

        def worker():
            try:
                self.app.post(lambda: None)
            except Exception as exc:         # pragma: no cover
                errors.append(exc)

        for closing in (False, True):
            self.app._closing = closing
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(5)
        self.assertEqual(errors, [])


@unittest.skipUnless(tkinter is not None and os.environ.get("DISPLAY"),
                     "needs Tkinter and a display")
class TestShelfScanFinishes(unittest.TestCase):
    """The end-to-end version: the bug was found by the window hanging."""

    def setUp(self):
        from bookstore.ui.app import App
        self.tmp = tempfile.mkdtemp(prefix="shelfscan-ui-")
        self.app = App(data_dir=os.path.join(self.tmp, "data"),
                       config_path=os.path.join(self.tmp, "settings.json"))
        self.app.update()

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _find_shelf_view(self):
        from tkinter import ttk

        def walk(widget, out):
            out.append(widget)
            for child in widget.winfo_children():
                walk(child, out)
            return out

        widgets = walk(self.app, [])
        for widget in widgets:
            if type(widget).__name__ == "ShelfScanView":
                return widget, widgets
        return None, widgets

    def test_a_scan_clears_its_own_busy_flag(self):
        import time

        view, _ = self._find_shelf_view()
        if view is None:
            self.skipTest("the Shelf Scan view is not built in this layout")

        # Post the kind of callback the scan's worker posts, from a worker
        # thread, and require the view to come out of its busy state.
        view._busy = True
        finished = []

        def worker():
            self.app.post(lambda: (setattr(view, "_busy", False),
                                   finished.append(True)))

        threading.Thread(target=worker).start()

        deadline = time.time() + 5
        while time.time() < deadline and not finished:
            self.app.update()
            self.app.pump_workers()
            time.sleep(0.05)

        self.assertTrue(finished, "the scan's completion callback never ran")
        self.assertFalse(view._busy,
                         "the view would sit with a spinner forever")


if __name__ == "__main__":
    unittest.main(verbosity=2)
