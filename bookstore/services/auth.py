"""Authentication: short-lived JWT access tokens, rotating refresh tokens.

What this replaces
------------------
The first version of the API kept ``{token: user_id}`` in a Python dict. It
worked, and it was wrong in three ways that matter the moment anything real
runs on it: every session died on restart, logging out of one device logged
you out of none of them, and a second server process would not recognise the
first one's tokens.

The design
----------
**Access token** -- a signed JWT, 15 minutes, never stored server-side. It
carries the user id, role and an expiry. Checking it is a signature
verification, so the hot path (every authenticated request) costs no database
query at all.

**Refresh token** -- an opaque 256-bit random string, 30 days, stored in the
``refresh_tokens`` table as a SHA-256 hash. Opaque rather than a JWT because
it must be revocable, and a stateless token cannot be revoked. Hashed rather
than plain because a database leak should not hand the attacker a working key
ring.

**Rotation with reuse detection.** Refreshing issues a new refresh token and
revokes the one presented. If a token that was already revoked comes back,
that means two parties hold it -- the legitimate user and a thief -- and there
is no way to tell which is which. Every refresh token for that account is
revoked and they all sign in again. This is the standard answer and it is the
part an interviewer is most likely to ask about.

Why the short access-token lifetime matters: a stolen access token cannot be
revoked, so the only defence is that it expires quickly. Fifteen minutes is
the usual trade between that exposure and how often clients must refresh.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import secrets
import warnings
from typing import Optional, Tuple

from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import models as m


ALGORITHM = "HS256"
ACCESS_TOKEN_MINUTES = 15
REFRESH_TOKEN_DAYS = 30

#: Generated per process when unset, which is correct for a developer running
#: `python -m bookstore.api` and wrong for anything with more than one worker
#: -- tokens signed by one would be rejected by another. Deployment must set
#: JWT_SECRET, and the warning says so rather than failing mysteriously later.
_DEV_SECRET: Optional[str] = None


class AuthError(Exception):
    """Credentials refused. The message is safe to show a caller."""


def secret_key() -> str:
    global _DEV_SECRET
    configured = os.environ.get("JWT_SECRET")
    if configured:
        return configured

    if _DEV_SECRET is None:
        _DEV_SECRET = secrets.token_urlsafe(48)
        warnings.warn(
            "JWT_SECRET is not set, so a random development key was generated. "
            "Tokens will stop working when this process restarts, and a "
            "second worker will reject this one's tokens. Set JWT_SECRET "
            "before running more than one process.",
            RuntimeWarning, stacklevel=2,
        )
    return _DEV_SECRET


# ------------------------------------------------------------ access tokens


def create_access_token(user: m.User) -> Tuple[str, dt.datetime]:
    expires = m.utcnow() + dt.timedelta(minutes=ACCESS_TOKEN_MINUTES)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "type": "access",
        "iat": int(m.utcnow().timestamp()),
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(payload, secret_key(), algorithm=ALGORITHM), expires


def decode_access_token(token: str) -> dict:
    """Verify and decode, or raise ``AuthError``.

    The role is deliberately *not* trusted from the token for authorisation
    decisions -- see ``resolve_user``. It is carried for display only. A token
    issued before a demotion would otherwise keep its old privileges until it
    expired.
    """
    try:
        payload = jwt.decode(token, secret_key(), algorithms=[ALGORITHM])
    except JWTError as exc:
        raise AuthError("That sign-in has expired or is not valid.") from exc

    if payload.get("type") != "access":
        raise AuthError("Wrong kind of token for this request.")
    return payload


def resolve_user(session: Session, token: str) -> m.User:
    """The account a token belongs to, as it is *now*.

    One query, and worth it: privilege comes from the row, not the token, so
    a suspension or a demotion takes effect on the next request rather than
    whenever the token happens to expire.
    """
    payload = decode_access_token(token)
    try:
        user_id = int(payload.get("sub", ""))
    except (TypeError, ValueError):
        raise AuthError("Malformed token.")

    user = session.get(m.User, user_id)
    if user is None:
        raise AuthError("That account no longer exists.")
    return user


# ----------------------------------------------------------- refresh tokens


def _hash(token: str) -> str:
    """SHA-256, not bcrypt.

    A refresh token is 256 bits of entropy from ``secrets``, not a human
    password, so there is nothing to brute-force and no reason to pay bcrypt's
    cost on every refresh. Passwords are a different matter and use PBKDF2.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_refresh_token(session: Session, user: m.User,
                        user_agent: str = "") -> str:
    raw = secrets.token_urlsafe(32)
    session.add(m.RefreshToken(
        user_id=user.id,
        token_hash=_hash(raw),
        expires_at=m.utcnow() + dt.timedelta(days=REFRESH_TOKEN_DAYS),
        user_agent=(user_agent or "")[:255],
    ))
    session.flush()
    return raw


