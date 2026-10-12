"""Shopping views: the home shelf, browsing, search and the book page.

Covers spec requirements 2 (role-based top three), 5 (browse by category,
author and time; search by keyword and by picture), 6 (purchasing with its fine
and suspension rules) and 7 (rating and commenting).
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, ttk
from typing import List, Optional

from .. import imagesearch, models
from ..rules import RuleError
from .widgets import (Badge, CoverImage, ReadOnlyText, ScrollFrame, StarPicker,
                      Theme, hline, stars_text, toast)


# ===========================================================================
# Shared book card
# ===========================================================================


def book_card(parent, app, book: dict, width: int = 236,
              show_reason: str = "", rank: Optional[int] = None) -> tk.Frame:
    """One book tile, used on the home shelf and in result grids."""
    services = app.services
    card = tk.Frame(parent, bg=Theme.SURFACE, highlightthickness=1,
                    highlightbackground=Theme.BORDER, width=width)
    inner = tk.Frame(card, bg=Theme.SURFACE)
    inner.pack(fill="both", expand=True, padx=12, pady=12)

    top = tk.Frame(inner, bg=Theme.SURFACE)
    top.pack(fill="x")

    photo = CoverImage.load(book.get("cover_path", ""), size=(84, 126),
                            project_root=services.project_root,
                            title=book.get("title", ""))
    if photo is not None:
        label = tk.Label(top, image=photo, bg=Theme.SURFACE, cursor="hand2")
        label.image = photo            # keep a reference alive
        label.pack(side="left", anchor="n")
        label.bind("<Button-1>", lambda _e: app.open_book(book))

    text = tk.Frame(top, bg=Theme.SURFACE)
    text.pack(side="left", fill="both", expand=True, padx=(10, 0))

    if rank is not None:
        tk.Label(text, text="#%d" % rank, bg=Theme.SURFACE, fg=Theme.ACCENT,
                 font=(app.fonts["sans"], 8, "bold")).pack(anchor="w")

    title = tk.Label(text, text=book.get("title", ""), bg=Theme.SURFACE,
                     fg=Theme.INK, font=(app.fonts["serif"], 11, "bold"),
                     wraplength=width - 120, justify="left", anchor="w",
                     cursor="hand2")
    title.pack(anchor="w", fill="x")
    title.bind("<Button-1>", lambda _e: app.open_book(book))

    tk.Label(text, text=book.get("author", ""), bg=Theme.SURFACE,
             fg=Theme.INK_SOFT, font=(app.fonts["sans"], 9),
             wraplength=width - 120, justify="left", anchor="w").pack(anchor="w", fill="x")

    meta = "%s  -  %s" % (book.get("category", ""),
                          book.get("year") or "n.d.")
    tk.Label(text, text=meta, bg=Theme.SURFACE, fg=Theme.INK_FAINT,
             font=(app.fonts["sans"], 8)).pack(anchor="w", pady=(2, 0))

    summary = services.rules.rating_summary(book["book_id"])
    tk.Label(text, text=stars_text(summary.weighted_average, summary.count),
             bg=Theme.SURFACE, fg=Theme.WARN if summary.count else Theme.INK_FAINT,
             font=(app.fonts["sans"], 8)).pack(anchor="w", pady=(4, 0))

    bottom = tk.Frame(inner, bg=Theme.SURFACE)
    bottom.pack(fill="x", pady=(10, 0))

    price = services.rules.price_for(app.current_user, book)
    base = float(book.get("price", 0.0))
    if price < base:
        price_text = "$%.2f" % price
        tk.Label(bottom, text=price_text, bg=Theme.SURFACE, fg=Theme.ACCENT,
                 font=(app.fonts["sans"], 12, "bold")).pack(side="left")
        tk.Label(bottom, text="$%.2f" % base, bg=Theme.SURFACE,
                 fg=Theme.INK_FAINT,
                 font=(app.fonts["sans"], 9, "overstrike")).pack(side="left", padx=(4, 0))
    else:
        tk.Label(bottom, text="$%.2f" % price, bg=Theme.SURFACE, fg=Theme.ACCENT,
                 font=(app.fonts["sans"], 12, "bold")).pack(side="left")

    status = services.rules.list_status(book["book_id"])
    if status == "to-order":
        Badge(bottom, "TO-ORDER", Theme.GOOD,
              font_family=app.fonts["sans"]).pack(side="right")
    elif status == "no-order":
        Badge(bottom, "NO-ORDER", Theme.BAD,
              font_family=app.fonts["sans"]).pack(side="right")
    elif int(book.get("stock", 0)) == 0:
        Badge(bottom, "OUT OF STOCK", Theme.INK_FAINT,
              font_family=app.fonts["sans"]).pack(side="right")

    if show_reason:
        tk.Label(inner, text=show_reason, bg=Theme.SURFACE, fg=Theme.INK_SOFT,
                 font=(app.fonts["sans"], 8, "italic"),
                 wraplength=width - 24, justify="left").pack(anchor="w", pady=(8, 0))

    ttk.Button(inner, text="View", command=lambda: app.open_book(book)
               ).pack(anchor="w", pady=(10, 0))
    return card


def grid_books(parent, app, books: List[dict], columns: int = 4,
               reasons: Optional[List[str]] = None,
               ranks: bool = False) -> None:
    """Lay book cards out in a responsive-ish grid."""
    for index in range(columns):
        parent.columnconfigure(index, weight=1, uniform="bookcol")
    for i, book in enumerate(books):
        reason = reasons[i] if reasons and i < len(reasons) else ""
        card = book_card(parent, app, book, show_reason=reason,
                         rank=(i + 1) if ranks else None)
        card.grid(row=i // columns, column=i % columns, padx=7, pady=7,
                  sticky="nsew")


# ===========================================================================
# Home
# ===========================================================================


class HomeView(ttk.Frame):
    """Requirement 2: top three, personalised for RC/VC and store-wide for SU/VS."""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.services = app.services
        self._scroll = ScrollFrame(self)
        self._scroll.pack(fill="both", expand=True)

    def refresh(self) -> None:
        self._scroll.clear()
        body = self._scroll.body
        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True, padx=26, pady=20)

        user = self.app.current_user
        role = self.app.role
        personalised = role in (models.REGISTERED, models.VIP)

        heading = ttk.Frame(wrap)
        heading.pack(fill="x")
        if personalised:
            name = (user.get("display_name") or user.get("username")).split()[0]
            ttk.Label(heading, text="Picked for you, %s" % name,
                      style="H1.TLabel").pack(anchor="w")
            explanation = ("Chosen from the books you have browsed and bought. "
                           "Titles on the no-order list are left out.")
        else:
            ttk.Label(heading, text="Most popular at Inkwell & Co.",
                      style="H1.TLabel").pack(anchor="w")
            explanation = (
                "The three most purchased and most browsed titles in the shop. "
                + ("Sign in for recommendations based on your own reading."
                   if role == models.VISITOR else
                   "Super-users see the store-wide picture rather than a "
                   "personal one.")
            )
        ttk.Label(heading, text=explanation, style="Muted.TLabel",
                  wraplength=860, justify="left").pack(anchor="w", pady=(4, 18))

        recs = self.services.recommender.top_for(
            user, self.services.config.recommendation_count
        )
        if recs:
            shelf = ttk.Frame(wrap)
            shelf.pack(fill="x")
            grid_books(shelf, self.app, [r.book for r in recs], columns=3,
                       reasons=[r.reason for r in recs], ranks=True)
        else:
            ttk.Label(wrap, text="The catalogue is empty. The super-user can add "
                                 "books from the Manage store tab.",
                      style="Muted.TLabel").pack(anchor="w")

        hline(wrap, pady=22)

        # A second shelf: newest arrivals, which gives the home page some body.
        ttk.Label(wrap, text="Recently added", style="H2.TLabel").pack(anchor="w")
        ttk.Label(wrap, text="The latest titles on our shelves.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 12))

        recent = sorted(
            self.services.catalog.all_books(),
            key=lambda b: str(b.get("added_at", "")), reverse=True,
        )[:4]
        if recent:
            shelf = ttk.Frame(wrap)
            shelf.pack(fill="x")
            grid_books(shelf, self.app, recent, columns=4)

        # VIP and suspension notices.
        if user:
            self._notices(wrap, user)

    def _notices(self, parent, user: dict) -> None:
        config = self.services.config
        notes = []
        if user.get("role") == models.REGISTERED:
            purchases = int(user.get("purchase_count", 0))
            spent = float(user.get("total_spent", 0.0))
            remaining_p = config.vip_purchase_threshold + 1 - purchases
            remaining_s = config.vip_spend_threshold + 0.01 - spent
            if remaining_p <= 2 or remaining_s <= 40:
                notes.append(
                    ("good",
                     "You are close to VIP: %d more purchase%s or $%.2f more "
                     "spent unlocks a %d%% discount on everything."
                     % (max(0, remaining_p), "" if remaining_p == 1 else "s",
                        max(0.0, remaining_s), int(config.vip_discount * 100))))
        balance = float(user.get("balance", 0.0))
        if balance < config.fine_amount:
            notes.append(
                ("warn",
                 "Your balance is $%.2f. A purchase you cannot cover is refused "
                 "and charged a $%.2f fine, and a negative balance suspends the "
                 "account." % (balance, config.fine_amount)))
        if int(user.get("warnings", 0)) > 0:
            notes.append(
                ("warn",
                 "You have %d moderation warning%s. More than %d leads to %s."
                 % (int(user["warnings"]),
                    "" if user["warnings"] == 1 else "s",
                    config.warning_limit,
                    "demotion from VIP" if user.get("role") == models.VIP
                    else "suspension")))

        if not notes:
            return
        hline(parent, pady=20)
        for kind, text in notes:
            colour = Theme.GOOD if kind == "good" else Theme.WARN
            box = tk.Frame(parent, bg=Theme.SURFACE, highlightthickness=1,
                           highlightbackground=colour)
            box.pack(fill="x", pady=4)
            tk.Label(box, text=text, bg=Theme.SURFACE, fg=colour,
                     font=(self.app.fonts["sans"], 9), wraplength=880,
                     justify="left", anchor="w", padx=14, pady=10).pack(fill="x")


# ===========================================================================
# Browse (and the book detail page)
# ===========================================================================


class BrowseView(ttk.Frame):
    """Requirement 5: browse by category, author and time."""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.services = app.services
        self._mode = tk.StringVar(value="category")
        self._selection: Optional[str] = None
        self._book: Optional[dict] = None
        self._build()

    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=20, pady=(16, 8))

        ttk.Label(bar, text="Browse by", style="H2.TLabel").pack(side="left")
        for value, label in (("category", "Category"), ("author", "Author"),
                             ("decade", "Time")):
            ttk.Radiobutton(bar, text=label, value=value, variable=self._mode,
                            command=self._mode_changed).pack(side="left", padx=(14, 0))

        self._facet_bar = ttk.Frame(self)
        self._facet_bar.pack(fill="x", padx=20, pady=(0, 8))

        self._scroll = ScrollFrame(self)
        self._scroll.pack(fill="both", expand=True)

    def _mode_changed(self) -> None:
        self._selection = None
        self._book = None
        self.refresh()

    # -- facets --------------------------------------------------------

    def _facets(self):
        mode = self._mode.get()
        if mode == "author":
            return self.services.catalog.authors()
        if mode == "decade":
            return self.services.catalog.decades()
        return self.services.catalog.categories()

    def _books_for(self, value: str) -> List[dict]:
        mode = self._mode.get()
        if mode == "author":
            return self.services.catalog.by_author(value)
        if mode == "decade":
            return self.services.catalog.by_decade(value)
        return self.services.catalog.by_category(value)

    def show_book(self, book: dict) -> None:
        """Open a book's page, and move the facet selection to match it.

        A book can be opened from the home shelf or from search, where the
        current facet has nothing to do with it; without this the Back button
        would offer to return to an unrelated category.
        """
        self._book = book
        mode = self._mode.get()
        if mode == "author":
            self._selection = book.get("author") or self._selection
        elif mode == "decade":
            year = int(book.get("year") or 0)
            self._selection = "Unknown" if year <= 0 else "%ds" % (year // 10 * 10)
        else:
            self._selection = (book.get("category") or self._selection)
        self.refresh()

    # -- rendering -----------------------------------------------------

    def refresh(self) -> None:
        for child in self._facet_bar.winfo_children():
            child.destroy()

        facets = self._facets()
        if self._selection is None and facets:
            self._selection = facets[0][0]

        chips = ttk.Frame(self._facet_bar)
        chips.pack(fill="x")
        for name, count in facets:
            selected = name == self._selection and self._book is None
            button = tk.Button(
                chips, text="%s (%d)" % (name, count), bd=0, cursor="hand2",
                bg=Theme.ACCENT if selected else Theme.SURFACE,
                fg="#ffffff" if selected else Theme.INK,
                activebackground=Theme.ACCENT_DARK if selected else Theme.SURFACE_ALT,
                font=(self.app.fonts["sans"], 9,
                      "bold" if selected else "normal"),
                padx=12, pady=5,
                command=lambda n=name: self._select(n),
            )
            button.pack(side="left", padx=(0, 6), pady=2)

        self._scroll.clear()
        self._scroll.scroll_to_top()
        if self._book is not None:
            self._render_detail(self._scroll.body, self._book)
        else:
            self._render_list(self._scroll.body)

    def _select(self, name: str) -> None:
        self._selection = name
        self._book = None
        self.refresh()

    def _render_list(self, body) -> None:
        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True, padx=20, pady=12)

        books = self._books_for(self._selection) if self._selection else []
        label = {"category": "in", "author": "by", "decade": "from"}[self._mode.get()]
        ttk.Label(wrap, text="%d title%s %s %s"
                             % (len(books), "" if len(books) == 1 else "s",
                                label, self._selection or "-"),
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 10))

        if not books:
            ttk.Label(wrap, text="Nothing here yet.", style="Muted.TLabel"
                      ).pack(anchor="w")
            return

        grid = ttk.Frame(wrap)
        grid.pack(fill="both", expand=True)
        grid_books(grid, self.app, books, columns=4)

    # -- book detail ---------------------------------------------------

    def _render_detail(self, body, book: dict) -> None:
        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True, padx=26, pady=18)

        ttk.Button(wrap, text="< Back to %s" % (self._selection or "list"),
                   command=lambda: self._select(self._selection)).pack(anchor="w")

        head = ttk.Frame(wrap)
        head.pack(fill="x", pady=(14, 0))

        photo = CoverImage.load(book.get("cover_path", ""), size=(190, 285),
                                project_root=self.services.project_root,
                                title=book.get("title", ""))
        if photo is not None:
            label = tk.Label(head, image=photo, bg=Theme.BG)
            label.image = photo
            label.pack(side="left", anchor="n")

        info = ttk.Frame(head)
        info.pack(side="left", fill="both", expand=True, padx=(22, 0))

        ttk.Label(info, text=book.get("title", ""), style="H1.TLabel",
                  wraplength=620).pack(anchor="w")
        ttk.Label(info, text="by %s" % book.get("author", ""),
                  style="H2.TLabel").pack(anchor="w", pady=(2, 8))

        facts = ttk.Frame(info)
        facts.pack(anchor="w", pady=(0, 10))
        for key, value in (
            ("Category", book.get("category", "")),
            ("Published", str(book.get("year") or "unknown")),
            ("In stock", str(int(book.get("stock", 0)))),
            ("Viewed", str(int(book.get("browse_count", 0)))),
            ("Sold", str(int(book.get("purchase_count", 0)))),
        ):
            cell = ttk.Frame(facts)
            cell.pack(side="left", padx=(0, 26))
            ttk.Label(cell, text=key.upper(), style="Muted.TLabel").pack(anchor="w")
            ttk.Label(cell, text=value, style="TLabel",
                      font=(self.app.fonts["sans"], 11, "bold")).pack(anchor="w")

        summary = self.services.rules.rating_summary(book["book_id"])
        status = self.services.rules.list_status(book["book_id"])
        rating_row = ttk.Frame(info)
        rating_row.pack(anchor="w", pady=(0, 8))
        ttk.Label(rating_row,
                  text=stars_text(summary.weighted_average, summary.count),
                  style="TLabel",
                  font=(self.app.fonts["sans"], 11)).pack(side="left")
        if summary.count:
            ttk.Label(rating_row,
                      text="   (weighted; plain average %.2f, %d verified buyer%s)"
                           % (summary.plain_average, summary.purchaser_count,
                              "" if summary.purchaser_count == 1 else "s"),
                      style="Muted.TLabel").pack(side="left")
        if status != "normal":
            colour = Theme.GOOD if status == "to-order" else Theme.BAD
            note = ("More than %d ratings averaging above %.1f, so this is on "
                    "our to-order list -- we keep it in stock permanently."
                    % (self.services.config.min_ratings_for_list,
                       self.services.config.to_order_min_avg)
                    if status == "to-order" else
                    "More than %d ratings averaging below %.1f, so this is on "
                    "our no-order list -- we will not be reordering it."
                    % (self.services.config.min_ratings_for_list,
                       self.services.config.no_order_max_avg))
            box = tk.Frame(info, bg=Theme.SURFACE, highlightthickness=1,
                           highlightbackground=colour)
            box.pack(fill="x", pady=(2, 10))
            tk.Label(box, text=note, bg=Theme.SURFACE, fg=colour,
                     font=(self.app.fonts["sans"], 9), wraplength=600,
                     justify="left", anchor="w", padx=12, pady=8).pack(fill="x")

        if book.get("description"):
            ttk.Label(info, text=book["description"], style="Muted.TLabel",
                      wraplength=620, justify="left").pack(anchor="w", pady=(0, 12))

        self._render_buy_box(info, book)

        figures = [f for f in (book.get("figure_paths") or [])]
        if figures:
            hline(wrap, pady=18)
            ttk.Label(wrap, text="Figures from inside this book",
                      style="H2.TLabel").pack(anchor="w")
            ttk.Label(wrap, text="These are indexed by picture search too.",
                      style="Muted.TLabel").pack(anchor="w", pady=(2, 8))
            strip = ttk.Frame(wrap)
            strip.pack(anchor="w")
            for path in figures[:4]:
                photo = CoverImage.load(path, size=(190, 150),
                                        project_root=self.services.project_root,
                                        title="figure")
                if photo is not None:
                    label = tk.Label(strip, image=photo, bg=Theme.BG)
                    label.image = photo
                    label.pack(side="left", padx=(0, 10))

        hline(wrap, pady=18)
        self._render_reviews(wrap, book)

    def _render_buy_box(self, parent, book: dict) -> None:
        user = self.app.current_user
        price = self.services.rules.price_for(user, book)
        base = float(book.get("price", 0.0))

        box = tk.Frame(parent, bg=Theme.SURFACE, highlightthickness=1,
                       highlightbackground=Theme.BORDER)
        box.pack(fill="x", pady=(4, 0))
        inner = tk.Frame(box, bg=Theme.SURFACE)
        inner.pack(fill="x", padx=16, pady=14)

        row = tk.Frame(inner, bg=Theme.SURFACE)
        row.pack(fill="x")

        tk.Label(row, text="$%.2f" % price, bg=Theme.SURFACE, fg=Theme.ACCENT,
                 font=(self.app.fonts["sans"], 20, "bold")).pack(side="left")
        if price < base:
            tk.Label(row, text="list $%.2f, VIP discount applied" % base,
                     bg=Theme.SURFACE, fg=Theme.GOOD,
                     font=(self.app.fonts["sans"], 9)).pack(side="left", padx=(10, 0))

        if user is None:
            tk.Label(inner, text="Sign in to buy. Visitors can browse and search "
                                 "but cannot purchase or review.",
                     bg=Theme.SURFACE, fg=Theme.INK_SOFT,
                     font=(self.app.fonts["sans"], 9), wraplength=520,
                     justify="left").pack(anchor="w", pady=(8, 0))
            return

        if user.get("role") == models.SUPER_USER:
            tk.Label(inner, text="Super-user accounts manage the shop rather than "
                                 "buying from it.",
                     bg=Theme.SURFACE, fg=Theme.INK_SOFT,
                     font=(self.app.fonts["sans"], 9)).pack(anchor="w", pady=(8, 0))
            return

        balance = float(user.get("balance", 0.0))
        tk.Label(inner, text="Your balance: $%.2f" % balance, bg=Theme.SURFACE,
                 fg=Theme.INK_SOFT if balance >= price else Theme.WARN,
                 font=(self.app.fonts["sans"], 9)).pack(anchor="w", pady=(8, 0))

        if balance < price:
            tk.Label(
                inner,
                text="This costs more than your balance. Buying anyway is "
                     "refused and charges a $%.2f fine."
                     % self.services.config.fine_amount,
                bg=Theme.SURFACE, fg=Theme.WARN,
                font=(self.app.fonts["sans"], 9), wraplength=520,
                justify="left",
            ).pack(anchor="w", pady=(4, 0))

        buttons = tk.Frame(inner, bg=Theme.SURFACE)
        buttons.pack(anchor="w", pady=(12, 0))
        state = "normal" if int(book.get("stock", 0)) > 0 else "disabled"
        ttk.Button(buttons, text="Buy this book", style="Accent.TButton",
                   state=state,
                   command=lambda: self._buy(book)).pack(side="left")
        if state == "disabled":
            tk.Label(buttons, text="Out of stock", bg=Theme.SURFACE,
                     fg=Theme.INK_FAINT,
                     font=(self.app.fonts["sans"], 9)).pack(side="left", padx=(10, 0))

    def _buy(self, book: dict) -> None:
        user = self.app.current_user
        if user is None:
            return
        result = self.services.rules.purchase(user, book)
        self.app.reload_user()
        self.app.rebuild_tabs()
        toast(self.app, result.message,
              kind="good" if result.ok else ("bad" if result.suspended else "warn"),
              ms=6000)
        if result.suspended:
            self.app.log_out()
            return
        self.app.refresh_all()
        fresh = self.services.storage.get_book(book["book_id"])
        if fresh:
            self.show_book(fresh)

    # -- reviews -------------------------------------------------------

    def _render_reviews(self, parent, book: dict) -> None:
        ttk.Label(parent, text="Ratings and comments", style="H2.TLabel"
                  ).pack(anchor="w")
        ttk.Label(
            parent,
            text="Comments from customers who bought the book count %.0fx in the "
                 "average. Taboo words are masked, and a comment with more than "
                 "%d is blocked and earns its author a warning."
                 % (self.services.config.purchaser_weight / max(1e-9, self.services.config.normal_weight),
                    self.services.config.max_taboo_before_block),
            style="Muted.TLabel", wraplength=860, justify="left",
        ).pack(anchor="w", pady=(2, 12))

        self._render_review_form(parent, book)

        reviews = self.services.storage.reviews_for_book(book["book_id"])
        if not reviews:
            ttk.Label(parent, text="No ratings yet. Be the first.",
                      style="Muted.TLabel").pack(anchor="w", pady=(12, 0))
            return

        for review in sorted(reviews, key=lambda r: r.get("created_at", ""),
                             reverse=True):
            self._render_review(parent, review)

    def _render_review(self, parent, review: dict) -> None:
        from ..rules import RulesEngine

        author_id = review.get("user_id", "")
        if author_id == RulesEngine.ANONYMOUS_AUTHOR:
            name = "A visitor"
        else:
            author = self.services.storage.get_user(author_id)
            name = (author.get("display_name") or author.get("username")
                    if author else "a former customer")

        box = tk.Frame(parent, bg=Theme.SURFACE, highlightthickness=1,
                       highlightbackground=Theme.BORDER)
        box.pack(fill="x", pady=5)
        inner = tk.Frame(box, bg=Theme.SURFACE)
        inner.pack(fill="x", padx=14, pady=11)

        head = tk.Frame(inner, bg=Theme.SURFACE)
        head.pack(fill="x")
        tk.Label(head, text="★" * int(review.get("stars", 0))
                      + "☆" * (5 - int(review.get("stars", 0))),
                 bg=Theme.SURFACE, fg=Theme.ACCENT,
                 font=(self.app.fonts["sans"], 11)).pack(side="left")
        tk.Label(head, text="  %s" % name, bg=Theme.SURFACE, fg=Theme.INK,
                 font=(self.app.fonts["sans"], 9, "bold")).pack(side="left")
        if review.get("verified_purchase"):
            Badge(head, "VERIFIED BUYER", Theme.GOOD,
                  font_family=self.app.fonts["sans"]).pack(side="left", padx=8)
        tk.Label(head, text=str(review.get("created_at", ""))[:10],
                 bg=Theme.SURFACE, fg=Theme.INK_FAINT,
                 font=(self.app.fonts["sans"], 8)).pack(side="right")

        if review.get("comment"):
            tk.Label(inner, text=review["comment"], bg=Theme.SURFACE,
                     fg=Theme.INK, font=(self.app.fonts["sans"], 10),
                     wraplength=840, justify="left", anchor="w"
                     ).pack(fill="x", pady=(6, 0))

        if int(review.get("taboo_hits", 0)) > 0:
            tk.Label(inner,
                     text="%d taboo word(s) masked by the moderation filter."
                          % int(review["taboo_hits"]),
                     bg=Theme.SURFACE, fg=Theme.WARN,
                     font=(self.app.fonts["sans"], 8)).pack(anchor="w", pady=(4, 0))

    def _render_review_form(self, parent, book: dict) -> None:
        user = self.app.current_user
        box = tk.Frame(parent, bg=Theme.SURFACE_ALT)
        box.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(box, bg=Theme.SURFACE_ALT)
        inner.pack(fill="x", padx=14, pady=12)

        visitors_allowed = self.services.config.flag("visitors_may_review")

        if user is None and not visitors_allowed:
            tk.Label(inner,
                     text="Sign in to leave a rating. Reviews are tied to an "
                          "account so moderation warnings can be applied.",
                     bg=Theme.SURFACE_ALT, fg=Theme.INK_SOFT,
                     font=(self.app.fonts["sans"], 9)).pack(anchor="w")
            return

        if user is not None and self.services.storage.user_has_reviewed(
                user["user_id"], book["book_id"]):
            tk.Label(inner, text="You have already reviewed this book.",
                     bg=Theme.SURFACE_ALT, fg=Theme.INK_SOFT,
                     font=(self.app.fonts["sans"], 9)).pack(anchor="w")
            return

        tk.Label(inner, text="Leave a rating", bg=Theme.SURFACE_ALT,
                 fg=Theme.INK, font=(self.app.fonts["sans"], 10, "bold")
                 ).pack(anchor="w")

        if user is None:
            # Requirement 7 says any user may comment, so a visitor may -- but
            # it is worth telling them what signing in would buy them.
            tk.Label(inner,
                     text="Posting as a visitor. Sign in and your rating is "
                          "kept with your account, and counts double on books "
                          "you bought.",
                     bg=Theme.SURFACE_ALT, fg=Theme.INK_SOFT,
                     wraplength=820, justify="left",
                     font=(self.app.fonts["sans"], 9)).pack(anchor="w",
                                                             pady=(2, 4))

        picker = StarPicker(inner, background=Theme.SURFACE_ALT)
        picker.pack(anchor="w", pady=(4, 8))

        entry = tk.Text(inner, height=3, wrap="word", bd=0,
                        highlightthickness=1, highlightbackground=Theme.BORDER,
                        bg=Theme.SURFACE, fg=Theme.INK, padx=8, pady=6,
                        font=(self.app.fonts["sans"], 10))
        entry.pack(fill="x")

        message = tk.Label(inner, text="", bg=Theme.SURFACE_ALT,
                           fg=Theme.INK_SOFT, font=(self.app.fonts["sans"], 9),
                           wraplength=840, justify="left", anchor="w")

        def submit():
            result = self.services.rules.submit_review(
                user, book, picker.get(), entry.get("1.0", "end").strip()
            )
            self.app.reload_user()
            self.app.rebuild_tabs()
            toast(self.app, result.message,
                  kind="good" if result.ok else "warn", ms=7000)
            if result.suspended:
                self.app.log_out()
                return
            if result.ok or result.blocked:
                self.app.refresh_all()
                self.show_book(self.services.storage.get_book(book["book_id"]) or book)
            else:
                message.configure(text=result.message, fg=Theme.BAD)
                message.pack(anchor="w", pady=(8, 0))

        ttk.Button(inner, text="Post rating", style="Accent.TButton",
                   command=submit).pack(anchor="w", pady=(10, 0))


# ===========================================================================
# Search
# ===========================================================================


class SearchView(ttk.Frame):
    """Requirement 5: search by keyword and/or picture."""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.services = app.services
        self._query_image: Optional[str] = None
        self._build()

    def _build(self) -> None:
        wrap = ttk.Frame(self)
        wrap.pack(fill="x", padx=22, pady=(16, 6))

        ttk.Label(wrap, text="Search the catalogue", style="H1.TLabel").pack(anchor="w")
        ttk.Label(wrap,
                  text="Search by keyword across titles, authors, descriptions "
                       "and the books' own text -- or upload a picture of a cover "
                       "or an internal figure.",
                  style="Muted.TLabel", wraplength=860, justify="left"
                  ).pack(anchor="w", pady=(4, 14))

        row = ttk.Frame(wrap)
        row.pack(fill="x")
        self.entry = ttk.Entry(row, font=(self.app.fonts["sans"], 12))
        self.entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.entry.bind("<Return>", lambda _e: self._keyword_search())
        ttk.Button(row, text="Search", style="Accent.TButton",
                   command=self._keyword_search).pack(side="left", padx=(8, 0))

        picture_row = ttk.Frame(wrap)
        picture_row.pack(fill="x", pady=(10, 0))
        self.picture_button = ttk.Button(
            picture_row, text="Search by picture...", command=self._picture_search
        )
        self.picture_button.pack(side="left")
        self.picture_label = ttk.Label(picture_row, text="", style="Muted.TLabel")
        self.picture_label.pack(side="left", padx=(10, 0))

        self.summary_label = ttk.Label(wrap, text="", style="Muted.TLabel")
        self.summary_label.pack(anchor="w", pady=(12, 0))

        self._scroll = ScrollFrame(self)
        self._scroll.pack(fill="both", expand=True)

    # -- keyword -------------------------------------------------------

    def _keyword_search(self) -> None:
        query = self.entry.get().strip()
        self._scroll.clear()
        self._scroll.scroll_to_top()
        if not query:
            self.summary_label.configure(text="Type something to search for.")
            return

        hits = self.services.catalog.search(query, limit=24)
        self.summary_label.configure(
            text="%d result%s for \"%s\"" % (len(hits),
                                             "" if len(hits) == 1 else "s", query)
        )
        if not hits:
            ttk.Label(self._scroll.body,
                      text="Nothing matched. Try an author's surname or a "
                           "broader keyword.",
                      style="Muted.TLabel").pack(anchor="w", padx=22, pady=14)
            return

        wrap = ttk.Frame(self._scroll.body)
        wrap.pack(fill="both", expand=True, padx=16, pady=10)
        reasons = ["matched %s in %s" % (", ".join(h.matched[:4]), h.where)
                   for h in hits]
        grid_books(wrap, self.app, [h.book for h in hits], columns=4,
                   reasons=reasons)

    # -- picture -------------------------------------------------------

    def _picture_search(self) -> None:
        if not imagesearch.available():
            toast(self.app, imagesearch.missing_dependency_message(), kind="bad",
                  ms=7000)
            return

        path = filedialog.askopenfilename(
            title="Choose a cover or figure to search with",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp"),
                       ("All files", "*.*")],
        )
        if not path:
            return

        self._query_image = path
        self.picture_label.configure(text="Searching with %s..."
                                          % os.path.basename(path))
        self.app.set_status("Building the cover index...")
        self.services.images.build()
        hits = self.services.images.search(
            path, limit=int(self.services.config.get("image_search_results") or 8)
        )
        self.app.refresh_status()

        self._scroll.clear()
        self._scroll.scroll_to_top()
        self.picture_label.configure(text="Searched with %s"
                                          % os.path.basename(path))
        self.summary_label.configure(
            text="%d visual match%s against %d indexed image%s"
                 % (len(hits), "" if len(hits) == 1 else "es",
                    self.services.images.size,
                    "" if self.services.images.size == 1 else "s")
        )

        if not hits:
            ttk.Label(self._scroll.body,
                      text="No visual matches. The index may be empty -- the "
                           "super-user can add covers from the Manage store tab.",
                      style="Muted.TLabel").pack(anchor="w", padx=22, pady=14)
            return

        wrap = ttk.Frame(self._scroll.body)
        wrap.pack(fill="both", expand=True, padx=16, pady=10)

        preview = ttk.Frame(wrap)
        preview.pack(fill="x", pady=(0, 10))
        photo = CoverImage.load(path, size=(110, 160), title="your image")
        if photo is not None:
            label = tk.Label(preview, image=photo, bg=Theme.BG)
            label.image = photo
            label.pack(side="left")
        ttk.Label(preview,
                  text="Your image, and the closest covers and figures below.\n"
                       "Matching combines a 3x3 colour-layout histogram, a "
                       "perceptual hash and per-cell edge density.",
                  style="Muted.TLabel", justify="left").pack(side="left", padx=14)

        grid = ttk.Frame(wrap)
        grid.pack(fill="both", expand=True)
        reasons = ["%.0f%% match on the %s" % (h.similarity * 100, h.kind)
                   for h in hits]
        grid_books(grid, self.app, [h.book for h in hits], columns=4,
                   reasons=reasons)

    def refresh(self) -> None:
        if not imagesearch.available():
            self.picture_button.configure(state="disabled")
            self.picture_label.configure(
                text=imagesearch.missing_dependency_message()
            )
