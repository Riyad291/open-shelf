"""Shared Tkinter widgets and the visual theme.

Keeping the theme in one place is what stops a Tkinter app from looking like a
default Tk app: every colour, font and padding value used anywhere in the GUI
comes from :class:`Theme`.
"""

from __future__ import annotations

import os
from collections import OrderedDict
import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional

try:
    from PIL import Image, ImageTk
except ImportError:  # pragma: no cover
    Image = None  # type: ignore
    ImageTk = None  # type: ignore


class Theme:
    """Single source of truth for the look of the app."""

    BG = "#f4f1ea"            # warm paper
    SURFACE = "#ffffff"
    SURFACE_ALT = "#ece7dc"
    INK = "#23201c"
    INK_SOFT = "#5d574d"
    INK_FAINT = "#8c857a"
    ACCENT = "#7a2e28"        # oxblood
    ACCENT_DARK = "#5c211d"
    ACCENT_SOFT = "#f0e2de"
    GOOD = "#2f6b45"
    WARN = "#9a6213"
    BAD = "#9c2f25"
    BORDER = "#d8d1c3"

    ROLE_COLOURS = {
        "SU": "#5c3a86",
        "VC": "#9a7016",
        "RC": "#2f5d7a",
        "VS": "#6d665b",
    }

    @staticmethod
    def fonts(root: tk.Misc) -> dict:
        """Pick the best available serif/sans pair on this machine."""
        families = set(tk.font.families(root)) if hasattr(tk, "font") else set()
        try:
            from tkinter import font as tkfont
            families = set(tkfont.families(root))
        except Exception:
            families = set()

        def pick(candidates, fallback):
            for name in candidates:
                if name in families:
                    return name
            return fallback

        serif = pick(["Georgia", "Palatino", "Palatino Linotype", "Book Antiqua",
                      "DejaVu Serif", "Times New Roman"], "Times")
        sans = pick(["Inter", "Segoe UI", "Helvetica Neue", "SF Pro Text",
                     "DejaVu Sans", "Arial"], "Helvetica")
        mono = pick(["SF Mono", "Consolas", "Menlo", "DejaVu Sans Mono",
                     "Courier New"], "Courier")
        return {"serif": serif, "sans": sans, "mono": mono}

    @classmethod
    def apply(cls, root: tk.Misc) -> dict:
        """Configure ttk styles.  Returns the chosen font families."""
        families = cls.fonts(root)
        serif, sans, mono = families["serif"], families["sans"], families["mono"]

        style = ttk.Style(root)
        for candidate in ("clam", "alt", "default"):
            try:
                style.theme_use(candidate)
                break
            except tk.TclError:
                continue

        root.configure(bg=cls.BG)

        style.configure(".", background=cls.BG, foreground=cls.INK,
                        font=(sans, 10))
        style.configure("TFrame", background=cls.BG)
        style.configure("Surface.TFrame", background=cls.SURFACE)
        style.configure("Alt.TFrame", background=cls.SURFACE_ALT)
        style.configure("Card.TFrame", background=cls.SURFACE, relief="flat")

        style.configure("TLabel", background=cls.BG, foreground=cls.INK)
        style.configure("Surface.TLabel", background=cls.SURFACE, foreground=cls.INK)
        style.configure("Alt.TLabel", background=cls.SURFACE_ALT, foreground=cls.INK)
        style.configure("H1.TLabel", font=(serif, 26, "bold"), foreground=cls.INK,
                        background=cls.BG)
        style.configure("H2.TLabel", font=(serif, 17, "bold"), foreground=cls.INK,
                        background=cls.BG)
        style.configure("H3.TLabel", font=(sans, 11, "bold"), foreground=cls.INK,
                        background=cls.SURFACE)
        style.configure("Muted.TLabel", font=(sans, 9), foreground=cls.INK_SOFT,
                        background=cls.BG)
        style.configure("SurfaceMuted.TLabel", font=(sans, 9),
                        foreground=cls.INK_SOFT, background=cls.SURFACE)
        style.configure("Price.TLabel", font=(sans, 13, "bold"),
                        foreground=cls.ACCENT, background=cls.SURFACE)
        style.configure("Good.TLabel", foreground=cls.GOOD, background=cls.BG)
        style.configure("Bad.TLabel", foreground=cls.BAD, background=cls.BG)
        style.configure("Warn.TLabel", foreground=cls.WARN, background=cls.BG)

        style.configure("TButton", font=(sans, 10), padding=(12, 6),
                        background=cls.SURFACE_ALT, foreground=cls.INK,
                        borderwidth=0, focuscolor=cls.BG)
        style.map("TButton",
                  background=[("active", cls.BORDER), ("disabled", cls.SURFACE_ALT)],
                  foreground=[("disabled", cls.INK_FAINT)])

        style.configure("Accent.TButton", background=cls.ACCENT,
                        foreground="#ffffff", font=(sans, 10, "bold"),
                        padding=(14, 7), borderwidth=0)
        style.map("Accent.TButton",
                  background=[("active", cls.ACCENT_DARK),
                              ("disabled", cls.BORDER)],
                  foreground=[("disabled", cls.INK_FAINT)])

        style.configure("Link.TButton", background=cls.SURFACE,
                        foreground=cls.ACCENT, font=(sans, 9, "underline"),
                        padding=(2, 2), borderwidth=0)
        style.map("Link.TButton", background=[("active", cls.SURFACE)])

        style.configure("TEntry", fieldbackground=cls.SURFACE,
                        foreground=cls.INK, borderwidth=1, padding=6)
        style.configure("TCombobox", fieldbackground=cls.SURFACE,
                        background=cls.SURFACE, padding=4)

        style.configure("TNotebook", background=cls.BG, borderwidth=0,
                        tabmargins=(8, 6, 8, 0))
        style.configure("TNotebook.Tab", font=(sans, 10), padding=(16, 8),
                        background=cls.SURFACE_ALT, foreground=cls.INK_SOFT,
                        borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", cls.SURFACE)],
                  foreground=[("selected", cls.ACCENT)],
                  font=[("selected", (sans, 10, "bold"))])

        style.configure("Treeview", background=cls.SURFACE,
                        fieldbackground=cls.SURFACE, foreground=cls.INK,
                        rowheight=24, borderwidth=0, font=(sans, 9))
        style.configure("Treeview.Heading", font=(sans, 9, "bold"),
                        background=cls.SURFACE_ALT, foreground=cls.INK,
                        relief="flat")
        style.map("Treeview", background=[("selected", cls.ACCENT_SOFT)],
                  foreground=[("selected", cls.INK)])

        style.configure("TSeparator", background=cls.BORDER)
        style.configure("Horizontal.TProgressbar", background=cls.ACCENT,
                        troughcolor=cls.SURFACE_ALT, borderwidth=0)
        style.configure("TLabelframe", background=cls.BG, foreground=cls.INK,
                        borderwidth=1, relief="solid")
        style.configure("TLabelframe.Label", background=cls.BG,
                        foreground=cls.INK_SOFT, font=(sans, 9, "bold"))
        style.configure("TCheckbutton", background=cls.BG, foreground=cls.INK)
        style.configure("TRadiobutton", background=cls.BG, foreground=cls.INK)

        return families


