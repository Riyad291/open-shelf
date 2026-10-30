"""Accounts: registration, sign-in, suspension, promotion and the warning ladder.

The privilege rules from the specification live here, reinterpreted for a
free platform: standing is earned by writing reviews that pass moderation,
and lost by writing ones that do not.

Password hashing lives here too, because accounts are the only thing that
uses it.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Dict, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import models as m


class AccountError(Exception):
    """Something the caller did wrong, phrased for a person to read."""


# --------------------------------------------------------------- passwords
#
# PBKDF2 from the standard library: salted, slow on purpose, and no extra
# dependency. 120,000 rounds of SHA-256 takes a noticeable fraction of a
# second, which is the point -- it makes guessing expensive.

_PBKDF2_ROUNDS = 120_000


def hash_password(password: str,
                  salt: Optional[bytes] = None) -> Dict[str, str]:
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return {"salt": salt.hex(), "password_hash": digest.hex()}


def verify_password(password: str, salt_hex: str, expected_hex: str) -> bool:
    """Check a password against a stored salt and hash.

    Returns False for a malformed stored credential rather than raising, and
    compares in constant time so the response time does not leak how much of
    the hash matched.
    """
    if not salt_hex or not expected_hex:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return hmac.compare_digest(digest, expected)


# ----------------------------------------------------------- registration


def create_account(session: Session, username: str, password: str, *,
                   role: str = m.REGISTERED,
                   display_name: str = "") -> m.User:
    username = (username or "").strip()
    if not username:
        raise AccountError("Choose a username.")
    if len(password or "") < 4:
        raise AccountError("The password must be at least 4 characters.")

    secret = hash_password(password)
    user = m.User(
        username=username,
        display_name=display_name or username.title(),
        password_hash=secret["password_hash"],
        salt=secret["salt"],
        role=role,
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
    return verify_password(password, user.salt, user.password_hash)


def set_password(session: Session, user: m.User, password: str) -> None:
    if len(password or "") < 4:
        raise AccountError("The password must be at least 4 characters.")
    secret = hash_password(password)
    user.password_hash = secret["password_hash"]
    user.salt = secret["salt"]
    session.add(m.Event(kind="password_changed", user_id=user.id, detail=""))
    session.flush()


def can_sign_in(user: m.User) -> Tuple[bool, str]:
    """Specification item 6: a suspended account cannot sign in."""
    if user.suspended:
        return False, ("This account is suspended: %s. An administrator must "
                       "reopen it." % (user.suspend_reason or "contact us"))
    return True, ""


# ---------------------------------------------------------- suspension


def suspend(session: Session, user: m.User, reason: str) -> None:
    if user.suspended:
        return
    user.suspended = True
    user.suspend_reason = reason
    session.add(m.Event(kind="account_suspended", user_id=user.id,
                        detail=reason))
    session.flush()


def reopen(session: Session, user: m.User, *,
           clear_warnings: bool = False) -> None:
    """Specification item 6: only an administrator reopens an account.

    Warnings are kept unless the administrator clears them, so a reopened
    account that misbehaves once more is suspended again straight away.
    """
    user.suspended = False
    user.suspend_reason = ""
    if clear_warnings:
        user.warnings = 0
    session.add(m.Event(
        kind="account_reopened", user_id=user.id,
        detail="warnings cleared" if clear_warnings else "warnings kept"))
    session.flush()


# ------------------------------------------------------- the warning ladder


def apply_warning(session: Session, user: m.User, reason: str,
                  config) -> Tuple[bool, bool]:
    """Specification items 7 and 8.

    More than ``warning_limit`` warnings suspends a member and demotes a
    trusted contributor back to member. Returns ``(suspended, demoted)``.

    A demoted contributor is marked ``promotion_blocked``. Without it, the
    next accepted review would promote them straight back, and the demotion
    would last one review.
    """
    user.warnings += 1
    session.add(m.Event(kind="warning", user_id=user.id,
                        detail="warning %d: %s" % (user.warnings, reason)))

    suspended = demoted = False
    if user.warnings > config.warning_limit:
        if user.role == m.VIP:
            user.role = m.REGISTERED
            user.promotion_blocked = True
            demoted = True
            session.add(m.Event(
                kind="demoted", user_id=user.id,
                detail="VC -> RC after %d warnings" % user.warnings))
        elif user.role == m.REGISTERED:
            suspend(session, user, "%d warnings" % user.warnings)
            suspended = True

    session.flush()
    return suspended, demoted


# --------------------------------------------------------------- promotion


def maybe_promote(session: Session, user: m.User, config) -> bool:
    """Promote a member to trusted contributor once they have earned it.

    Earned means ``trusted_review_threshold`` reviews that passed
    moderation. Blocked reviews do not count, administrators are never
    "promoted", and a demoted contributor stays demoted.
    """
    if user.role != m.REGISTERED or user.promotion_blocked:
        return False

    accepted = session.execute(
        select(func.count(m.Review.id)).where(
            m.Review.user_id == user.id, m.Review.blocked.is_(False))
    ).scalar_one()
    if accepted < config.trusted_review_threshold:
        return False

    user.role = m.VIP
    session.add(m.Event(
        kind="promoted", user_id=user.id,
        detail="RC -> VC after %d accepted reviews" % accepted))
    session.flush()
    return True
