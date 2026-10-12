"""Application shell: service wiring, session state and the tab layout.

The visible tabs depend on who is signed in, which is how spec requirement 1's
"different privileges" shows up in the interface rather than only in the rules.
"""

from __future__ import annotations

import os
import queue
import tkinter as tk
import traceback
from tkinter import ttk
from typing import Optional

from .. import models
from ..biometrics import BiometricService
from ..catalog import Catalog
from ..chat import ChatService
from ..config import Config, PROJECT_ROOT, ensure_dirs
from ..finance import FinanceService
from ..imagesearch import ImageIndex
from ..llm import OllamaClient
from ..rag import Retriever
from ..recommend import Recommender
from ..rules import RulesEngine
from ..shelfscan import ShelfScanner
from ..storage import Storage
from .widgets import CoverImage, Theme, toast


class Services:
    """Everything the views need, constructed once.

    ``data_dir`` and ``config_path`` default to the real project folders.  They
    exist so a test can point a whole App at a temporary directory instead of
    writing into the shop's live data -- without them, running the GUI tests
    creates real accounts.
    """

    def __init__(self, data_dir: Optional[str] = None,
                 config_path: Optional[str] = None):
        ensure_dirs()
        self.config = Config(config_path) if config_path else Config()
        self.storage = Storage(data_dir) if data_dir else Storage()
        self.rules = RulesEngine(self.storage, self.config)
        self.catalog = Catalog(self.storage)
        self.recommender = Recommender(self.storage, self.rules)
        self.retriever = Retriever(self.storage)
        self.llm = OllamaClient(self.config)
        self.finance = FinanceService(self.storage)
        self.chat = ChatService(
            self.storage, self.config, self.rules, self.catalog,
            self.recommender, self.retriever, self.llm,
        )
        self.images = ImageIndex(self.storage)
        self.biometrics = BiometricService(self.storage)
        self.shelfscan = ShelfScanner(self.images, self.recommender, self.rules)
        self.project_root = PROJECT_ROOT