# ---------------------------------------------------------------------------
# Scrollable container
# ---------------------------------------------------------------------------


class ScrollFrame(ttk.Frame):
    """A frame that scrolls vertically, with a working mouse wheel."""

    def __init__(self, master, style_name: str = "TFrame", **kwargs):
        super().__init__(master, **kwargs)
        self.canvas = tk.Canvas(self, bg=Theme.BG, highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical",
                                       command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas, style=style_name)

        self._window = self.canvas.create_window((0, 0), window=self.body,
                                                 anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self._bind_wheel()

    def _on_body_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self) -> None:
        def on_enter(_e):
            self.canvas.bind_all("<MouseWheel>", self._on_wheel)
            self.canvas.bind_all("<Button-4>", self._on_wheel)
            self.canvas.bind_all("<Button-5>", self._on_wheel)

        def on_leave(_e):
            self.canvas.unbind_all("<MouseWheel>")
            self.canvas.unbind_all("<Button-4>")
            self.canvas.unbind_all("<Button-5>")

        self.canvas.bind("<Enter>", on_enter)
        self.canvas.bind("<Leave>", on_leave)

    def _on_wheel(self, event) -> None:
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta, "units")

    def clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0.0)


# ---------------------------------------------------------------------------
# Small building blocks
# ---------------------------------------------------------------------------


def hline(master, pady: int = 10) -> ttk.Separator:
    sep = ttk.Separator(master, orient="horizontal")
    sep.pack(fill="x", pady=pady)
    return sep


def stars_text(value: float, count: Optional[int] = None) -> str:
    """Render a rating as filled/empty stars plus the numeric value."""
    if count is not None and count == 0:
        return "no ratings yet"
    filled = int(round(value))
    filled = max(0, min(5, filled))
    bar = "★" * filled + "☆" * (5 - filled)
    if count is None:
        return "%s  %.1f" % (bar, value)
    return "%s  %.1f  (%d rating%s)" % (bar, value, count,
                                        "" if count == 1 else "s")


class StarPicker(ttk.Frame):
    """Clickable 1-5 star selector."""

    def __init__(self, master, initial: int = 0, on_change: Optional[Callable] = None,
                 background: str = Theme.SURFACE):
        super().__init__(master)
        self.value = initial
        self.on_change = on_change
        self._bg = background
        self._labels: List[tk.Label] = []
        for i in range(1, 6):
            label = tk.Label(self, text="☆", font=("Helvetica", 20),
                             bg=background, fg=Theme.INK_FAINT, cursor="hand2",
                             bd=0, padx=2)
            label.pack(side="left")
            label.bind("<Button-1>", lambda _e, n=i: self.set(n))
            label.bind("<Enter>", lambda _e, n=i: self._preview(n))
            label.bind("<Leave>", lambda _e: self._render())
            self._labels.append(label)
        self._render()

    def _preview(self, n: int) -> None:
        for i, label in enumerate(self._labels, start=1):
            label.configure(text="★" if i <= n else "☆",
                            fg=Theme.WARN if i <= n else Theme.INK_FAINT)

    def _render(self) -> None:
        for i, label in enumerate(self._labels, start=1):
            label.configure(text="★" if i <= self.value else "☆",
                            fg=Theme.ACCENT if i <= self.value else Theme.INK_FAINT)

    def set(self, n: int) -> None:
        self.value = max(0, min(5, int(n)))
        self._render()
        if self.on_change:
            self.on_change(self.value)

    def get(self) -> int:
        return self.value


