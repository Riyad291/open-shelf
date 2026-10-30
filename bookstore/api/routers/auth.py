"""Registration, sign-in, refresh, sign-out and password change."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ...db import models as m
from ...services import accounts, auth as auth_service
from .. import present, runtime
from .. import schemas as S
from ..deps import client_label, current_user, db_session, require_user


router = APIRouter(prefix="/api/auth", tags=["auth"])


def _tokens(session: Session, user: m.User, request: Request) -> S.TokenOut:
    access, expires = auth_service.create_access_token(user)
    refresh = auth_service.issue_refresh_token(session, user,
                                               client_label(request))
    samples = runtime.face_recogniser().sample_count(str(user.id))
    return S.TokenOut(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires,
        session=present.session_out(user, runtime.config(), samples),
    )


@router.post("/register", response_model=S.TokenOut,
             status_code=status.HTTP_201_CREATED)
def register(body: S.RegisterIn, request: Request,
             session: Session = Depends(db_session)):
    """Create a registered-customer account and sign it in.

    A face is enrolled afterwards, through /api/biometrics/face/enroll, so
    that a browser which has not been granted camera permission can still
    create an account and add a face later.
    """
    try:
        user = accounts.create_account(
            session, body.username, body.password,
            display_name=body.display_name)
    except accounts.AccountError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    return _tokens(session, user, request)


@router.post("/login", response_model=S.TokenOut)
def login(body: S.LoginIn, request: Request,
          session: Session = Depends(db_session)):
    user = accounts.find_by_username(session, body.username)

    # One message for both "no such user" and "wrong password": telling them
    # apart lets anyone enumerate which usernames exist.
    if user is None or not accounts.check_password(user, body.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "No account matches that username and password.")

    allowed, why = accounts.can_sign_in(user)
    if not allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, why)

    return _tokens(session, user, request)


@router.post("/refresh", response_model=S.TokenOut)
def refresh(body: S.RefreshIn, request: Request,
            session: Session = Depends(db_session)):
    try:
        user, access, new_refresh = auth_service.rotate_refresh_token(
            session, body.refresh_token, client_label(request))
    except auth_service.AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))

    _, expires = auth_service.create_access_token(user)
    samples = runtime.face_recogniser().sample_count(str(user.id))
    return S.TokenOut(
        access_token=access, refresh_token=new_refresh, expires_at=expires,
        session=present.session_out(user, runtime.config(), samples))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(body: S.RefreshIn, session: Session = Depends(db_session)):
    """Sign out of this device. Idempotent: logging out twice is not an error."""
    auth_service.revoke_refresh_token(session, body.refresh_token)


@router.post("/logout-everywhere", status_code=status.HTTP_204_NO_CONTENT)
def logout_everywhere(user: m.User = Depends(require_user),
                      session: Session = Depends(db_session)):
    auth_service.revoke_all_for(session, user, "signed out everywhere")


@router.get("/session", response_model=S.SessionOut)
def whoami(user=Depends(current_user)):
    """Who the caller is. A visitor is a valid answer, not an error."""
    samples = (runtime.face_recogniser().sample_count(str(user.id))
               if user else 0)
    return present.session_out(user, runtime.config(), samples)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(body: S.PasswordChangeIn,
                    user: m.User = Depends(require_user),
                    session: Session = Depends(db_session)):
    if not accounts.check_password(user, body.current_password):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "That is not your current password.")
    try:
        accounts.set_password(session, user, body.new_password)
    except accounts.AccountError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    # Changing a password signs out every other device, which is the point of
    # changing it after a suspected compromise.
    auth_service.revoke_all_for(session, user, "password changed")
