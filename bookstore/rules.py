"""The business-rules engine.

This is the heart of the project: every numbered rule in the spec that is not
about the UI or the AI lives here, in one place, with no Tkinter and no model
loading.  That makes all of it unit-testable (see ``tests/test_rules.py``).

Requirement map
---------------
R1   roles                      -> models.py + ``Session``
R6   balance, fine, suspension  -> ``purchase``, ``deposit``, ``reopen_account``
R7   weighted reviews           -> ``submit_review``, ``rating_summary``
R8   no-order / to-order lists  -> ``rating_summary``, ``order_lists``
R9   taboo words and warnings   -> ``submit_review`` (via moderation.py)
R10  warning consequences       -> ``_apply_warning``
R11  VIP promotion and discount -> ``_check_promotion``, ``price_for``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import models
from .config import Config
from .moderation import screen_comment
from .storage import Storage, utc_now


class RuleError(Exception):
    """Raised when an action is refused by a business rule."""


# ---------------------------------------------------------------------------
# Results returned to the GUI.  Dataclasses rather than bare tuples so the
# view code reads clearly.
# ---------------------------------------------------------------------------


@dataclass
class PurchaseResult:
    ok: bool
    message: str
    price_paid: float = 0.0
    fine_applied: float = 0.0
    new_balance: float = 0.0
    promoted: bool = False
    suspended: bool = False


@dataclass
class ReviewResult:
    ok: bool
    message: str
    blocked: bool = False
    taboo_hits: int = 0
    warning_issued: bool = False
    warnings_total: int = 0
    suspended: bool = False
    demoted: bool = False


@dataclass
class RatingSummary:
    book_id: str
    count: int = 0                 # unweighted number of visible ratings
    weighted_average: float = 0.0
    plain_average: float = 0.0
    purchaser_count: int = 0

    @property
    def average(self) -> float:
        return self.weighted_average


class RulesEngine:
    """Applies the spec's rules against a :class:`Storage`."""

    def __init__(self, storage: Storage, config: Config):
        self.db = storage
        self.cfg = config

    # ======================================================================
    # Accounts and authentication state (R1, R6)
    # ======================================================================

    def create_account(
        self,
        username: str,
        password: str,
        role: str = models.REGISTERED,
        balance: float = 0.0,
        display_name: str = "",
    ) -> dict:
        username = (username or "").strip()
        if not username:
            raise RuleError("Username cannot be empty.")
        if len(password or "") < 4:
            raise RuleError("Password must be at least 4 characters.")
        if self.db.find_user_by_username(username):
            raise RuleError("That username is already taken.")
        if role not in (models.SUPER_USER, models.REGISTERED, models.VIP):
            raise RuleError("Unknown role: %s" % role)
        if balance < 0:
            raise RuleError("Opening balance cannot be negative.")

        user = models.new_user(
            self.db.next_user_id(),
            username,
            password,
            role=role,
            balance=balance,
            display_name=display_name,
        )
        self.db.put_user(user)
        if balance:
            self.db.add_transaction(
                "deposit", user["user_id"], balance, note="opening balance"
            )
        self.db.add_event("account_created", user["user_id"], "role=%s" % role)
        return user

    def can_log_in(self, user: dict) -> Tuple[bool, str]:
        """R6: a suspended account cannot log in until the SU reopens it."""
        models.normalise_user(user)
        if user.get("suspended"):
            reason = user.get("suspend_reason") or "account suspended"
            return False, (
                "This account is suspended (%s). Ask the super-user to reopen it "
                "with a positive deposit." % reason
            )
        return True, ""

    def deposit(self, user: dict, amount: float, by_super_user: bool = False) -> float:
        """Add money to an account.

        R6: depositing enough to bring the balance positive is what lets the
        super-user reopen a suspended account.
        """
        models.normalise_user(user)
        if amount <= 0:
            raise RuleError("Deposit must be a positive amount.")
        user["balance"] = round(float(user["balance"]) + float(amount), 2)
        self.db.add_transaction(
            "deposit",
            user["user_id"],
            -abs(amount),  # money in from the customer is not store revenue
            note="deposit by SU" if by_super_user else "self deposit",
        )
        self.db.put_user(user)
        return user["balance"]

    def reopen_account(self, user: dict, deposit_amount: float) -> dict:
        """R6: SU reopens a suspended account with a positive deposit.

        Suspension for warnings (R10) is a moderation action, not a money
        problem, so clearing it does not require the balance to be positive --
        but a suspended-for-debt account must end up in the black.
        """
        models.normalise_user(user)
        if not user.get("suspended"):
            raise RuleError("That account is not suspended.")
        if deposit_amount > 0:
            self.deposit(user, deposit_amount, by_super_user=True)
        if float(user["balance"]) < 0:
            raise RuleError(
                "Balance is still negative (%.2f). Deposit enough to clear the debt."
                % user["balance"]
            )
        user["suspended"] = False
        user["suspend_reason"] = ""
        user["warnings"] = 0          # the SU's reinstatement wipes the slate
        user["vip_ineligible"] = False
        self.db.put_user(user)
        self.db.add_event("account_reopened", user["user_id"], "by super-user")
        return user

    def suspend(self, user: dict, reason: str) -> dict:
        models.normalise_user(user)
        user["suspended"] = True
        user["suspend_reason"] = reason
        self.db.put_user(user)
        self.db.add_event("suspended", user["user_id"], reason)
        return user

    # ======================================================================
    # Pricing and purchases (R6, R11)
    # ======================================================================

    def price_for(self, user: Optional[dict], book: dict) -> float:
        """R11: a VIP customer gets 10% off every purchase."""
        price = float(book.get("price", 0.0))
        if user and user.get("role") == models.VIP:
            price = price * (1.0 - self.cfg.vip_discount)
        return round(price, 2)

    def purchase(self, user: dict, book: dict) -> PurchaseResult:
        """R6 + R11.

        Default reading of the spec (``allow_overdraft_purchase = false``): a
        purchase costing more than the balance is **refused** and a $1 fine is
        applied for the attempt.  Repeated attempts can therefore drive the
        balance negative, which triggers suspension.

        Flip ``allow_overdraft_purchase`` to ``true`` in settings.json for the
        other reading, where the purchase goes through and the balance is
        allowed to go negative.
        """
        models.normalise_user(user)

        allowed, why = self.can_log_in(user)
        if not allowed:
            return PurchaseResult(False, why)

        if int(book.get("stock", 0)) <= 0:
            return PurchaseResult(False, "'%s' is out of stock." % book.get("title"))

        price = self.price_for(user, book)
        balance = float(user["balance"])

        if price > balance:
            fine = self.cfg.fine_amount
            user["balance"] = round(balance - fine, 2)
            user["total_fines"] = round(float(user.get("total_fines", 0.0)) + fine, 2)
            self.db.add_transaction(
                "fine", user["user_id"], fine, book_id=book["book_id"],
                note="insufficient funds for '%s'" % book.get("title"),
            )
            self.db.add_event(
                "fine", user["user_id"],
                "$%.2f fine: tried to buy '%s' (%.2f) with %.2f"
                % (fine, book.get("title"), price, balance),
            )

            if self.cfg.flag("allow_overdraft_purchase"):
                # Alternative reading: let the purchase through anyway.
                result = self._settle_purchase(user, book, price)
                result.fine_applied = fine
                result.message = (
                    "Purchased '%s' for $%.2f. Balance was short, so a $%.2f fine "
                    "was applied. New balance $%.2f."
                    % (book.get("title"), price, fine, user["balance"])
                )
                result.suspended = self._enforce_negative_balance(user)
                return result

            self.db.put_user(user)
            suspended = self._enforce_negative_balance(user)
            message = (
                "Purchase refused: '%s' costs $%.2f but your balance is $%.2f. "
                "A $%.2f fine was applied; balance is now $%.2f."
                % (book.get("title"), price, balance, fine, user["balance"])
            )
            if suspended:
                message += " Your account is now suspended because the balance is negative."
            return PurchaseResult(
                False, message, fine_applied=fine,
                new_balance=user["balance"], suspended=suspended,
            )

        result = self._settle_purchase(user, book, price)
        result.message = "Purchased '%s' for $%.2f. New balance $%.2f." % (
            book.get("title"), price, user["balance"],
        )
        if result.promoted:
            result.message += " You have been promoted to VIP -- 10%% off from now on."
        return result

    def _settle_purchase(self, user: dict, book: dict, price: float) -> PurchaseResult:
        user["balance"] = round(float(user["balance"]) - price, 2)
        user["purchase_count"] = int(user.get("purchase_count", 0)) + 1
        user["total_spent"] = round(float(user.get("total_spent", 0.0)) + price, 2)
        user.setdefault("purchase_history", []).append(
            {"book_id": book["book_id"], "price_paid": price, "at": utc_now()}
        )

        book["stock"] = max(0, int(book.get("stock", 0)) - 1)
        book["purchase_count"] = int(book.get("purchase_count", 0)) + 1

        self.db.add_transaction(
            "purchase", user["user_id"], price, book_id=book["book_id"],
            note=book.get("title", ""),
        )

        promoted = self._check_promotion(user)
        self.db.put_user(user)
        self.db.put_book(book)

        return PurchaseResult(
            True, "", price_paid=price, new_balance=user["balance"], promoted=promoted
        )

    def _enforce_negative_balance(self, user: dict) -> bool:
        """R6: any account with a negative balance is suspended."""
        if float(user.get("balance", 0.0)) < 0 and not user.get("suspended"):
            self.suspend(user, "negative balance")
            return True
        self.db.put_user(user)
        return False

    # ======================================================================
    # Promotion and demotion (R10, R11)
    # ======================================================================

    def _check_promotion(self, user: dict) -> bool:
        """R11: an RC with >3 purchases or >$200 spent becomes a VIP.

        Both thresholds are strictly greater-than, exactly as worded.  A user
        demoted for warnings carries ``vip_ineligible`` so their old history
        cannot immediately re-promote them (see ARCHITECTURE.md).
        """
        if user.get("role") != models.REGISTERED:
            return False
        if self.cfg.flag("demotion_blocks_repromotion") and user.get("vip_ineligible"):
            return False

        purchases = int(user.get("purchase_count", 0))
        spent = float(user.get("total_spent", 0.0))
        if purchases > self.cfg.vip_purchase_threshold or spent > self.cfg.vip_spend_threshold:
            user["role"] = models.VIP
            self.db.add_event(
                "promoted", user["user_id"],
                "RC -> VC (%d purchases, $%.2f spent)" % (purchases, spent),
            )
            return True
        return False

    def _apply_warning(self, user: dict, reason: str) -> Tuple[bool, bool]:
        """R9 + R10: issue a warning and apply the consequence.

        Returns ``(suspended, demoted)``.  An RC with more than 2 warnings is
        suspended; a VC with more than 2 warnings is demoted to RC.
        """
        models.normalise_user(user)
        user["warnings"] = int(user.get("warnings", 0)) + 1
        self.db.add_event(
            "warning", user["user_id"],
            "warning %d: %s" % (user["warnings"], reason),
        )

        suspended = False
        demoted = False
        if user["warnings"] > self.cfg.warning_limit:
            if user.get("role") == models.VIP:
                user["role"] = models.REGISTERED
                user["vip_ineligible"] = True
                demoted = True
                self.db.add_event(
                    "demoted", user["user_id"],
                    "VC -> RC after %d warnings" % user["warnings"],
                )
            elif user.get("role") == models.REGISTERED:
                self.suspend(user, "%d warnings" % user["warnings"])
                suspended = True

        self.db.put_user(user)
        return suspended, demoted

    # ======================================================================
    # Reviews (R7, R8, R9)
    # ======================================================================

    def review_weight(self, user: dict, book_id: str) -> Tuple[float, bool]:
        """R7: a reviewer who bought the book carries more weight."""
        purchased = any(
            entry.get("book_id") == book_id
            for entry in (user.get("purchase_history") or [])
        )
        weight = self.cfg.purchaser_weight if purchased else self.cfg.normal_weight
        return weight, purchased

    def submit_review(
        self, user: Optional[dict], book: dict, stars: int, comment: str
    ) -> ReviewResult:
        """R7 + R9.

        A visitor has no account to carry a warning, so by default reviewing
        requires a login (``visitors_may_review = false``).
        """
        if user is None:
            if not self.cfg.flag("visitors_may_review"):
                return ReviewResult(
                    False,
                    "Please log in to post a rating -- reviews are tied to an account "
                    "so that moderation warnings can be applied.",
                )
            return self._submit_visitor_review(book, stars, comment)

        models.normalise_user(user)
        allowed, why = self.can_log_in(user)
        if not allowed:
            return ReviewResult(False, why)

        try:
            stars = int(stars)
        except (TypeError, ValueError):
            return ReviewResult(False, "Rating must be a whole number of stars.")
        if not 1 <= stars <= 5:
            return ReviewResult(False, "Rating must be between 1 and 5 stars.")

        if self.db.user_has_reviewed(user["user_id"], book["book_id"]):
            return ReviewResult(False, "You have already reviewed this book.")

        screened = screen_comment(
            comment, self.cfg.taboo_words, self.cfg.max_taboo_before_block
        )
        weight, purchased = self.review_weight(user, book["book_id"])

        review = models.new_review(
            self.db.next_review_id(),
            book["book_id"],
            user["user_id"],
            user.get("role", models.REGISTERED),
            stars,
            screened.original,
            screened.masked,
            screened.hits,
            screened.blocked,
            purchased,
            weight,
        )
        self.db.add_review(review)

        if screened.blocked:
            suspended, demoted = self._apply_warning(
                user, "blocked comment on '%s' (%d taboo words)"
                % (book.get("title"), screened.hits),
            )
            message = (
                "Your comment was blocked for containing %d taboo words. "
                "You have received a warning (%d of %d allowed)."
                % (screened.hits, user["warnings"], self.cfg.warning_limit)
            )
            if suspended:
                message += " Your account has been suspended."
            elif demoted:
                message += " Your VIP status has been revoked; you are now a registered customer."
            return ReviewResult(
                False, message, blocked=True, taboo_hits=screened.hits,
                warning_issued=True, warnings_total=user["warnings"],
                suspended=suspended, demoted=demoted,
            )

        if screened.hits:
            message = (
                "Review posted. %d taboo word(s) were masked."
                % screened.hits
            )
        else:
            message = "Review posted."
        if purchased:
            message += " Counted with extra weight because you bought this book."
        return ReviewResult(True, message, taboo_hits=screened.hits)

    #: Stored as the author of a review left by somebody who was not signed in.
    #: It is deliberately not a real user id, so it can never collide with one
    #: and can never be looked up as an account.
    ANONYMOUS_AUTHOR = "visitor"

    def _submit_visitor_review(self, book: dict, stars: int,
                               comment: str) -> ReviewResult:
        """R7: "Any user can comment on any book."

        A visitor is a user, so their rating counts.  Two things necessarily
        differ from a signed-in review, and both follow from their having no
        account rather than from any policy choice:

        * The review can never carry the verified-purchaser weight, because
          there is no purchase history to check it against.
        * A blocked comment cannot earn its author a warning, because R7's
          escalation (suspend an RC, demote a VC) needs an account to act on.
          The comment is still screened, masked and blocked exactly as anyone
          else's would be; only the consequence has nowhere to land.

        Nor can "one review per customer per book" be enforced, since there is
        no identity to enforce it against.
        """
        try:
            stars = int(stars)
        except (TypeError, ValueError):
            return ReviewResult(False, "Rating must be a whole number of stars.")
        if not 1 <= stars <= 5:
            return ReviewResult(False, "Rating must be between 1 and 5 stars.")

        screened = screen_comment(
            comment, self.cfg.taboo_words, self.cfg.max_taboo_before_block
        )

        review = models.new_review(
            self.db.next_review_id(),
            book["book_id"],
            self.ANONYMOUS_AUTHOR,
            models.VISITOR,
            stars,
            screened.original,
            screened.masked,
            screened.hits,
            screened.blocked,
            False,                      # never a verified purchase
            self.cfg.normal_weight,
        )
        self.db.add_review(review)

        if screened.blocked:
            self.db.add_event(
                "blocked_visitor_comment", self.ANONYMOUS_AUTHOR,
                "blocked comment on '%s' (%d taboo words); no account to warn"
                % (book.get("title"), screened.hits),
            )
            return ReviewResult(
                False,
                "That comment was blocked for containing %d taboo words. "
                "Sign in if you would like your ratings kept with your account."
                % screened.hits,
                blocked=True, taboo_hits=screened.hits,
            )

        message = "Rating posted as a visitor."
        if screened.hits:
            message += " %d taboo word(s) were masked." % screened.hits
        message += (" Sign in next time and your ratings will count with your "
                    "account -- and double if you bought the book.")
        return ReviewResult(True, message, taboo_hits=screened.hits)

    def rating_summary(self, book_id: str) -> RatingSummary:
        """R7 + R8: weighted and plain averages for one book."""
        reviews = self.db.reviews_for_book(book_id, include_blocked=False)
        if not reviews:
            return RatingSummary(book_id=book_id)

        total_weight = sum(float(r.get("weight", 1.0)) for r in reviews) or 1.0
        weighted = sum(
            float(r.get("weight", 1.0)) * int(r.get("stars", 0)) for r in reviews
        ) / total_weight
        plain = sum(int(r.get("stars", 0)) for r in reviews) / len(reviews)

        return RatingSummary(
            book_id=book_id,
            count=len(reviews),
            weighted_average=round(weighted, 3),
            plain_average=round(plain, 3),
            purchaser_count=sum(1 for r in reviews if r.get("verified_purchase")),
        )

    def list_status(self, book_id: str) -> str:
        """R8: classify a book as ``no-order``, ``to-order`` or ``normal``.

        Needs more than ``min_ratings_for_list`` ratings (so 4+ by default,
        since the spec says "more than 3").  The count is unweighted; the
        average is weighted unless the config flag says otherwise.
        """
        summary = self.rating_summary(book_id)
        if summary.count <= self.cfg.min_ratings_for_list:
            return "normal"

        average = (
            summary.weighted_average
            if self.cfg.flag("order_lists_use_weighted_average")
            else summary.plain_average
        )
        if average < self.cfg.no_order_max_avg:
            return "no-order"
        if average > self.cfg.to_order_min_avg:
            return "to-order"
        return "normal"

    def order_lists(self) -> Dict[str, List[dict]]:
        """R8: the two lists the super-user acts on."""
        no_order: List[dict] = []
        to_order: List[dict] = []
        for book in self.db.list_books():
            status = self.list_status(book["book_id"])
            summary = self.rating_summary(book["book_id"])
            row = {
                "book": book,
                "count": summary.count,
                "average": summary.weighted_average,
                "plain_average": summary.plain_average,
            }
            if status == "no-order":
                no_order.append(row)
            elif status == "to-order":
                to_order.append(row)
        no_order.sort(key=lambda r: r["average"])
        to_order.sort(key=lambda r: -r["average"])
        return {"no_order": no_order, "to_order": to_order}

    # ======================================================================
    # Browsing history (feeds the recommender, R2)
    # ======================================================================

    def record_browse(self, user: Optional[dict], book: dict) -> None:
        book["browse_count"] = int(book.get("browse_count", 0)) + 1
        self.db.put_book(book)
        if user is None:
            return
        models.normalise_user(user)
        history = user.setdefault("browsing_history", [])
        history.append({"book_id": book["book_id"], "at": utc_now()})
        # Keep the file small; the recommender only needs recent behaviour.
        if len(history) > 200:
            del history[:-200]
        self.db.put_user(user)