class Badge(tk.Label):
    """A small coloured pill, used for roles and list status."""

    def __init__(self, master, text: str, colour: str = Theme.INK_SOFT,
                 background: Optional[str] = None, font_family: str = "Helvetica"):
        super().__init__(
            master, text=" %s " % text, bg=background or colour,
            fg="#ffffff", font=(font_family, 8, "bold"), padx=6, pady=2, bd=0,
        )


class CoverImage:
    """Loads and caches cover thumbnails, with a drawn placeholder fallback.

    The cache is bounded and evicts oldest-first.  Tk ``PhotoImage`` objects are
    only freed when the last Python reference goes, so an unbounded dict here
    would hold every thumbnail at every size for the life of the session.
    """

    _cache: "OrderedDict" = OrderedDict()
    MAX_CACHED = 240

    @classmethod
    def clear(cls) -> None:
        """Drop every cached image.

        A ``PhotoImage`` belongs to the Tk interpreter that created it, so a
        cache entry outliving its root raises ``image "pyimageN" doesn't exist``
        the next time it is used.  The app clears the cache when it starts, so
        a second App() in one process (tests, or a restart) is safe.
        """
        cls._cache.clear()

    @classmethod
    def load(cls, path: str, size=(96, 144), project_root: str = "",
             title: str = "", fallback_colour: str = Theme.SURFACE_ALT):
        if ImageTk is None:
            return None
        key = (path, size, title)
        if key in cls._cache:
            cls._cache.move_to_end(key)
            return cls._cache[key]

        resolved = ""
        if path:
            candidates = [path]
            if project_root:
                candidates.append(os.path.join(project_root, path))
            for candidate in candidates:
                if candidate and os.path.exists(candidate):
                    resolved = candidate
                    break

        try:
            if resolved:
                img = Image.open(resolved).convert("RGB")
                img.thumbnail(size, Image.LANCZOS)
            else:
                img = cls._placeholder(size, title, fallback_colour)
            photo = ImageTk.PhotoImage(img)
        except Exception:
            return None

        cls._cache[key] = photo
        while len(cls._cache) > cls.MAX_CACHED:
            cls._cache.popitem(last=False)
        return photo

    @staticmethod
    def _placeholder(size, title: str, colour: str):
        from PIL import ImageDraw
        img = Image.new("RGB", size, colour)
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, size[0] - 1, size[1] - 1], outline="#c6bda9")
        words = (title or "No cover").split()[:4]
        y = max(6, size[1] // 2 - len(words) * 7)
        for word in words:
            draw.text((6, y), word[:14], fill="#6d665b")
            y += 13
        return img


def toast(root: tk.Misc, message: str, kind: str = "info", ms: int = 3200) -> None:
    """A transient message in the bottom-right corner."""
    colour = {"info": Theme.INK, "good": Theme.GOOD,
              "warn": Theme.WARN, "bad": Theme.BAD}.get(kind, Theme.INK)
    top = tk.Toplevel(root)
    top.overrideredirect(True)
    top.configure(bg=colour)
    try:
        top.attributes("-topmost", True)
    except tk.TclError:
        pass

    label = tk.Label(top, text=message, bg=colour, fg="#ffffff",
                     font=("Helvetica", 10), padx=16, pady=10,
                     wraplength=380, justify="left")
    label.pack()

    root.update_idletasks()
    width = top.winfo_reqwidth()
    height = top.winfo_reqheight()
    x = root.winfo_rootx() + root.winfo_width() - width - 28
    y = root.winfo_rooty() + root.winfo_height() - height - 28
    top.geometry("+%d+%d" % (max(0, x), max(0, y)))
    top.after(ms, top.destroy)


class ReadOnlyText(tk.Text):
    """Text widget for output: selectable and copyable, but not editable."""

    def __init__(self, master, **kwargs):
        kwargs.setdefault("bg", Theme.SURFACE)
        kwargs.setdefault("fg", Theme.INK)
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("wrap", "word")
        kwargs.setdefault("padx", 14)
        kwargs.setdefault("pady", 12)
        kwargs.setdefault("insertwidth", 0)
        super().__init__(master, **kwargs)
        self.configure(state="disabled")

    def set_text(self, text: str) -> None:
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.insert("1.0", text)
        self.configure(state="disabled")

    def append(self, text: str, tag: Optional[str] = None) -> None:
        self.configure(state="normal")
        if tag:
            self.insert("end", text, tag)
        else:
            self.insert("end", text)
        self.configure(state="disabled")
        self.see("end")
