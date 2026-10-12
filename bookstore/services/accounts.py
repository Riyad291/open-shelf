"""Accounts: registration, sign-in, suspension, deposits and the warning ladder.

The privilege rules from the specification live here. They are the same rules
the file-backed version enforced; only the storage underneath changed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models as legacy          # PBKDF2 helpers, unchanged
from ..db import models as m
from . import pricing


class AccountError(Exception):
    """Something the caller did wrong, phrased for a person to read."""


# ----------------------------------------------------------- registration


def create_account(session: Session, username: str, password: str, *,
                   role: str = m.REGISTERED, balance: Decimal | float = 0,
                   display_name: str = "") -> m.User:
    username = (username or "").strip()
    if not username:
        raise AccountError("Choose a username.")
    if len(password or "") < 4:
        raise AccountError("The password must be at least 4 characters.")

    secret = legacy.hash_password(password)
    user = m.User(
        username=username,
        display_name=display_name or username.title(),
        password_hash=secret["password_hash"],
        salt=secret["salt"],
        role=role,
        balance=pricing.money(balance),
    )
    session.add(user)

    try:
        # Let the unique index decide, rather than SELECT-then-INSERT. Two
        # simultaneous registrations of the same name would both pass a
        # prior check; only one can win the constraint.
        session.flush()
    except IntegrityError:
        session.rollback()
        raise AccountError("That username is already taken.")

    session.add(m.Event(kind="account_created", user_id=user.id,
                        detail="role=%s" % role))
    session.flush()
    return user


def find_by_username(session: Session, username: str) -> Optional[m.User]:
    return session.execute(
        select(m.User).where(func.lower(m.User.username)
                             == (username or "").strip().lower())
    ).scalar_one_or_none()


def check_password(user: m.User, password: str) -> bool:
    return legacy.verify_password(password, user.salt, user.password_hash)


def set_password(session: Session, user: m.User, password: str) -> None:
    if len(password or "") < 4:
        raise AccountError("The password must be at least 4 characters.")
    secret = legacy.hash_password(password)
    user.password_hash = secret["password_hash"]
    user.salt = secret["salt"]
    session.add(m.Event(kind="password_changed", user_id=user.id, detail=""))
    session.flush()


def can_sign_in(user: m.User) -> Tuple[bool, str]:
    """Specification item 6: a suspended account cannot sign in."""
    if user.suspended:
        return False, ("This account is suspended: %s. A super-user must "
                       "reopen it." % (user.suspend_reason or "contact the shop"))
    return True, ""


# -------------------------------------------------------------- the money


def deposit(session: Session, user: m.User, amount: Decimal | float,
            note: str = "deposit") -> Decimal:
    amount = pricing.money(amount)
    if amount <= 0:
        raise AccountError("A deposit must be more than zero.")

    user.balance = pricing.money(user.balance + amount)
    session.add(m.Transaction(kind="deposit", user_id=user.id,
                              amount=amount, note=note))
    session.add(m.Event(kind="deposit", user_id=user.id,
                        detail="$%s (%s)" % (amount, note)))

    # A deposit that clears a negative balance does not by itself reopen the
    # account: the specification gives that decision to the super-user.
    session.flush()
    return user.balance


def suspend(session: Session, user: m.User, reason: str) -> None:
    if user.suspended:
        return
    user.suspended = True
    user.suspend_reason = reason
    session.add(m.Event(kind="account_suspended", user_id=user.id,
                        detail=reason))
    session.flush()


def reopen(session: Session, user: m.User, *,
           deposit_amount: Decimal | float = 0) -> Decimal:
    """Specification item 6: only a super-user reopens a suspended account."""
    if deposit_amount:
        deposit(session, user, deposit_amount, note="reopening deposit")

    if user.balance < 0:
        raise AccountError(
            "The balance is still $%s. Deposit enough to clear it before "
            "reopening." % pricing.money(user.balance))

    user.suspended = False
    user.suspend_reason = ""
    session.add(m.Event(kind="account_reopened", user_id=user.id,
                        detail="balance $%s" % pricing.money(user.balance)))
    session.flush()
    return user.balance


# ------------------------------------------------------- the warning ladder


def apply_warning(session: Session, user: m.User, reason: str,
                  config) -> Tuple[bool, bool]:
    """Specification items 7 and 8.

    More than ``warning_limit`` warnings suspends a registered customer and
    demotes a VIP back to registered. Returns ``(suspended, demoted)``.

    A demoted VIP is marked ``vip_ineligible``, which is this project's
    resolution of a contradiction in the specification: item 7 says a demoted
    VIP becomes a registered customer, and item 8 says a registered customer
    who has bought enough becomes a VIP -- together they would promote the
    person straight back. Recorded under `spec_interpretation` in settings.
    """
    user.warnings += 1
    session.add(m.Event(kind="warning", user_id=user.id,
                        detail="warning %d: %s" % (user.warnings, reason)))

    suspended = demoted = False
    if user.warnings > config.warning_limit:
        if user.role == m.VIP:
            user.role = m.REGISTERED
            user.vip_ineligible = True
            demoted = True
            session.add(m.Event(
                kind="demoted", user_id=user.id,
                detail="VC -> RC after %d warnings" % user.warnings))
        elif user.role == m.REGISTERED:
            suspend(session, user, "%d warnings" % user.warnings)
            suspended = True

    session.flush()
    return suspended, demoted
