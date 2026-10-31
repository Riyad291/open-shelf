"""Shared FastAPI dependencies: the session, the caller, and the role gates.

Authorisation lives here rather than in each handler, so a new endpoint
cannot forget it -- it either declares a dependency that checks privilege or
it is public on purpose. Spreading `if user.role != "SU"` through twenty
handlers is how one of them ends up missing it.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..db import models as m
from ..db.base import get_session
from ..services import auth


# One definition of the roles, in db/models, used by the API and the
# assistant alike.
ROLE_NAMES = m.ROLE_NAMES
role_at_least = m.role_at_least


def db_session() -> Session:
    yield from get_session()


def bearer_token(authorization: Optional[str] = Header(None)) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def current_user(
    token: Optional[str] = Depends(bearer_token),
    session: Session = Depends(db_session),
) -> Optional[m.User]:
    """Who is calling, or None. A visitor is a valid caller, not an error."""
    if not token:
        return None
    try:
        return auth.resolve_user(session, token)
    except auth.AuthError:
        # An expired or bad token is treated as "not signed in" rather than
        # a hard error, so browsing the catalogue keeps working while the
        # client refreshes in the background.
        return None


def require_user(user: Optional[m.User] = Depends(current_user)) -> m.User:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Sign in to do that.",
                            headers={"WWW-Authenticate": "Bearer"})
    if user.suspended:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This account is suspended: %s"
            % (user.suspend_reason or "contact an administrator."))
    return user


def require_role(minimum: str):
    """Build a dependency that admits ``minimum`` and above."""

    def guard(user: m.User = Depends(require_user)) -> m.User:
        if not role_at_least(user.role, minimum):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "This needs %s access; you are signed in as %s."
                % (ROLE_NAMES.get(minimum, minimum),
                   ROLE_NAMES.get(user.role, user.role)))
        return user

    return guard


require_super_user = require_role(m.SUPER_USER)
require_vip = require_role(m.VIP)


def client_label(request: Request) -> str:
    """A short description of the client, stored against a refresh token so a
    person can see which devices are signed in."""
    return (request.headers.get("user-agent") or "")[:255]