class App(tk.Tk):
    def __init__(self, data_dir: Optional[str] = None,
                 config_path: Optional[str] = None):
        super().__init__()
        self.title("Inkwell & Co. -- Smart Bookstore")
        self.geometry("1180x820")
        self.minsize(980, 660)

        self.services = Services(data_dir=data_dir, config_path=config_path)
        self.fonts = Theme.apply(self)
        # Cached PhotoImages belong to a Tk interpreter; a stale one from a
        # previous root would raise 'image "pyimageN" doesn't exist'.
        CoverImage.clear()
        self.current_user: Optional[dict] = None
        #: Set on window close so late worker callbacks are dropped rather than
        #: crashing their thread.
        self._closing = False
        #: Work handed back from background threads, drained on the Tk thread.
        #: See post() for why this is a queue rather than a direct after().
        self._from_workers: "queue.Queue" = queue.Queue()
        #: Handle of the pending queue poll, so close can cancel it.
        self._poll_id: Optional[str] = None

        self._build_chrome()
        self._build_body()
        self.refresh_all()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._drain_worker_queue()

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    @property
    def role(self) -> str:
        return (self.current_user or {}).get("role", models.VISITOR)

    @property
    def is_visitor(self) -> bool:
        return self.current_user is None

    def log_in(self, user: dict, how: str = "password") -> None:
        self.current_user = user
        self.services.storage.put_user(user)
        toast(self, "Welcome back, %s. Signed in as %s via %s."
              % (user.get("display_name") or user.get("username"),
                 models.role_name(user.get("role", "")), how),
              kind="good")
        self.rebuild_tabs()
        self.refresh_all()

    def log_out(self) -> None:
        self.current_user = None
        toast(self, "Signed out. You are browsing as a visitor.", kind="info")
        self.rebuild_tabs()
        self.refresh_all()

    def reload_user(self) -> None:
        """Re-read the signed-in account after a rule changed it."""
        if self.current_user:
            fresh = self.services.storage.get_user(self.current_user["user_id"])
            if fresh:
                self.current_user = models.normalise_user(fresh)

    # ------------------------------------------------------------------
    # Chrome: header and status bar
    # ------------------------------------------------------------------

    def _build_chrome(self) -> None:
        header = tk.Frame(self, bg=Theme.ACCENT, height=74)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        left = tk.Frame(header, bg=Theme.ACCENT)
        left.pack(side="left", padx=22, pady=10)
        tk.Label(left, text="Inkwell & Co.", bg=Theme.ACCENT, fg="#ffffff",
                 font=(self.fonts["serif"], 21, "bold")).pack(anchor="w")
        tk.Label(left, text="an AI-enabled multimodal bookstore",
                 bg=Theme.ACCENT, fg="#e7cfcb",
                 font=(self.fonts["sans"], 9)).pack(anchor="w")

        right = tk.Frame(header, bg=Theme.ACCENT)
        right.pack(side="right", padx=22)

        self.user_label = tk.Label(right, text="", bg=Theme.ACCENT, fg="#ffffff",
                                   font=(self.fonts["sans"], 10, "bold"),
                                   justify="right")
        self.user_label.pack(side="top", anchor="e", pady=(16, 0))

        self.balance_label = tk.Label(right, text="", bg=Theme.ACCENT,
                                      fg="#e7cfcb",
                                      font=(self.fonts["sans"], 9),
                                      justify="right")
        self.balance_label.pack(side="top", anchor="e")

        self.auth_button = tk.Button(
            header, text="Sign in", command=self._auth_button_clicked,
            bg="#ffffff", fg=Theme.ACCENT, activebackground="#f0e2de",
            activeforeground=Theme.ACCENT_DARK, bd=0, padx=16, pady=6,
            font=(self.fonts["sans"], 10, "bold"), cursor="hand2",
        )
        self.auth_button.pack(side="right", padx=(0, 14))

        # Status bar.
        status = tk.Frame(self, bg=Theme.SURFACE_ALT, height=26)
        status.pack(fill="x", side="bottom")
        status.pack_propagate(False)
        self.status_label = tk.Label(
            status, text="", bg=Theme.SURFACE_ALT, fg=Theme.INK_SOFT,
            font=(self.fonts["sans"], 9), anchor="w", padx=14,
        )
        self.status_label.pack(side="left", fill="both", expand=True)

    def _auth_button_clicked(self) -> None:
        if self.is_visitor:
            self.notebook.select(self.tab_frames["login"])
        else:
            self.log_out()

    def set_status(self, text: str) -> None:
        self.status_label.configure(text=text)
        self.update_idletasks()

    def refresh_status(self) -> None:
        # The picture index is built lazily on first use, so say so rather than
        # reporting a bare "0" that reads like a failure.
        indexed = self.services.images.size
        covers = ("covers indexed %d" % indexed if indexed
                  else "covers indexed on first search")

        bits = [
            "%d books" % len(self.services.storage.books),
            "%d accounts" % len(self.services.storage.users),
            "KB %d chunks" % self.services.retriever.size,
            covers,
            self.services.biometrics.status_line(),
        ]
        ok, _ = self.services.llm.status()
        bits.append("Ollama %s" % ("ready" if ok else "offline"))
        self.set_status("   |   ".join(bits))

    # ------------------------------------------------------------------
    # Body: notebook
    # ------------------------------------------------------------------

    def _build_body(self) -> None:
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True, padx=14, pady=(10, 6))

        self.notebook = ttk.Notebook(container)
        self.notebook.pack(fill="both", expand=True)
        self.tab_frames: dict = {}
        self.views: dict = {}
        self._create_views()
        self.rebuild_tabs()
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _create_views(self) -> None:
        # Imported here to avoid a circular import at module load.
        from .login import LoginView
        from .shop import BrowseView, HomeView, SearchView
        from .panels import AccountView, ChatView, ShelfScanView
        from .admin import AdminView

        self.views["home"] = HomeView(self.notebook, self)
        self.views["browse"] = BrowseView(self.notebook, self)
        self.views["search"] = SearchView(self.notebook, self)
        self.views["chat"] = ChatView(self.notebook, self)
        self.views["shelfscan"] = ShelfScanView(self.notebook, self)
        self.views["account"] = AccountView(self.notebook, self)
        self.views["admin"] = AdminView(self.notebook, self)
        self.views["login"] = LoginView(self.notebook, self)

        for key, view in self.views.items():
            self.tab_frames[key] = view

    def rebuild_tabs(self) -> None:
        """Show only the tabs this role may use (requirement 1)."""
        for key in list(self.tab_frames):
            try:
                self.notebook.forget(self.tab_frames[key])
            except tk.TclError:
                pass

        layout = [("home", "Home"), ("browse", "Browse"), ("search", "Search"),
                  ("chat", "Assistant"), ("shelfscan", "Shelf Scan")]
        if self.role in (models.REGISTERED, models.VIP):
            layout.append(("account", "My account"))
        if self.role == models.SUPER_USER:
            layout.append(("admin", "Manage store"))
        if self.is_visitor:
            layout.append(("login", "Sign in"))

        for key, label in layout:
            self.notebook.add(self.tab_frames[key], text=label)

        # Header state.
        if self.is_visitor:
            self.user_label.configure(text="Browsing as a visitor")
            self.balance_label.configure(text="Sign in for recommendations and purchases")
            self.auth_button.configure(text="Sign in")
        else:
            user = self.current_user or {}
            self.user_label.configure(
                text="%s  -  %s" % (user.get("display_name") or user.get("username"),
                                    models.role_name(user.get("role", "")))
            )
            extra = ""
            if user.get("role") == models.VIP:
                extra = "   |   %d%% VIP discount" % int(self.services.config.vip_discount * 100)
            self.balance_label.configure(
                text="Balance $%.2f%s" % (float(user.get("balance", 0.0)), extra)
            )
            self.auth_button.configure(text="Sign out")

    def _on_tab_changed(self, _event=None) -> None:
        try:
            current = self.notebook.select()
        except tk.TclError:
            return
        for key, frame in self.tab_frames.items():
            if str(frame) == str(current):
                view = self.views.get(key)
                if view is not None and hasattr(view, "refresh"):
                    view.refresh()
                break

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh_all(self) -> None:
        self.reload_user()
        for view in self.views.values():
            if hasattr(view, "refresh"):
                try:
                    view.refresh()
                except Exception as exc:  # pragma: no cover - defensive
                    print("Refresh failed for %s: %s" % (type(view).__name__, exc))
        self.refresh_status()

    def open_book(self, book: dict) -> None:
        """Jump to the book detail page, recording the browse (requirement 2)."""
        self.services.rules.record_browse(self.current_user, book)
        self.reload_user()
        view = self.views["browse"]
        self.notebook.select(self.tab_frames["browse"])
        view.show_book(book)

    # ------------------------------------------------------------------
    # Threading
    # ------------------------------------------------------------------

    #: How often the Tk thread looks for work handed back by a worker. 40ms is
    #: below the threshold where a person notices a delay, and costs nothing.
    WORKER_POLL_MS = 40

    def post(self, callback) -> None:
        """Hand ``callback`` to the Tk thread from a background worker.

        Background workers (biometrics, the chat box, Shelf Scan) finish by
        calling this.

        It does *not* call ``after()`` directly, even though that reads more
        naturally. Tk is not thread-safe, and touching it from another thread
        raises ``RuntimeError: main thread is not in main loop`` -- sometimes.
        Whether it raises depends on how the interpreter was built and on
        whether the main loop happens to be running at that instant, which is
        the worst kind of bug: it works on the machine you wrote it on and
        hangs on the examiner's.

        An earlier version did call ``after()`` here and swallowed that
        exception. The result was not a crash but something worse -- the
        worker finished, its result was dropped, and the window sat with a
        spinner forever, because the code that clears the spinner was inside
        the callback that never ran.

        So the callback goes on a queue instead. ``queue.Queue`` is
        thread-safe by design, and ``_drain_worker_queue`` below empties it
        on the Tk thread, where touching widgets is legal.
        """
        if self._closing:
            return
        self._from_workers.put(callback)

    def pump_workers(self) -> int:
        """Run whatever the workers left, on the Tk thread. Never raises.

        Returns how many callbacks ran, which is what the tests assert on.
        """
        ran = 0
        while True:
            try:
                callback = self._from_workers.get_nowait()
            except queue.Empty:
                break
            if self._closing:
                continue
            ran += 1
            try:
                callback()
            except Exception:
                # One failed callback must not stop the poller: if this loop
                # dies, every future background result is lost silently and
                # the window appears to hang -- the exact failure this whole
                # mechanism exists to prevent.
                traceback.print_exc()
        return ran

    def _drain_worker_queue(self) -> None:
        self._poll_id = None
        self.pump_workers()
        if self._closing:
            return
        try:
            self._poll_id = self.after(self.WORKER_POLL_MS,
                                       self._drain_worker_queue)
        except (RuntimeError, tk.TclError):
            pass

    def _stop_polling(self) -> None:
        """Cancel the pending poll so Tk does not try to call a method on a
        window that no longer exists ('invalid command name ...')."""
        poll_id, self._poll_id = getattr(self, "_poll_id", None), None
        if poll_id is not None:
            try:
                self.after_cancel(poll_id)
            except (RuntimeError, tk.TclError):
                pass

    def destroy(self) -> None:
        self._closing = True
        self._stop_polling()
        super().destroy()

    def _on_close(self) -> None:
        self._closing = True
        try:
            self.services.storage.save_all()
        except Exception:
            pass
        self.destroy()


def main() -> int:
    app = App()
    app.mainloop()
    return 0