def revoke_all_for(session: Session, user: m.User, reason: str) -> int:
    rows = session.execute(
        select(m.RefreshToken).where(
            m.RefreshToken.user_id == user.id,
            m.RefreshToken.revoked_at.is_(None))
    ).scalars().all()

    now = m.utcnow()
    for row in rows:
        row.revoked_at = now

    if rows:
        session.add(m.Event(kind="tokens_revoked", user_id=user.id,
                            detail="%d token(s): %s" % (len(rows), reason)))
    session.flush()
    return len(rows)


def rotate_refresh_token(session: Session, raw_token: str,
                         user_agent: str = "") -> Tuple[m.User, str, str]:
    """Exchange a refresh token for a new access and refresh token pair.

    Returns ``(user, access_token, new_refresh_token)``.
    """
    row = session.execute(
        select(m.RefreshToken).where(m.RefreshToken.token_hash
                                     == _hash(raw_token))
    ).scalar_one_or_none()

    if row is None:
        raise AuthError("Please sign in again.")

    if row.revoked_at is not None:
        # Reuse of a revoked token: two parties hold it and there is no way to
        # tell the thief from the owner, so neither keeps access.
        user = session.get(m.User, row.user_id)
        if user is not None:
            revoke_all_for(session, user, "refresh token reused")

            # Committed here, deliberately, before raising.
            #
            # A service committing its own work is usually a smell -- the
            # request owns the transaction. This is the exception, and the
            # reason is the thing being defended against. The caller turns
            # this AuthError into a 401, and the session dependency rolls the
            # transaction back when a request raises. That rollback would
            # undo the revocation: the API would answer "this session was
            # ended for security" while every token in the family kept
            # working, which is worse than not detecting reuse at all,
            # because it looks handled.
            session.commit()

        raise AuthError(
            "This session was ended for security. Please sign in again.")

    if m.as_utc(row.expires_at) <= m.utcnow():
        raise AuthError("This session has expired. Please sign in again.")

    user = session.get(m.User, row.user_id)
    if user is None:
        raise AuthError("That account no longer exists.")

    row.revoked_at = m.utcnow()
    session.flush()

    access, _ = create_access_token(user)
    new_refresh = issue_refresh_token(session, user, user_agent)
    return user, access, new_refresh


def revoke_refresh_token(session: Session, raw_token: str) -> bool:
    """Sign out of one device. Already-revoked and unknown tokens are not an
    error: logging out twice should not fail."""
    row = session.execute(
        select(m.RefreshToken).where(m.RefreshToken.token_hash
                                     == _hash(raw_token))
    ).scalar_one_or_none()

    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = m.utcnow()
    session.flush()
    return True


def purge_expired(session: Session) -> int:
    """Delete refresh tokens that expired over a week ago.

    Revoked tokens are kept for a while on purpose: reuse detection needs the
    row to exist in order to notice that a stolen token came back.
    """
    cutoff = m.utcnow() - dt.timedelta(days=7)
    rows = [row for row in session.execute(select(m.RefreshToken)).scalars()
            if m.as_utc(row.expires_at) < cutoff]
    for row in rows:
        session.delete(row)
    session.flush()
    return len(rows)
