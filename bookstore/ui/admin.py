"""The super-user's store management panel.

Covers spec requirement 5 (a GUI for the SU to input new books), 6 (reopening
suspended accounts with a deposit), 8 (acting on the order lists), 9 (keeping
the taboo word list) and 4 (enrolling and training the biometric models).
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import List, Optional

from .. import models
from ..moderation import preview as moderation_preview
from ..rules import RuleError
from .widgets import (Badge, ReadOnlyText, ScrollFrame, Theme, hline, toast)


class AdminView(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.services = app.services

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=12)

        self.pages = {
            "books": AddBookPage(self.notebook, app),
            "catalogue": CataloguePage(self.notebook, app),
            "users": UsersPage(self.notebook, app),
            "moderation": ModerationPage(self.notebook, app),
            "orders": OrderListsPage(self.notebook, app),
            "finance": FinancePage(self.notebook, app),
            "biometrics": BiometricsPage(self.notebook, app),
            "system": SystemPage(self.notebook, app),
        }
        for key, label in (("books", "Add a book"), ("catalogue", "Catalogue"),
                           ("users", "Accounts"), ("moderation", "Moderation"),
                           ("orders", "Order lists"), ("finance", "Finance"),
                           ("biometrics", "Biometrics"), ("system", "System")):
            self.notebook.add(self.pages[key], text=label)

        self.notebook.bind("<<NotebookTabChanged>>", self._page_changed)

    def _page_changed(self, _event=None) -> None:
        try:
            current = self.notebook.select()
        except tk.TclError:
            return
        for page in self.pages.values():
            if str(page) == str(current) and hasattr(page, "refresh"):
                page.refresh()
                break

    def refresh(self) -> None:
        for page in self.pages.values():
            if hasattr(page, "refresh"):
                try:
                    page.refresh()
                except Exception as exc:  # pragma: no cover
                    print("Admin page refresh failed: %s" % exc)


# ===========================================================================
# Add a book (requirement 5)
# ===========================================================================


class AddBookPage(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.services = app.services
        self._cover: str = ""
        self._figures: List[str] = []
        self._text: str = ""
        self._build()

    def _build(self) -> None:
        scroll = ScrollFrame(self)
        scroll.pack(fill="both", expand=True)
        wrap = ttk.Frame(scroll.body)
        wrap.pack(fill="both", expand=True, padx=22, pady=18)

        ttk.Label(wrap, text="Add a book to the catalogue", style="H1.TLabel"
                  ).pack(anchor="w")
        ttk.Label(wrap,
                  text="A cover image makes the title findable by picture "
                       "search. Internal figures are indexed too.",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 16))

        form = tk.Frame(wrap, bg=Theme.SURFACE, highlightthickness=1,
                        highlightbackground=Theme.BORDER)
        form.pack(fill="x")
        inner = tk.Frame(form, bg=Theme.SURFACE)
        inner.pack(fill="x", padx=20, pady=18)

        grid = tk.Frame(inner, bg=Theme.SURFACE)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        self.title_entry = self._row(grid, "Title", 0, 0, span=3)
        self.author_entry = self._row(grid, "Author", 1, 0, span=3)

        self.category_box = ttk.Combobox(grid, values=models.CATEGORIES,
                                         state="readonly", width=18)
        self.category_box.set(models.CATEGORIES[0])
        self._label(grid, "Category", 2, 0)
        self.category_box.grid(row=2, column=1, sticky="w", pady=4)

        self.year_entry = self._row(grid, "Year published", 2, 2, width=12)
        self.price_entry = self._row(grid, "Price ($)", 3, 0, width=12)
        self.stock_entry = self._row(grid, "Stock", 3, 2, width=12)
        self.stock_entry.insert(0, "10")

        self._label(grid, "Description", 4, 0)
        self.description = tk.Text(grid, height=4, wrap="word", bd=0,
                                   highlightthickness=1,
                                   highlightbackground=Theme.BORDER,
                                   bg="#ffffff", fg=Theme.INK, padx=8, pady=6,
                                   font=(self.app.fonts["sans"], 10))
        self.description.grid(row=4, column=1, columnspan=3, sticky="ew", pady=4)

        files = tk.Frame(inner, bg=Theme.SURFACE)
        files.pack(fill="x", pady=(14, 0))

        self.cover_label = self._file_row(
            files, "Cover image", "Choose cover...", self._pick_cover
        )
        self.figures_label = self._file_row(
            files, "Internal figures", "Choose figures...", self._pick_figures
        )
        self.text_label = self._file_row(
            files, "Full text (.txt)", "Choose text file...", self._pick_text
        )

        actions = tk.Frame(inner, bg=Theme.SURFACE)
        actions.pack(fill="x", pady=(16, 0))
        ttk.Button(actions, text="Add book", style="Accent.TButton",
                   command=self._add).pack(side="left")
        ttk.Button(actions, text="Clear form", command=self._clear
                   ).pack(side="left", padx=8)

        self.message = ttk.Label(inner, text="", style="SurfaceMuted.TLabel",
                                 wraplength=820, justify="left")
        self.message.pack(anchor="w", pady=(12, 0))

    def _label(self, parent, text, row, column):
        tk.Label(parent, text=text, bg=Theme.SURFACE, fg=Theme.INK_SOFT,
                 font=(self.app.fonts["sans"], 9)).grid(
            row=row, column=column, sticky="w", padx=(0, 10), pady=4)

    def _row(self, parent, label, row, column, width=None, span=1):
        self._label(parent, label, row, column)
        entry = ttk.Entry(parent, width=width or 30)
        entry.grid(row=row, column=column + 1, columnspan=span, sticky="ew" if not width else "w",
                   pady=4)
        return entry

    def _file_row(self, parent, label, button_text, command):
        row = tk.Frame(parent, bg=Theme.SURFACE)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, bg=Theme.SURFACE, fg=Theme.INK_SOFT,
                 font=(self.app.fonts["sans"], 9), width=18, anchor="w"
                 ).pack(side="left")
        ttk.Button(row, text=button_text, command=command).pack(side="left")
        value = tk.Label(row, text="none selected", bg=Theme.SURFACE,
                         fg=Theme.INK_FAINT,
                         font=(self.app.fonts["sans"], 9), anchor="w")
        value.pack(side="left", padx=(10, 0))
        return value

    # -- file pickers --------------------------------------------------

    def _pick_cover(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a cover image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp")],
        )
        if path:
            self._cover = path
            self.cover_label.configure(text=os.path.basename(path),
                                       fg=Theme.INK)

    def _pick_figures(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Choose internal figures",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp")],
        )
        if paths:
            self._figures = list(paths)
            self.figures_label.configure(
                text="%d file(s) selected" % len(self._figures), fg=Theme.INK
            )

    def _pick_text(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose the book's text file",
            filetypes=[("Text", "*.txt *.md"), ("All files", "*.*")],
        )
        if path:
            self._text = path
            self.text_label.configure(text=os.path.basename(path), fg=Theme.INK)

    # -- adding --------------------------------------------------------

    def _copy_into(self, source: str, folder: str, name: str) -> str:
        """Copy an attachment into data/ and return a project-relative path."""
        import shutil
        os.makedirs(folder, exist_ok=True)
        extension = os.path.splitext(source)[1] or ".png"
        target = os.path.join(folder, name + extension)
        shutil.copyfile(source, target)
        return os.path.relpath(target, self.services.project_root)

    def _add(self) -> None:
        storage = self.services.storage
        book_id = storage.next_book_id()

        cover_rel = ""
        figure_rels: List[str] = []
        text_rel = ""
        try:
            if self._cover:
                cover_rel = self._copy_into(self._cover, storage.covers_dir, book_id)
            for i, path in enumerate(self._figures, start=1):
                figure_rels.append(
                    self._copy_into(path, storage.figures_dir,
                                    "%s_fig%d" % (book_id, i))
                )
            if self._text:
                text_rel = self._copy_into(self._text, storage.book_text_dir, book_id)
        except OSError as exc:
            self.message.configure(text="Could not copy a file: %s" % exc,
                                   foreground=Theme.BAD)
            return

        try:
            book = self.services.catalog.add_book(
                self.title_entry.get(),
                self.author_entry.get(),
                self.category_box.get(),
                self.year_entry.get() or 0,
                self.price_entry.get(),
                stock=self.stock_entry.get() or 0,
                description=self.description.get("1.0", "end").strip(),
                cover_path=cover_rel,
                figure_paths=figure_rels,
                text_path=text_rel,
            )
        except ValueError as exc:
            self.message.configure(text=str(exc), foreground=Theme.BAD)
            return

        # Index the new cover straight away so picture search finds it.
        if cover_rel or figure_rels:
            self.services.images.build()

        self.message.configure(
            text="Added '%s' as %s.%s" % (
                book["title"], book["book_id"],
                "  Cover and figures indexed for picture search."
                if (cover_rel or figure_rels) else ""),
            foreground=Theme.GOOD,
        )
        toast(self.app, "Added '%s'." % book["title"], kind="good")
        self._clear()
        self.app.refresh_all()

    def _clear(self) -> None:
        for entry in (self.title_entry, self.author_entry, self.year_entry,
                      self.price_entry):
            entry.delete(0, "end")
        self.stock_entry.delete(0, "end")
        self.stock_entry.insert(0, "10")
        self.description.delete("1.0", "end")
        self._cover = ""
        self._figures = []
        self._text = ""
        for label in (self.cover_label, self.figures_label, self.text_label):
            label.configure(text="none selected", fg=Theme.INK_FAINT)

    def refresh(self) -> None:
        pass


# ===========================================================================
# Catalogue management
# ===========================================================================


class CataloguePage(ttk.Frame):
    COLUMNS = [("book_id", "ID", 60), ("title", "Title", 290),
               ("author", "Author", 180), ("category", "Category", 110),
               ("year", "Year", 60), ("price", "Price", 70),
               ("stock", "Stock", 60), ("rating", "Rating", 90),
               ("status", "List", 90), ("sold", "Sold", 60)]

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.services = app.services
        self._build()

    def _build(self) -> None:
        head = ttk.Frame(self)
        head.pack(fill="x", padx=18, pady=(14, 8))
        ttk.Label(head, text="Catalogue", style="H1.TLabel").pack(anchor="w")
        self.count_label = ttk.Label(head, text="", style="Muted.TLabel")
        self.count_label.pack(anchor="w", pady=(2, 10))

        actions = ttk.Frame(head)
        actions.pack(anchor="w")
        ttk.Button(actions, text="Restock selected (+10)",
                   command=self._restock).pack(side="left")
        ttk.Button(actions, text="Change price...",
                   command=self._change_price).pack(side="left", padx=6)
        ttk.Button(actions, text="Rebuild picture index",
                   command=self._rebuild_index).pack(side="left", padx=6)
        ttk.Button(actions, text="Delete selected",
                   command=self._delete).pack(side="left", padx=6)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 14))

        self.tree = ttk.Treeview(body, columns=[c[0] for c in self.COLUMNS],
                                 show="headings", selectmode="browse")
        for key, label, width in self.COLUMNS:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width,
                             anchor="e" if key in ("price", "stock", "sold", "year")
                             else "w")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.tag_configure("no_order", foreground=Theme.BAD)
        self.tree.tag_configure("to_order", foreground=Theme.GOOD)
        self.tree.tag_configure("oos", background="#f7eeec")

    def _selected_book(self) -> Optional[dict]:
        selection = self.tree.selection()
        if not selection:
            toast(self.app, "Select a book first.", kind="warn")
            return None
        return self.services.storage.get_book(selection[0])

    def _restock(self) -> None:
        book = self._selected_book()
        if not book:
            return
        self.services.catalog.update_book(
            book["book_id"], stock=int(book.get("stock", 0)) + 10
        )
        toast(self.app, "Restocked '%s'." % book["title"], kind="good")
        self.refresh()

    def _change_price(self) -> None:
        book = self._selected_book()
        if not book:
            return
        from tkinter import simpledialog
        value = simpledialog.askfloat(
            "Change price", "New price for '%s':" % book["title"],
            initialvalue=float(book.get("price", 0.0)), minvalue=0.0,
        )
        if value is None:
            return
        self.services.catalog.update_book(book["book_id"], price=round(value, 2))
        toast(self.app, "Price updated.", kind="good")
        self.refresh()
        self.app.refresh_all()

    def _delete(self) -> None:
        book = self._selected_book()
        if not book:
            return
        if not messagebox.askyesno(
            "Delete book",
            "Delete '%s' from the catalogue?\n\nReviews and past purchases of it "
            "stay in the records." % book["title"],
        ):
            return
        self.services.catalog.delete_book(book["book_id"])
        toast(self.app, "Deleted '%s'." % book["title"], kind="info")
        self.refresh()
        self.app.refresh_all()

    def _rebuild_index(self) -> None:
        self.app.set_status("Rebuilding the picture index...")
        count = self.services.images.build(force=True)
        toast(self.app, "Re-described %d image(s); %d in the index."
              % (count, self.services.images.size), kind="good")
        self.app.refresh_status()

    def refresh(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

        books = self.services.catalog.all_books()
        for book in books:
            summary = self.services.rules.rating_summary(book["book_id"])
            status = self.services.rules.list_status(book["book_id"])
            tags = []
            if status == "no-order":
                tags.append("no_order")
            elif status == "to-order":
                tags.append("to_order")
            if int(book.get("stock", 0)) == 0:
                tags.append("oos")

            self.tree.insert(
                "", "end", iid=book["book_id"],
                values=(
                    book["book_id"], book.get("title", ""), book.get("author", ""),
                    book.get("category", ""), book.get("year") or "",
                    "%.2f" % float(book.get("price", 0.0)),
                    int(book.get("stock", 0)),
                    ("%.2f (%d)" % (summary.weighted_average, summary.count))
                    if summary.count else "-",
                    status if status != "normal" else "",
                    int(book.get("purchase_count", 0)),
                ),
                tags=tags,
            )

        self.count_label.configure(
            text="%d titles.  %d with a cover indexed for picture search."
                 % (len(books), self.services.images.size)
        )


# ===========================================================================
# Accounts (requirement 6 and 10)
# ===========================================================================


class UsersPage(ttk.Frame):
    COLUMNS = [("user_id", "ID", 60), ("username", "Username", 120),
               ("display_name", "Name", 170), ("role", "Role", 150),
               ("balance", "Balance", 90), ("spent", "Spent", 90),
               ("purchases", "Buys", 55), ("fines", "Fines", 70),
               ("warnings", "Warn", 55), ("state", "State", 150)]

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.services = app.services
        self._build()

    def _build(self) -> None:
        head = ttk.Frame(self)
        head.pack(fill="x", padx=18, pady=(14, 8))
        ttk.Label(head, text="Accounts", style="H1.TLabel").pack(anchor="w")
        ttk.Label(head,
                  text="A negative balance suspends an account. Reopening it "
                       "requires a deposit that brings the balance back above "
                       "zero, and clears the warning count.",
                  style="Muted.TLabel", wraplength=880, justify="left"
                  ).pack(anchor="w", pady=(2, 10))

        actions = ttk.Frame(head)
        actions.pack(anchor="w")
        ttk.Button(actions, text="Deposit...", style="Accent.TButton",
                   command=self._deposit).pack(side="left")
        ttk.Button(actions, text="Reopen account...",
                   command=self._reopen).pack(side="left", padx=6)
        ttk.Button(actions, text="Suspend",
                   command=self._suspend).pack(side="left", padx=6)
        ttk.Button(actions, text="Clear warnings",
                   command=self._clear_warnings).pack(side="left", padx=6)
        ttk.Button(actions, text="New account...",
                   command=self._new_account).pack(side="left", padx=6)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 14))

        self.tree = ttk.Treeview(body, columns=[c[0] for c in self.COLUMNS],
                                 show="headings", selectmode="browse")
        for key, label, width in self.COLUMNS:
            self.tree.heading(key, text=label)
            self.tree.column(
                key, width=width,
                anchor="e" if key in ("balance", "spent", "purchases", "fines",
                                      "warnings") else "w",
            )
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.tag_configure("suspended", foreground=Theme.BAD,
                                background="#f7eeec")
        self.tree.tag_configure("warned", foreground=Theme.WARN)
        self.tree.tag_configure("vip", foreground="#7a5c0f")

    def _selected_user(self) -> Optional[dict]:
        selection = self.tree.selection()
        if not selection:
            toast(self.app, "Select an account first.", kind="warn")
            return None
        user = self.services.storage.get_user(selection[0])
        return models.normalise_user(user) if user else None

    def _deposit(self) -> None:
        user = self._selected_user()
        if not user:
            return
        from tkinter import simpledialog
        amount = simpledialog.askfloat(
            "Deposit", "Amount to credit to %s:" % user["username"],
            initialvalue=20.0, minvalue=0.01,
        )
        if amount is None:
            return
        try:
            balance = self.services.rules.deposit(user, amount, by_super_user=True)
        except RuleError as exc:
            toast(self.app, str(exc), kind="bad")
            return
        toast(self.app, "Credited $%.2f. Balance is now $%.2f."
              % (amount, balance), kind="good")
        self.refresh()
        self.app.refresh_all()

    def _reopen(self) -> None:
        user = self._selected_user()
        if not user:
            return
        if not user.get("suspended"):
            toast(self.app, "That account is not suspended.", kind="warn")
            return
        from tkinter import simpledialog
        needed = max(0.0, -float(user.get("balance", 0.0)))
        amount = simpledialog.askfloat(
            "Reopen account",
            "%s is suspended (%s).\nBalance is $%.2f.\n\nDeposit at least $%.2f "
            "to bring it positive:"
            % (user["username"], user.get("suspend_reason") or "no reason",
               float(user.get("balance", 0.0)), needed + 0.01),
            initialvalue=round(needed + 10.0, 2), minvalue=0.0,
        )
        if amount is None:
            return
        try:
            self.services.rules.reopen_account(user, amount)
        except RuleError as exc:
            toast(self.app, str(exc), kind="bad", ms=6000)
            self.refresh()
            return
        toast(self.app, "Reopened %s. Warnings cleared." % user["username"],
              kind="good")
        self.refresh()
        self.app.refresh_all()

    def _suspend(self) -> None:
        user = self._selected_user()
        if not user:
            return
        if user.get("role") == models.SUPER_USER:
            toast(self.app, "You cannot suspend a super-user account.", kind="warn")
            return
        if not messagebox.askyesno("Suspend account",
                                   "Suspend %s? They will not be able to log in."
                                   % user["username"]):
            return
        self.services.rules.suspend(user, "suspended by super-user")
        toast(self.app, "Suspended %s." % user["username"], kind="info")
        self.refresh()

    def _clear_warnings(self) -> None:
        user = self._selected_user()
        if not user:
            return
        user["warnings"] = 0
        user["vip_ineligible"] = False
        self.services.storage.put_user(user)
        self.services.storage.add_event(
            "warnings_cleared", user["user_id"], "cleared by super-user"
        )
        toast(self.app, "Cleared warnings for %s." % user["username"], kind="good")
        self.refresh()

    def _new_account(self) -> None:
        dialog = tk.Toplevel(self.app)
        dialog.title("New account")
        dialog.configure(bg=Theme.BG)
        dialog.transient(self.app)
        dialog.grab_set()

        frame = ttk.Frame(dialog)
        frame.pack(fill="both", expand=True, padx=18, pady=16)

        entries = {}
        for i, (key, label, secret) in enumerate((
            ("display_name", "Display name", False),
            ("username", "Username", False),
            ("password", "Password", True),
            ("balance", "Opening balance ($)", False),
        )):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky="w", pady=4)
            entry = ttk.Entry(frame, width=26, show="•" if secret else "")
            entry.grid(row=i, column=1, sticky="ew", pady=4, padx=(10, 0))
            entries[key] = entry
        entries["balance"].insert(0, "25.00")

        ttk.Label(frame, text="Role").grid(row=4, column=0, sticky="w", pady=4)
        role_box = ttk.Combobox(
            frame, state="readonly", width=24,
            values=["%s (%s)" % (models.role_name(r), r)
                    for r in (models.REGISTERED, models.VIP, models.SUPER_USER)],
        )
        role_box.current(0)
        role_box.grid(row=4, column=1, sticky="ew", pady=4, padx=(10, 0))

        message = ttk.Label(frame, text="", style="Muted.TLabel", wraplength=300,
                            justify="left")
        message.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        def create():
            try:
                balance = float(entries["balance"].get() or 0)
            except ValueError:
                message.configure(text="Balance must be a number.",
                                  foreground=Theme.BAD)
                return
            role = role_box.get().split("(")[-1].rstrip(")")
            try:
                self.services.rules.create_account(
                    entries["username"].get(), entries["password"].get(),
                    role=role, balance=balance,
                    display_name=entries["display_name"].get(),
                )
            except RuleError as exc:
                message.configure(text=str(exc), foreground=Theme.BAD)
                return
            dialog.destroy()
            toast(self.app, "Account created.", kind="good")
            self.refresh()
            self.app.refresh_all()

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, sticky="w", pady=(14, 0))
        ttk.Button(buttons, text="Create", style="Accent.TButton",
                   command=create).pack(side="left")
        ttk.Button(buttons, text="Cancel",
                   command=dialog.destroy).pack(side="left", padx=8)

    def refresh(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for user in self.services.storage.list_users():
            models.normalise_user(user)
            tags = []
            if user.get("suspended"):
                tags.append("suspended")
            elif int(user.get("warnings", 0)) > 0:
                tags.append("warned")
            elif user.get("role") == models.VIP:
                tags.append("vip")

            state = "suspended: %s" % (user.get("suspend_reason") or "?") \
                if user.get("suspended") else "active"
            if user.get("vip_ineligible") and not user.get("suspended"):
                state += " (VIP on hold)"

            self.tree.insert(
                "", "end", iid=user["user_id"],
                values=(
                    user["user_id"], user.get("username", ""),
                    user.get("display_name", ""),
                    models.role_name(user.get("role", "")),
                    "%.2f" % float(user.get("balance", 0.0)),
                    "%.2f" % float(user.get("total_spent", 0.0)),
                    int(user.get("purchase_count", 0)),
                    "%.2f" % float(user.get("total_fines", 0.0)),
                    int(user.get("warnings", 0)),
                    state,
                ),
                tags=tags,
            )


# ===========================================================================
# Moderation (requirement 9)
# ===========================================================================


class ModerationPage(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.services = app.services
        self._build()

    def _build(self) -> None:
        scroll = ScrollFrame(self)
        scroll.pack(fill="both", expand=True)
        wrap = ttk.Frame(scroll.body)
        wrap.pack(fill="both", expand=True, padx=20, pady=16)

        ttk.Label(wrap, text="Moderation", style="H1.TLabel").pack(anchor="w")
        ttk.Label(wrap,
                  text="Taboo words are masked wherever they appear. A comment "
                       "with more than %d of them is blocked outright and its "
                       "author is warned."
                       % self.services.config.max_taboo_before_block,
                  style="Muted.TLabel", wraplength=880, justify="left"
                  ).pack(anchor="w", pady=(4, 16))

        columns = ttk.Frame(wrap)
        columns.pack(fill="both", expand=True)
        columns.columnconfigure(0, weight=1, uniform="mod")
        columns.columnconfigure(1, weight=1, uniform="mod")

        # Taboo list editor.
        left = tk.Frame(columns, bg=Theme.SURFACE, highlightthickness=1,
                        highlightbackground=Theme.BORDER)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 9))
        inner = tk.Frame(left, bg=Theme.SURFACE)
        inner.pack(fill="both", expand=True, padx=16, pady=14)

        ttk.Label(inner, text="Taboo word list", style="H3.TLabel").pack(anchor="w")
        self.taboo_count = ttk.Label(inner, text="", style="SurfaceMuted.TLabel")
        self.taboo_count.pack(anchor="w", pady=(2, 10))

        list_frame = tk.Frame(inner, bg=Theme.SURFACE)
        list_frame.pack(fill="both", expand=True)
        self.taboo_list = tk.Listbox(
            list_frame, height=12, bd=0, highlightthickness=1,
            highlightbackground=Theme.BORDER, bg="#ffffff", fg=Theme.INK,
            font=(self.app.fonts["mono"], 10), selectmode="browse",
            activestyle="none",
        )
        bar = ttk.Scrollbar(list_frame, orient="vertical",
                            command=self.taboo_list.yview)
        self.taboo_list.configure(yscrollcommand=bar.set)
        self.taboo_list.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        add_row = tk.Frame(inner, bg=Theme.SURFACE)
        add_row.pack(fill="x", pady=(10, 0))
        self.taboo_entry = ttk.Entry(add_row)
        self.taboo_entry.pack(side="left", fill="x", expand=True)
        self.taboo_entry.bind("<Return>", lambda _e: self._add_word())
        ttk.Button(add_row, text="Add", command=self._add_word
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(add_row, text="Remove", command=self._remove_word
                   ).pack(side="left", padx=(6, 0))

        # Tester and activity.
        right = tk.Frame(columns, bg=Theme.SURFACE, highlightthickness=1,
                         highlightbackground=Theme.BORDER)
        right.grid(row=0, column=1, sticky="nsew", padx=(9, 0))
        inner = tk.Frame(right, bg=Theme.SURFACE)
        inner.pack(fill="both", expand=True, padx=16, pady=14)

        ttk.Label(inner, text="Test the filter", style="H3.TLabel").pack(anchor="w")
        ttk.Label(inner, text="Type a comment to see exactly what the filter "
                             "would do to it.",
                  style="SurfaceMuted.TLabel", wraplength=380, justify="left"
                  ).pack(anchor="w", pady=(2, 8))

        self.test_entry = tk.Text(inner, height=4, wrap="word", bd=0,
                                  highlightthickness=1,
                                  highlightbackground=Theme.BORDER,
                                  bg="#ffffff", fg=Theme.INK, padx=8, pady=6,
                                  font=(self.app.fonts["sans"], 10))
        self.test_entry.pack(fill="x")
        self.test_entry.bind("<KeyRelease>", lambda _e: self._test())

        self.test_verdict = ttk.Label(inner, text="", style="SurfaceMuted.TLabel",
                                      wraplength=380, justify="left")
        self.test_verdict.pack(anchor="w", pady=(8, 0))
        self.test_output = ttk.Label(inner, text="", style="Surface.TLabel",
                                     wraplength=380, justify="left",
                                     font=(self.app.fonts["mono"], 9))
        self.test_output.pack(anchor="w", pady=(4, 0))

        hline(inner, pady=12)
        ttk.Label(inner, text="Recent moderation activity", style="H3.TLabel"
                  ).pack(anchor="w")
        self.activity = ReadOnlyText(inner, height=12,
                                     font=(self.app.fonts["mono"], 9),
                                     highlightthickness=1,
                                     highlightbackground=Theme.BORDER)
        self.activity.pack(fill="both", expand=True, pady=(8, 0))

    def _add_word(self) -> None:
        word = self.taboo_entry.get().strip()
        if not word:
            return
        if self.services.config.add_taboo_word(word):
            self.taboo_entry.delete(0, "end")
            toast(self.app, "Added '%s' to the taboo list." % word.lower(),
                  kind="good")
            self.refresh()
            self.app.refresh_all()
        else:
            toast(self.app, "'%s' is already on the list." % word.lower(),
                  kind="warn")

    def _remove_word(self) -> None:
        selection = self.taboo_list.curselection()
        if not selection:
            toast(self.app, "Select a word to remove.", kind="warn")
            return
        word = self.taboo_list.get(selection[0])
        if self.services.config.remove_taboo_word(word):
            toast(self.app, "Removed '%s'." % word, kind="info")
            self.refresh()
            self.app.refresh_all()

    def _test(self) -> None:
        text = self.test_entry.get("1.0", "end").strip()
        if not text:
            self.test_verdict.configure(text="")
            self.test_output.configure(text="")
            return
        config = self.services.config
        verdict = moderation_preview(text, config.taboo_words,
                                     config.max_taboo_before_block)
        colour = (Theme.BAD if verdict.startswith("BLOCKED")
                  else Theme.WARN if verdict.startswith("Masked") else Theme.GOOD)
        self.test_verdict.configure(text=verdict, foreground=colour)

        from ..moderation import screen_comment
        result = screen_comment(text, config.taboo_words,
                                config.max_taboo_before_block)
        self.test_output.configure(
            text=("(the comment would not be posted)" if result.blocked
                  else result.masked)
        )

    def refresh(self) -> None:
        words = self.services.config.taboo_words
        self.taboo_list.delete(0, "end")
        for word in words:
            self.taboo_list.insert("end", word)
        self.taboo_count.configure(text="%d word%s on the list."
                                        % (len(words), "" if len(words) == 1 else "s"))

        storage = self.services.storage
        lines = []
        blocked = [r for r in storage.reviews if r.get("blocked")]
        masked = [r for r in storage.reviews
                  if not r.get("blocked") and int(r.get("taboo_hits", 0)) > 0]
        lines.append("%d comment(s) blocked, %d masked." % (len(blocked), len(masked)))
        lines.append("")

        for review in sorted(storage.reviews,
                             key=lambda r: r.get("created_at", ""), reverse=True):
            if not int(review.get("taboo_hits", 0)):
                continue
            author = storage.get_user(review.get("user_id", ""))
            book = storage.get_book(review.get("book_id", ""))
            lines.append(
                "%s  %-10s on %-24s %d hit(s)"
                % ("BLOCKED" if review.get("blocked") else "masked ",
                   (author.get("username") if author else "?")[:10],
                   (book.get("title") if book else "?")[:24],
                   int(review.get("taboo_hits", 0)))
            )
            lines.append("    original: %s" % str(review.get("raw_comment", ""))[:120])
            lines.append("    shown:    %s" % (
                "(withheld)" if review.get("blocked")
                else str(review.get("comment", ""))[:120]))
            lines.append("")

        warnings = [e for e in storage.events if e.get("kind") in
                    ("warning", "demoted", "suspended", "warnings_cleared")]
        if warnings:
            lines.append("Consequences applied:")
            for event in warnings[-12:][::-1]:
                user = storage.get_user(event.get("user_id", ""))
                lines.append("  %-18s %-10s %s"
                             % (event.get("kind"),
                                (user.get("username") if user else "?")[:10],
                                event.get("detail", "")))

        self.activity.set_text("\n".join(lines))
        self._test()


# ===========================================================================
# Order lists (requirement 8)
# ===========================================================================


class OrderListsPage(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.services = app.services
        self._scroll = ScrollFrame(self)
        self._scroll.pack(fill="both", expand=True)

    def refresh(self) -> None:
        self._scroll.clear()
        wrap = ttk.Frame(self._scroll.body)
        wrap.pack(fill="both", expand=True, padx=20, pady=16)
        config = self.services.config

        ttk.Label(wrap, text="Order lists", style="H1.TLabel").pack(anchor="w")
        ttk.Label(
            wrap,
            text="A title with more than %d ratings goes on the no-order list "
                 "below %.1f stars, or the to-order list above %.1f. The count "
                 "is the number of ratings; the average is %s."
                 % (config.min_ratings_for_list, config.no_order_max_avg,
                    config.to_order_min_avg,
                    "weighted, so verified buyers count more"
                    if config.flag("order_lists_use_weighted_average")
                    else "unweighted"),
            style="Muted.TLabel", wraplength=880, justify="left",
        ).pack(anchor="w", pady=(4, 16))

        lists = self.services.rules.order_lists()

        self._section(
            wrap, "To order", lists["to_order"], Theme.GOOD,
            "Keep these in stock permanently -- customers rate them highly.",
            show_restock=True,
        )
        hline(wrap, pady=18)
        self._section(
            wrap, "No order", lists["no_order"], Theme.BAD,
            "Stop reordering these. They are hidden from personalised "
            "recommendations automatically.",
            show_restock=False,
        )

        hline(wrap, pady=18)
        ttk.Label(wrap, text="Everything else", style="H2.TLabel").pack(anchor="w")
        counted = {row["book"]["book_id"] for row in lists["to_order"] + lists["no_order"]}
        others = [b for b in self.services.catalog.all_books()
                  if b["book_id"] not in counted]
        ttk.Label(wrap,
                  text="%d title%s have too few ratings or sit between the "
                       "thresholds." % (len(others), "" if len(others) == 1 else "s"),
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 0))

    def _section(self, parent, title: str, rows, colour: str, blurb: str,
                 show_restock: bool) -> None:
        header = ttk.Frame(parent)
        header.pack(fill="x")
        ttk.Label(header, text=title, style="H2.TLabel").pack(side="left")
        Badge(header, "%d" % len(rows), colour,
              font_family=self.app.fonts["sans"]).pack(side="left", padx=10,
                                                        pady=(8, 0))
        ttk.Label(parent, text=blurb, style="Muted.TLabel", wraplength=880,
                  justify="left").pack(anchor="w", pady=(2, 10))

        if not rows:
            ttk.Label(parent, text="Nothing on this list.", style="Muted.TLabel"
                      ).pack(anchor="w")
            return

        for row in rows:
            book = row["book"]
            box = tk.Frame(parent, bg=Theme.SURFACE, highlightthickness=1,
                           highlightbackground=colour)
            box.pack(fill="x", pady=4)
            inner = tk.Frame(box, bg=Theme.SURFACE)
            inner.pack(fill="x", padx=14, pady=10)

            tk.Label(inner, text=book.get("title", ""), bg=Theme.SURFACE,
                     fg=Theme.INK, font=(self.app.fonts["serif"], 12, "bold")
                     ).pack(side="left")
            tk.Label(inner, text="  by %s" % book.get("author", ""),
                     bg=Theme.SURFACE, fg=Theme.INK_SOFT,
                     font=(self.app.fonts["sans"], 9)).pack(side="left")

            right = tk.Frame(inner, bg=Theme.SURFACE)
            right.pack(side="right")
            tk.Label(right,
                     text="%.2f weighted  /  %.2f plain  from %d ratings  |  "
                          "stock %d"
                          % (row["average"], row["plain_average"], row["count"],
                             int(book.get("stock", 0))),
                     bg=Theme.SURFACE, fg=colour,
                     font=(self.app.fonts["sans"], 9)).pack(side="left", padx=(0, 10))
            if show_restock:
                ttk.Button(right, text="+10 stock",
                           command=lambda b=book: self._restock(b)).pack(side="left")

    def _restock(self, book: dict) -> None:
        self.services.catalog.update_book(
            book["book_id"], stock=int(book.get("stock", 0)) + 10
        )
        toast(self.app, "Restocked '%s'." % book["title"], kind="good")
        self.refresh()


# ===========================================================================
# Finance (requirement 3)
# ===========================================================================


class FinancePage(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.services = app.services
        self._build()

    def _build(self) -> None:
        head = ttk.Frame(self)
        head.pack(fill="x", padx=20, pady=(14, 8))
        ttk.Label(head, text="Finance", style="H1.TLabel").pack(anchor="w")
        ttk.Label(head,
                  text="Derived from the transaction ledger, so it always "
                       "agrees with the accounts. The same report answers a "
                       "super-user's finance question in the assistant.",
                  style="Muted.TLabel", wraplength=880, justify="left"
                  ).pack(anchor="w", pady=(2, 10))

        self.tiles = ttk.Frame(self)
        self.tiles.pack(fill="x", padx=20)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=20, pady=14)
        self.report = ReadOnlyText(body, font=(self.app.fonts["mono"], 10),
                                   highlightthickness=1,
                                   highlightbackground=Theme.BORDER)
        scrollbar = ttk.Scrollbar(body, orient="vertical",
                                  command=self.report.yview)
        self.report.configure(yscrollcommand=scrollbar.set)
        self.report.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def refresh(self) -> None:
        for child in self.tiles.winfo_children():
            child.destroy()

        report = self.services.finance.report()
        stats = [
            ("Total revenue", "$%.2f" % report.total_revenue, Theme.GOOD),
            ("Sales", "$%.2f" % report.revenue_sales, Theme.INK),
            ("Fines", "$%.2f" % report.revenue_fines, Theme.WARN),
            ("Orders", str(report.purchase_count), Theme.INK),
            ("Avg order", "$%.2f" % report.average_order, Theme.INK),
            ("Customer balances", "$%.2f" % report.customer_liability, Theme.ACCENT),
        ]
        for i, (label, value, colour) in enumerate(stats):
            self.tiles.columnconfigure(i, weight=1, uniform="fin")
            tile = tk.Frame(self.tiles, bg=Theme.SURFACE, highlightthickness=1,
                            highlightbackground=Theme.BORDER)
            tile.grid(row=0, column=i, sticky="nsew", padx=3)
            tk.Label(tile, text=label.upper(), bg=Theme.SURFACE,
                     fg=Theme.INK_FAINT,
                     font=(self.app.fonts["sans"], 8, "bold")).pack(anchor="w",
                                                                     padx=12,
                                                                     pady=(10, 0))
            tk.Label(tile, text=value, bg=Theme.SURFACE, fg=colour,
                     font=(self.app.fonts["sans"], 15, "bold")).pack(anchor="w",
                                                                      padx=12,
                                                                      pady=(2, 10))

        self.report.set_text(self.services.finance.report_text())


# ===========================================================================
# Biometrics (requirement 4)
# ===========================================================================


class BiometricsPage(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.services = app.services
        self._busy = False
        self._build()

    def _build(self) -> None:
        scroll = ScrollFrame(self)
        scroll.pack(fill="both", expand=True)
        wrap = ttk.Frame(scroll.body)
        wrap.pack(fill="both", expand=True, padx=20, pady=16)

        ttk.Label(wrap, text="Biometric enrolment", style="H1.TLabel").pack(anchor="w")
        ttk.Label(
            wrap,
            text="Enrol a customer's face or voice, then train the models. "
                 "Face detection uses YOLO when weights are present, YuNet or a "
                 "Haar cascade otherwise. Classification is eigenfaces (PCA) for "
                 "faces and MFCC nearest-centroid for voices -- both trained on "
                 "the samples below.",
            style="Muted.TLabel", wraplength=880, justify="left",
        ).pack(anchor="w", pady=(4, 14))

        self.status = tk.Label(wrap, bg=Theme.SURFACE, fg=Theme.INK,
                               font=(self.app.fonts["mono"], 9), justify="left",
                               anchor="w", padx=14, pady=10,
                               highlightthickness=1,
                               highlightbackground=Theme.BORDER)
        self.status.pack(fill="x", pady=(0, 14))

        picker = ttk.Frame(wrap)
        picker.pack(fill="x")
        ttk.Label(picker, text="Account").pack(side="left")
        self.user_box = ttk.Combobox(picker, state="readonly", width=40)
        self.user_box.pack(side="left", padx=(10, 0))
        self.user_box.bind("<<ComboboxSelected>>", lambda _e: self._update_counts())

        self.counts = ttk.Label(wrap, text="", style="Muted.TLabel")
        self.counts.pack(anchor="w", pady=(8, 14))

        columns = ttk.Frame(wrap)
        columns.pack(fill="x")
        columns.columnconfigure(0, weight=1, uniform="bio")
        columns.columnconfigure(1, weight=1, uniform="bio")

        face = self._card(columns, 0, "Face samples",
                          "Five samples is plenty. Capture from the camera, or "
                          "add saved photographs on a machine without one.")
        ttk.Button(face, text="Capture 5 from camera", style="Accent.TButton",
                   command=self._capture_face).pack(anchor="w", pady=2)
        ttk.Button(face, text="Add photo files...",
                   command=self._face_from_files).pack(anchor="w", pady=2)
        ttk.Button(face, text="Delete this user's face samples",
                   command=self._clear_face).pack(anchor="w", pady=2)

        voice = self._card(columns, 1, "Voice samples",
                           "Three recordings of a few seconds each. 16-bit PCM "
                           "WAV files work without any extra package.")
        ttk.Button(voice, text="Record 3 seconds", style="Accent.TButton",
                   command=self._record_voice).pack(anchor="w", pady=2)
        ttk.Button(voice, text="Add WAV files...",
                   command=self._voice_from_files).pack(anchor="w", pady=2)
        ttk.Button(voice, text="Delete this user's voice samples",
                   command=self._clear_voice).pack(anchor="w", pady=2)

        hline(wrap, pady=18)
        train = ttk.Frame(wrap)
        train.pack(fill="x")
        ttk.Button(train, text="Train both models", style="Accent.TButton",
                   command=self._train).pack(side="left")
        self.progress = ttk.Progressbar(train, mode="indeterminate", length=200)

        self.log = ReadOnlyText(wrap, height=12,
                                font=(self.app.fonts["mono"], 9),
                                highlightthickness=1,
                                highlightbackground=Theme.BORDER)
        self.log.pack(fill="both", expand=True, pady=(14, 0))

    def _card(self, parent, column: int, title: str, blurb: str):
        card = tk.Frame(parent, bg=Theme.SURFACE, highlightthickness=1,
                        highlightbackground=Theme.BORDER)
        card.grid(row=0, column=column, sticky="nsew",
                  padx=(0, 9) if column == 0 else (9, 0))
        inner = tk.Frame(card, bg=Theme.SURFACE)
        inner.pack(fill="both", expand=True, padx=16, pady=14)
        ttk.Label(inner, text=title, style="H3.TLabel").pack(anchor="w")
        ttk.Label(inner, text=blurb, style="SurfaceMuted.TLabel", wraplength=380,
                  justify="left").pack(anchor="w", pady=(2, 10))
        return inner

    # -- helpers -------------------------------------------------------

    def _selected_user_id(self) -> Optional[str]:
        value = self.user_box.get()
        if not value or "(" not in value:
            toast(self.app, "Choose an account first.", kind="warn")
            return None
        return value.split("(")[-1].rstrip(")")

    def _busy_on(self, on: bool) -> None:
        self._busy = on
        if on:
            self.progress.pack(side="left", padx=(12, 0))
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()

    def _async(self, work, label: str) -> None:
        if self._busy:
            return
        self._busy_on(True)
        self.app.set_status(label)

        def worker():
            try:
                result = work()
            except Exception as exc:
                result = "Failed: %s" % exc
            self.app.post(lambda: self._async_done(result))

        threading.Thread(target=worker, daemon=True).start()

    def _async_done(self, result) -> None:
        self._busy_on(False)
        self.app.refresh_status()
        if isinstance(result, tuple):
            text = "%s" % (result[1],)
        elif hasattr(result, "message"):
            text = result.message
        else:
            text = str(result)
        self.log.append(text + "\n")
        toast(self.app, text[:160], kind="info", ms=5000)
        self._update_counts()
        self.refresh()

    # -- actions -------------------------------------------------------

    def _capture_face(self) -> None:
        """Enrol from the camera, with a live preview so the operator can see
        what is being captured."""
        user_id = self._selected_user_id()
        if not user_id:
            return

        from .camera import camera_available, dependency_message, enroll_face

        if not camera_available():
            toast(self.app, dependency_message(), kind="bad", ms=7000)
            return

        target = int(self.services.config.get("face_enrolment_samples") or 5)
        face = self.services.biometrics.face

        def done(crops):
            if not crops:
                self.log.append("Face enrolment cancelled; nothing saved.\n")
                return
            saved, message = face.add_samples_from_crops(user_id, crops)
            self.log.append(message + "\n")
            if saved:
                ok, note = face.train()
                self.log.append("Face: " + note + "\n")
                toast(self.app, note, kind="good" if ok else "warn", ms=6000)
            self._update_counts()
            self.refresh()

        enroll_face(self, self.app, face, target=target, on_complete=done,
                    person=user_id)

    def _face_from_files(self) -> None:
        user_id = self._selected_user_id()
        if not user_id:
            return
        paths = filedialog.askopenfilenames(
            title="Choose photographs of this person",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp")],
        )
        if not paths:
            return

        def work():
            added, failed = 0, []
            for path in paths:
                result = self.services.biometrics.face.add_sample_from_file(
                    user_id, path
                )
                if result.ok:
                    added += 1
                else:
                    failed.append("%s: %s" % (os.path.basename(path), result.message))
            message = "Added %d face sample(s)." % added
            if failed:
                message += " Skipped %d: %s" % (len(failed), "; ".join(failed[:3]))
            return message

        self._async(work, "Adding face samples...")

    def _clear_face(self) -> None:
        user_id = self._selected_user_id()
        if not user_id:
            return
        removed = self.services.biometrics.face.clear_samples(user_id)
        self.log.append("Deleted %d face sample(s) for %s.\n" % (removed, user_id))
        self._update_counts()

    def _record_voice(self) -> None:
        user_id = self._selected_user_id()
        if not user_id:
            return
        self._async(
            lambda: self.services.biometrics.voice.record_sample(user_id, 3.0),
            "Recording...",
        )

    def _voice_from_files(self) -> None:
        user_id = self._selected_user_id()
        if not user_id:
            return
        paths = filedialog.askopenfilenames(
            title="Choose WAV recordings of this person",
            filetypes=[("WAV audio", "*.wav")],
        )
        if not paths:
            return

        def work():
            added, failed = 0, []
            for path in paths:
                result = self.services.biometrics.voice.add_sample_from_file(
                    user_id, path
                )
                if result.ok:
                    added += 1
                else:
                    failed.append("%s: %s" % (os.path.basename(path), result.message))
            message = "Added %d voice sample(s)." % added
            if failed:
                message += " Skipped %d: %s" % (len(failed), "; ".join(failed[:2]))
            return message

        self._async(work, "Adding voice samples...")

    def _clear_voice(self) -> None:
        user_id = self._selected_user_id()
        if not user_id:
            return
        removed = self.services.biometrics.voice.clear_samples(user_id)
        self.log.append("Deleted %d voice sample(s) for %s.\n" % (removed, user_id))
        self._update_counts()

    def _train(self) -> None:
        self._async(lambda: self.services.biometrics.train_all(),
                    "Training the biometric models...")

    # -- refresh -------------------------------------------------------

    def _update_counts(self) -> None:
        value = self.user_box.get()
        if not value or "(" not in value:
            self.counts.configure(text="")
            return
        user_id = value.split("(")[-1].rstrip(")")
        self.counts.configure(
            text="This account has %s."
                 % self.services.biometrics.enrolled_summary(user_id)
        )

    def refresh(self) -> None:
        users = [u for u in self.services.storage.list_users()
                 if models.is_account_role(u.get("role", ""))]
        values = ["%s - %s (%s)" % (u.get("username"),
                                    models.role_name(u.get("role", "")),
                                    u["user_id"]) for u in users]
        current = self.user_box.get()
        self.user_box.configure(values=values)
        if current in values:
            self.user_box.set(current)
        elif values:
            self.user_box.set(values[0])
        self._update_counts()

        face = self.services.biometrics.face
        voice = self.services.biometrics.voice
        lines = [
            "Face detector backend : %s" % face.backend_description,
            "Face model            : %s" % ("trained" if face.trained
                                            else "NOT TRAINED"),
            "Face enrolled users   : %s" % (", ".join(face.enrolled_users()) or "none"),
            "Voice model           : %s" % ("trained" if voice.trained
                                            else "NOT TRAINED"),
            "Voice enrolled users  : %s" % (", ".join(voice.enrolled_users()) or "none"),
        ]
        if not face.available():
            lines.append("WARNING: %s" % face.dependency_message())
        self.status.configure(text="\n".join(lines))


# ===========================================================================
# System diagnostics
# ===========================================================================


class SystemPage(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.services = app.services
        self._build()

    def _build(self) -> None:
        head = ttk.Frame(self)
        head.pack(fill="x", padx=20, pady=(14, 8))
        ttk.Label(head, text="System", style="H1.TLabel").pack(anchor="w")
        ttk.Label(head,
                  text="The knowledge base, the language model and the search "
                       "indexes. Use this to check the assistant is wired up "
                       "before a demo.",
                  style="Muted.TLabel", wraplength=880, justify="left"
                  ).pack(anchor="w", pady=(2, 10))

        actions = ttk.Frame(head)
        actions.pack(anchor="w")
        ttk.Button(actions, text="Reload knowledge base",
                   command=self._reload_kb).pack(side="left")
        ttk.Button(actions, text="Rebuild picture index",
                   command=self._rebuild).pack(side="left", padx=6)
        ttk.Button(actions, text="Check Ollama",
                   command=self._check_llm).pack(side="left", padx=6)
        ttk.Button(actions, text="Save all data now",
                   command=self._save).pack(side="left", padx=6)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=20, pady=14)
        self.output = ReadOnlyText(body, font=(self.app.fonts["mono"], 9),
                                   highlightthickness=1,
                                   highlightbackground=Theme.BORDER)
        scrollbar = ttk.Scrollbar(body, orient="vertical",
                                  command=self.output.yview)
        self.output.configure(yscrollcommand=scrollbar.set)
        self.output.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _reload_kb(self) -> None:
        count = self.services.retriever.reload(force=True)
        toast(self.app, "Re-indexed %d knowledge-base chunk(s)." % count,
              kind="good")
        self.refresh()

    def _rebuild(self) -> None:
        count = self.services.images.build(force=True)
        toast(self.app, "Re-described %d image(s)." % count, kind="good")
        self.refresh()

    def _check_llm(self) -> None:
        ok, status = self.services.llm.status()
        toast(self.app, status, kind="good" if ok else "warn", ms=7000)
        self.refresh()

    def _save(self) -> None:
        self.services.storage.save_all()
        self.services.config.save()
        toast(self.app, "All data written to disk.", kind="good")

    def refresh(self) -> None:
        services = self.services
        config = services.config
        ok, llm_status = services.llm.status()
        models_list = services.llm.available_models()

        lines = [
            "KNOWLEDGE BASE (RAG corpus)",
            "  chunks indexed : %d" % services.retriever.size,
            "  source files   : %s" % (", ".join(services.retriever.sources) or "none"),
            "  folder         : %s" % services.storage.kb_dir,
            "  min similarity : %.3f  (above this, a question is answered from"
            " local files)" % float(config.get("rag_min_similarity")),
            "",
            "LANGUAGE MODEL",
            "  endpoint : %s" % config.get("ollama_url"),
            "  model    : %s" % config.get("ollama_model"),
            "  status   : %s" % llm_status,
            "  pulled   : %s" % (", ".join(models_list) or "none visible"),
            "",
            "SEARCH INDEXES",
            "  cover/figure descriptors : %d" % services.images.size,
            "  books in catalogue       : %d" % len(services.storage.books),
            "",
            "BIOMETRICS",
            "  %s" % services.biometrics.status_line(),
            "",
            "BUSINESS RULES IN FORCE",
            "  fine for an unaffordable purchase : $%.2f" % config.fine_amount,
            "  VIP discount                      : %d%%" % int(config.vip_discount * 100),
            "  VIP thresholds                    : more than %d purchases or"
            " more than $%.2f" % (config.vip_purchase_threshold,
                                  config.vip_spend_threshold),
            "  warnings allowed                  : %d" % config.warning_limit,
            "  purchaser review weight           : %.1fx" % config.purchaser_weight,
            "  order-list minimum ratings        : more than %d"
            % config.min_ratings_for_list,
            "  no-order below / to-order above   : %.1f / %.1f"
            % (config.no_order_max_avg, config.to_order_min_avg),
            "  taboo words before a block        : more than %d"
            % config.max_taboo_before_block,
            "",
            "SPEC INTERPRETATION FLAGS (config/settings.json)",
        ]
        for name in ("allow_overdraft_purchase", "demotion_blocks_repromotion",
                     "order_lists_use_weighted_average", "visitors_may_review"):
            lines.append("  %-34s : %s" % (name, config.flag(name)))
        lines += [
            "",
            "STORAGE",
            "  data folder  : %s" % services.storage.data_dir,
            "  users        : %d" % len(services.storage.users),
            "  books        : %d" % len(services.storage.books),
            "  reviews      : %d" % len(services.storage.reviews),
            "  transactions : %d" % len(services.storage.transactions),
            "  events       : %d" % len(services.storage.events),
            "",
            services.chat.diagnostics(),
        ]
        self.output.set_text("\n".join(lines))
