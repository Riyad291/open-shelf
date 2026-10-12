"""The assistant, the customer account page and Shelf Scan.

Covers spec requirements 3 and 13 (the role-tiered chat box with its RAG/LLM
labelling), 6 (account balance and its consequences) and 14 (the creative
feature).
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Optional

from .. import models
from ..chat import SOURCE_DENIED, SOURCE_ERROR, SOURCE_LLM, SOURCE_RAG, SOURCE_STORE
from .widgets import (Badge, CoverImage, ReadOnlyText, ScrollFrame, Theme,
                      hline, toast)


# ===========================================================================
# Chat
# ===========================================================================


class ChatView(ttk.Frame):
    """Requirement 3 and 13: one box, answers scoped by role and labelled by source."""

    SOURCE_STYLE = {
        SOURCE_STORE: ("store data", Theme.ACCENT),
        SOURCE_RAG: ("via RAG", "#2f6b45"),
        SOURCE_LLM: ("via LLM", "#2f5d7a"),
        SOURCE_DENIED: ("access denied", "#9a6213"),
        SOURCE_ERROR: ("unavailable", "#9c2f25"),
    }

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.services = app.services
        self._busy = False
        self._build()

    def _build(self) -> None:
        head = ttk.Frame(self)
        head.pack(fill="x", padx=22, pady=(16, 6))
        ttk.Label(head, text="Ask the shop", style="H1.TLabel").pack(anchor="w")
        self.scope_label = ttk.Label(head, text="", style="Muted.TLabel",
                                     wraplength=900, justify="left")
        self.scope_label.pack(anchor="w", pady=(4, 0))

        legend = ttk.Frame(head)
        legend.pack(anchor="w", pady=(10, 0))
        for source in (SOURCE_STORE, SOURCE_RAG, SOURCE_LLM, SOURCE_DENIED):
            label, colour = self.SOURCE_STYLE[source]
            Badge(legend, label.upper(), colour,
                  font_family=self.app.fonts["sans"]).pack(side="left", padx=(0, 6))
        ttk.Label(legend,
                  text="  every answer says where it came from",
                  style="Muted.TLabel").pack(side="left")

        # Transcript.
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=22, pady=(12, 6))

        self.transcript = tk.Text(
            body, wrap="word", bd=0, highlightthickness=1,
            highlightbackground=Theme.BORDER, bg=Theme.SURFACE, fg=Theme.INK,
            padx=16, pady=14, font=(self.app.fonts["sans"], 10),
            state="disabled", insertwidth=0,
        )
        scrollbar = ttk.Scrollbar(body, orient="vertical",
                                  command=self.transcript.yview)
        self.transcript.configure(yscrollcommand=scrollbar.set)
        self.transcript.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._configure_tags()

        # Input.
        footer = ttk.Frame(self)
        footer.pack(fill="x", padx=22, pady=(0, 14))

        self.entry = ttk.Entry(footer, font=(self.app.fonts["sans"], 12))
        self.entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.entry.bind("<Return>", lambda _e: self._send())
        self.send_button = ttk.Button(footer, text="Send", style="Accent.TButton",
                                      command=self._send)
        self.send_button.pack(side="left", padx=(8, 0))
        ttk.Button(footer, text="Clear", command=self._clear).pack(side="left",
                                                                   padx=(6, 0))

        self.suggestion_bar = ttk.Frame(self)
        self.suggestion_bar.pack(fill="x", padx=22, pady=(0, 14))

        self._greet()

    def _configure_tags(self) -> None:
        serif, sans, mono = (self.app.fonts["serif"], self.app.fonts["sans"],
                             self.app.fonts["mono"])
        self.transcript.tag_configure("question", font=(sans, 10, "bold"),
                                      foreground=Theme.INK, spacing1=12,
                                      spacing3=4)
        self.transcript.tag_configure("answer", font=(sans, 10),
                                      foreground=Theme.INK, spacing3=4,
                                      lmargin1=14, lmargin2=14)
        self.transcript.tag_configure("mono", font=(mono, 9),
                                      foreground=Theme.INK, lmargin1=14,
                                      lmargin2=14)
        self.transcript.tag_configure("cite", font=(sans, 8, "italic"),
                                      foreground=Theme.INK_FAINT, lmargin1=14,
                                      spacing3=10)
        for source, (_label, colour) in self.SOURCE_STYLE.items():
            self.transcript.tag_configure("src_%s" % source.replace(" ", "_"),
                                          font=(sans, 8, "bold"),
                                          foreground=colour, spacing1=2)

    # -- transcript helpers --------------------------------------------

    def _write(self, text: str, tag: str) -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert("end", text, tag)
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _clear(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")
        self._greet()

    def _greet(self) -> None:
        role = self.app.role
        self._write("Inkwell & Co. assistant\n", "question")
        self._write(
            "Questions about the shop are answered from our own files and "
            "labelled [via RAG]. Anything else goes to the local language "
            "model and is labelled [via LLM]. Structured answers from the "
            "store's own records are labelled [store data].\n"
            "You are signed in as: %s. Type 'help' to see what that lets you "
            "ask.\n" % models.role_name(role),
            "answer",
        )

    # -- sending -------------------------------------------------------

    def _send(self) -> None:
        if self._busy:
            return
        question = self.entry.get().strip()
        if not question:
            return
        self.entry.delete(0, "end")
        self._write("\nYou: %s\n" % question, "question")

        self._busy = True
        self.send_button.configure(state="disabled", text="...")
        self.app.set_status("Thinking...")

        def worker():
            try:
                answer = self.services.chat.ask(self.app.current_user, question)
            except Exception as exc:  # pragma: no cover - defensive
                answer = None
                error = exc
            else:
                error = None
            self.app.post(lambda: self._receive(answer, error))

        threading.Thread(target=worker, daemon=True).start()

    def _receive(self, answer, error) -> None:
        self._busy = False
        self.send_button.configure(state="normal", text="Send")
        self.app.refresh_status()

        if error is not None or answer is None:
            self._write("[error] Something went wrong: %s\n" % error, "answer")
            return

        label, _colour = self.SOURCE_STYLE.get(
            answer.source, (answer.source, Theme.INK)
        )
        self._write("[%s]  " % label.upper(),
                    "src_%s" % answer.source.replace(" ", "_"))
        if answer.rag_score:
            self._write("local-file match %.2f\n" % answer.rag_score, "cite")
        else:
            self._write("\n", "cite")

        body = answer.text.strip()
        # Tables and reports are aligned with spaces, so render them monospaced.
        tag = "mono" if self._looks_tabular(body) else "answer"
        self._write(body + "\n", tag)

        if answer.citations:
            self._write("Sources: %s\n" % ", ".join(answer.citations), "cite")

    @staticmethod
    def _looks_tabular(text: str) -> bool:
        lines = text.split("\n")
        if len(lines) < 3:
            return False
        aligned = sum(1 for line in lines if "  " in line.strip())
        return aligned >= max(2, len(lines) // 2)

    # -- suggestions ---------------------------------------------------

    def _suggestions(self):
        role = self.app.role
        common = [
            "What are your opening hours?",
            "Do you ship internationally?",
            "Who is on the team?",
            "What categories do you carry?",
        ]
        if role == models.VISITOR:
            return common + ["What is the finance report?"]
        if role == models.REGISTERED:
            return common + ["What is my balance?", "What should I read next?",
                             "How do I become a VIP?"]
        if role == models.VIP:
            return common + ["What is on the to-order list?",
                             "What are my VIP benefits?",
                             "What should I read next?"]
        return ["What is the finance report?", "How many users are there?",
                "Show me the taboo word list", "What is low on stock?",
                "What are your opening hours?"]

    def refresh(self) -> None:
        role = self.app.role
        scopes = {
            models.VISITOR: "As a visitor you can ask about the catalogue, our "
                            "categories, prices, opening hours and policies. "
                            "Account and shop-internal questions need an account.",
            models.REGISTERED: "As a registered customer you can also ask about "
                               "your own balance, purchases, warnings and "
                               "personalised recommendations.",
            models.VIP: "As a VIP customer you can also ask about the to-order "
                        "and no-order lists and your discount.",
            models.SUPER_USER: "As the super-user you can also ask about "
                               "finances, every account, moderation activity "
                               "and stock levels.",
        }
        self.scope_label.configure(text=scopes.get(role, ""))

        for child in self.suggestion_bar.winfo_children():
            child.destroy()
        ttk.Label(self.suggestion_bar, text="Try:", style="Muted.TLabel"
                  ).pack(side="left", padx=(0, 8))
        for text in self._suggestions()[:5]:
            tk.Button(
                self.suggestion_bar, text=text, bd=0, cursor="hand2",
                bg=Theme.SURFACE, fg=Theme.ACCENT,
                activebackground=Theme.SURFACE_ALT,
                font=(self.app.fonts["sans"], 8), padx=9, pady=4,
                command=lambda t=text: self._ask_now(t),
            ).pack(side="left", padx=(0, 5))

    def _ask_now(self, text: str) -> None:
        self.entry.delete(0, "end")
        self.entry.insert(0, text)
        self._send()


# ===========================================================================
# Account
# ===========================================================================


class AccountView(ttk.Frame):
    """Requirement 6: the balance, its fines and its consequences, made visible."""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.services = app.services
        self._scroll = ScrollFrame(self)
        self._scroll.pack(fill="both", expand=True)

    def refresh(self) -> None:
        self._scroll.clear()
        body = self._scroll.body
        user = self.app.current_user
        if user is None:
            ttk.Label(body, text="Sign in to see your account.",
                      style="Muted.TLabel").pack(anchor="w", padx=24, pady=20)
            return

        models.normalise_user(user)
        config = self.services.config
        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True, padx=26, pady=20)

        header = ttk.Frame(wrap)
        header.pack(fill="x")
        ttk.Label(header,
                  text=user.get("display_name") or user.get("username"),
                  style="H1.TLabel").pack(side="left")
        Badge(header, models.role_name(user.get("role", "")).upper(),
              Theme.ROLE_COLOURS.get(user.get("role", ""), Theme.INK_SOFT),
              font_family=self.app.fonts["sans"]).pack(side="left", padx=12,
                                                        pady=(10, 0))

        # Stat tiles.
        tiles = ttk.Frame(wrap)
        tiles.pack(fill="x", pady=(16, 0))
        balance = float(user.get("balance", 0.0))
        stats = [
            ("Balance", "$%.2f" % balance,
             Theme.BAD if balance < 0 else (Theme.WARN if balance < config.fine_amount
                                            else Theme.GOOD)),
            ("Lifetime spend", "$%.2f" % float(user.get("total_spent", 0.0)), Theme.INK),
            ("Purchases", str(int(user.get("purchase_count", 0))), Theme.INK),
            ("Fines paid", "$%.2f" % float(user.get("total_fines", 0.0)),
             Theme.WARN if float(user.get("total_fines", 0.0)) else Theme.INK),
            ("Warnings", "%d of %d" % (int(user.get("warnings", 0)),
                                       config.warning_limit),
             Theme.WARN if int(user.get("warnings", 0)) else Theme.INK),
        ]
        for i, (label, value, colour) in enumerate(stats):
            tiles.columnconfigure(i, weight=1, uniform="stat")
            tile = tk.Frame(tiles, bg=Theme.SURFACE, highlightthickness=1,
                            highlightbackground=Theme.BORDER)
            tile.grid(row=0, column=i, sticky="nsew", padx=4)
            tk.Label(tile, text=label.upper(), bg=Theme.SURFACE,
                     fg=Theme.INK_FAINT,
                     font=(self.app.fonts["sans"], 8, "bold")).pack(anchor="w",
                                                                     padx=14,
                                                                     pady=(12, 0))
            tk.Label(tile, text=value, bg=Theme.SURFACE, fg=colour,
                     font=(self.app.fonts["sans"], 18, "bold")).pack(anchor="w",
                                                                      padx=14,
                                                                      pady=(2, 12))

        # VIP progress.
        if user.get("role") == models.REGISTERED:
            hline(wrap, pady=20)
            self._vip_progress(wrap, user)

        if user.get("role") == models.VIP:
            hline(wrap, pady=20)
            box = tk.Frame(wrap, bg=Theme.SURFACE, highlightthickness=1,
                           highlightbackground=Theme.GOOD)
            box.pack(fill="x")
            tk.Label(box,
                     text="VIP customer -- %d%% off every purchase. "
                          "More than %d moderation warnings would demote you "
                          "back to registered customer."
                          % (int(config.vip_discount * 100), config.warning_limit),
                     bg=Theme.SURFACE, fg=Theme.GOOD,
                     font=(self.app.fonts["sans"], 10), wraplength=880,
                     justify="left", anchor="w", padx=14, pady=12).pack(fill="x")

        # Statement.
        hline(wrap, pady=20)
        ttk.Label(wrap, text="Statement", style="H2.TLabel").pack(anchor="w")
        ttk.Label(wrap, text="Every purchase, fine and deposit on this account.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 10))

        statement = ReadOnlyText(wrap, height=16,
                                 font=(self.app.fonts["mono"], 9),
                                 highlightthickness=1,
                                 highlightbackground=Theme.BORDER)
        statement.pack(fill="x")
        statement.set_text(self.services.finance.user_statement(user))

        # Purchased books.
        history = user.get("purchase_history") or []
        if history:
            hline(wrap, pady=20)
            ttk.Label(wrap, text="Your library", style="H2.TLabel").pack(anchor="w")
            ttk.Label(wrap,
                      text="Reviews you leave on these count double in the "
                           "displayed average.",
                      style="Muted.TLabel").pack(anchor="w", pady=(2, 10))
            from .shop import grid_books
            books = []
            seen = set()
            for entry in reversed(history):
                book = self.services.storage.get_book(entry.get("book_id", ""))
                if book and book["book_id"] not in seen:
                    seen.add(book["book_id"])
                    books.append(book)
            grid = ttk.Frame(wrap)
            grid.pack(fill="x")
            grid_books(grid, self.app, books[:8], columns=4)

    def _vip_progress(self, parent, user: dict) -> None:
        config = self.services.config
        purchases = int(user.get("purchase_count", 0))
        spent = float(user.get("total_spent", 0.0))

        ttk.Label(parent, text="Progress to VIP", style="H2.TLabel").pack(anchor="w")
        ttk.Label(parent,
                  text="More than %d purchases or more than $%.2f spent, "
                       "whichever comes first."
                       % (config.vip_purchase_threshold, config.vip_spend_threshold),
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 10))

        for label, value, target, fmt in (
            ("Purchases", purchases, config.vip_purchase_threshold + 1, "%d"),
            ("Spent", spent, config.vip_spend_threshold + 0.01, "$%.2f"),
        ):
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text="%-10s" % label, style="TLabel",
                      font=(self.app.fonts["sans"], 9)).pack(side="left")
            bar = ttk.Progressbar(row, length=360, maximum=float(target),
                                  value=min(float(value), float(target)))
            bar.pack(side="left", padx=(8, 10))
            ttk.Label(row, text=(fmt + " of " + fmt) % (value, target),
                      style="Muted.TLabel").pack(side="left")

        if user.get("vip_ineligible"):
            tk.Label(parent,
                     text="VIP promotion is on hold for this account after a "
                          "moderation demotion. Ask the super-user to lift it.",
                     bg=Theme.BG, fg=Theme.WARN,
                     font=(self.app.fonts["sans"], 9), wraplength=860,
                     justify="left").pack(anchor="w", pady=(8, 0))


# ===========================================================================
# Shelf Scan
# ===========================================================================


class ShelfScanView(ttk.Frame):
    """Requirement 14: the team's creative feature."""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.services = app.services
        self._busy = False
        self._annotated: Optional[str] = None
        self._build()

    def _build(self) -> None:
        head = ttk.Frame(self)
        head.pack(fill="x", padx=22, pady=(16, 6))
        row = ttk.Frame(head)
        row.pack(fill="x")
        ttk.Label(row, text="Shelf Scan", style="H1.TLabel").pack(side="left")
        Badge(row, "OUR CREATIVE FEATURE", Theme.ACCENT,
              font_family=self.app.fonts["sans"]).pack(side="left", padx=12,
                                                        pady=(10, 0))

        ttk.Label(
            head,
            text="Photograph a bookshelf -- yours or ours -- and upload it. "
                 "The scanner segments the photo into individual books, matches "
                 "each one against our cover index, reads your taste from what "
                 "it finds, and recommends three more. It is how a brand-new "
                 "customer gets real recommendations before browsing anything: "
                 "the books you already own say far more than a few clicks.",
            style="Muted.TLabel", wraplength=900, justify="left",
        ).pack(anchor="w", pady=(6, 12))

        buttons = ttk.Frame(head)
        buttons.pack(anchor="w")
        self.scan_button = ttk.Button(buttons, text="Choose a shelf photo...",
                                      style="Accent.TButton",
                                      command=self._choose)
        self.scan_button.pack(side="left")
        self.demo_button = ttk.Button(buttons, text="Build a demo shelf",
                                      command=self._demo)
        self.demo_button.pack(side="left", padx=(8, 0))
        self.progress = ttk.Progressbar(buttons, mode="indeterminate", length=180)

        self.note = ttk.Label(head, text="", style="Muted.TLabel",
                              wraplength=900, justify="left")
        self.note.pack(anchor="w", pady=(10, 0))

        self._scroll = ScrollFrame(self)
        self._scroll.pack(fill="both", expand=True)

    # -- scanning ------------------------------------------------------

    def _choose(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a photo of a bookshelf",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp"),
                       ("All files", "*.*")],
        )
        if path:
            self._scan(path)

    def _demo(self) -> None:
        """Compose a shelf photo out of our own covers, for demos and testing."""
        try:
            from PIL import Image
        except ImportError:
            toast(self.app, "Pillow is needed to build a demo shelf.", kind="bad")
            return

        books = [b for b in self.services.catalog.all_books()
                 if b.get("cover_path")]
        if len(books) < 3:
            toast(self.app, "Need at least three books with covers.", kind="warn")
            return

        import random
        picks = random.sample(books, min(5, len(books)))
        root = self.services.project_root
        images = []
        height = 320
        for book in picks:
            path = book["cover_path"]
            if not os.path.isabs(path):
                path = os.path.join(root, path)
            if not os.path.exists(path):
                continue
            try:
                cover = Image.open(path).convert("RGB")
            except Exception:
                continue
            width = int(cover.width * height / cover.height)
            images.append(cover.resize((width, height), Image.LANCZOS))

        if not images:
            toast(self.app, "Could not read any cover images.", kind="bad")
            return

        gap = 14
        total = sum(i.width for i in images) + gap * (len(images) + 1)
        shelf = Image.new("RGB", (total, height + 36), (62, 46, 36))
        x = gap
        for image in images:
            shelf.paste(image, (x, 12))
            x += image.width + gap

        out = os.path.join(self.services.storage.data_dir, "demo_shelf.png")
        shelf.save(out)
        self.note.configure(
            text="Built a demo shelf from %d of our own covers: %s"
                 % (len(images), ", ".join(b["title"] for b in picks[:len(images)]))
        )
        self._scan(out)

    def _scan(self, path: str) -> None:
        if self._busy:
            return
        self._busy = True
        self.scan_button.configure(state="disabled")
        self.demo_button.configure(state="disabled")
        self.progress.pack(side="left", padx=(10, 0))
        self.progress.start(12)
        self.app.set_status("Scanning the shelf...")

        def worker():
            try:
                result = self.services.shelfscan.scan(path, self.app.current_user)
                annotated = os.path.join(
                    self.services.storage.data_dir, "shelf_annotated.png"
                )
                if self.services.shelfscan.annotate(path, result, annotated):
                    self._annotated = annotated
                else:
                    self._annotated = None
            except Exception as exc:  # pragma: no cover - defensive
                result = exc
            self.app.post(lambda: self._done(result, path))

        threading.Thread(target=worker, daemon=True).start()

    def _done(self, result, source_path: str) -> None:
        self._busy = False
        self.scan_button.configure(state="normal")
        self.demo_button.configure(state="normal")
        self.progress.stop()
        self.progress.pack_forget()
        self.app.refresh_status()

        self._scroll.clear()
        self._scroll.scroll_to_top()
        body = self._scroll.body
        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True, padx=22, pady=12)

        if isinstance(result, Exception):
            ttk.Label(wrap, text="The scan failed: %s" % result,
                      style="Bad.TLabel", wraplength=880,
                      justify="left").pack(anchor="w")
            return

        if not result.ok:
            ttk.Label(wrap, text=result.message, style="Warn.TLabel",
                      wraplength=880, justify="left").pack(anchor="w")
            return

        # Annotated preview.
        preview_path = self._annotated or source_path
        photo = CoverImage.load(preview_path, size=(880, 360), title="shelf")
        if photo is not None:
            ttk.Label(wrap, text="What the scanner saw", style="H2.TLabel"
                      ).pack(anchor="w")
            ttk.Label(wrap,
                      text="Green boxes matched a book in our catalogue; red "
                           "boxes are regions it could not place.",
                      style="Muted.TLabel").pack(anchor="w", pady=(2, 8))
            label = tk.Label(wrap, image=photo, bg=Theme.BG)
            label.image = photo
            label.pack(anchor="w", pady=(0, 14))

        # Report.
        report = ReadOnlyText(wrap, height=min(26, 8 + result.regions_found * 2),
                              font=(self.app.fonts["mono"], 9),
                              highlightthickness=1,
                              highlightbackground=Theme.BORDER)
        report.pack(fill="x")
        report.set_text(result.summary())

        # Recommendation cards.
        if result.recommendations:
            hline(wrap, pady=18)
            ttk.Label(wrap, text="Recommended from that shelf", style="H2.TLabel"
                      ).pack(anchor="w")
            ttk.Label(wrap,
                      text="Scored against the categories and authors the scan "
                           "found, excluding anything on the no-order list.",
                      style="Muted.TLabel").pack(anchor="w", pady=(2, 10))
            from .shop import grid_books
            grid = ttk.Frame(wrap)
            grid.pack(fill="x")
            grid_books(grid, self.app,
                       [r.book for r in result.recommendations], columns=3,
                       reasons=[r.reason for r in result.recommendations])

    def refresh(self) -> None:
        if not self.services.shelfscan.available():
            self.scan_button.configure(state="disabled")
            self.demo_button.configure(state="disabled")
            self.note.configure(
                text=self.services.shelfscan.dependency_message()
                or "Shelf Scan needs numpy and Pillow."
            )
            return
        backend = ("YOLO weights found -- using YOLO for segmentation."
                   if self.services.shelfscan._load_yolo() is not None
                   else "No YOLO weights installed, so segmentation uses the "
                        "built-in two-stage edge algorithm (background "
                        "separation, then aspect-ratio splitting). Drop "
                        "yolov8n.pt into data/models/ to use YOLO instead.")
        self.note.configure(
            text="%d cover and figure images indexed. %s"
                 % (self.services.images.size, backend)
        )
