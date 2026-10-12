"""Specification item 4: face login, in a browser.

The desktop client opened a camera with OpenCV. A browser cannot be handed a
camera handle, so the flow inverts: the page captures frames with
``getUserMedia``, draws them to a canvas and posts them as images. The
recogniser is unchanged -- the same eigenfaces classifier, the same detector
chain, the same confidence threshold. Only the way a frame arrives is
different.

Why frames and not a template: the classifier trains on images, and sending
a client-computed descriptor would mean trusting the client to compute it
honestly. A face vector produced by the browser is a credential the browser
chose.

What this is not. Browser face login over a camera is a convenience, not a
hard security boundary -- a photograph of the account holder will usually
pass, which is true of the desktop version too. The password remains, and the
threshold is set to reject strangers rather than to be unspoofable.
"""

from __future__ import annotations

import os
import tempfile
from typing import List

from fastapi import (
    APIRouter, Depends, File, HTTPException, Request, UploadFile, status,
)
from sqlalchemy.orm import Session

from ...db import models as m
from ...services import accounts, auth as auth_service
from .. import present, runtime
from .. import schemas as S
from ..deps import client_label, db_session, require_user


router = APIRouter(prefix="/api/biometrics", tags=["biometrics"])

MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_FRAMES = 10


def _write_temp(payload: bytes, filename: str) -> str:
    suffix = os.path.splitext(filename or "")[1][:8] or ".png"
    handle, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(handle, "wb") as out:
        out.write(payload)
    return path


async def _collect(frames: List[UploadFile]) -> List[bytes]:
    if not frames:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Send at least one frame.")
    if len(frames) > MAX_FRAMES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Send at most %d frames." % MAX_FRAMES)

    out = []
    for frame in frames:
        payload = await frame.read(MAX_FRAME_BYTES + 1)
        if len(payload) > MAX_FRAME_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                "A frame was larger than 4 MB.")
        out.append(payload)
    return out


@router.get("/status", response_model=S.BiometricStatusOut)
def status_(user: m.User = Depends(require_user)):
    service = runtime.biometrics()
    config = runtime.config()
    samples = service.face.sample_count(str(user.id))
    return S.BiometricStatusOut(
        face_enrolled=samples > 0,
        face_samples=samples,
        face_model_trained=service.face.trained,
        detector=service.face.backend_description,
        required_samples=int(config.get("face_enrolment_samples", 5)),
        available=service.face.available(),
        message="" if service.face.available()
        else service.face.dependency_message(),
    )


@router.post("/face/enroll", response_model=S.EnrolResultOut)
async def enroll_face(frames: List[UploadFile] = File(...),
                      user: m.User = Depends(require_user),
                      session: Session = Depends(db_session)):
    """Add captured frames to this account and retrain.

    Enrolment is always for the caller's own account. There is no user_id
    parameter, so there is no request that enrols a face against somebody
    else's login.
    """
    service = runtime.biometrics()
    if not service.face.available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            service.face.dependency_message())

    payloads = await _collect(frames)
    accepted = 0
    rejected: List[str] = []

    for index, payload in enumerate(payloads):
        path = _write_temp(payload, frames[index].filename or "")
        try:
            result = service.face.add_sample_from_file(str(user.id), path)
            if getattr(result, "ok", False):
                accepted += 1
                session.add(m.BiometricSample(
                    user_id=user.id, kind="face",
                    path=getattr(result, "path", "") or ""))
            else:
                rejected.append(getattr(result, "message", "no face found"))
        finally:
            os.unlink(path)

    total = service.face.sample_count(str(user.id))
    trained = False
    if accepted:
        trained, _ = service.face.train()
        session.add(m.Event(kind="face_enrolled", user_id=user.id,
                            detail="%d frame(s), %d total" % (accepted, total)))
    session.flush()

    if not accepted:
        message = ("No face was found in any frame. Move closer, face the "
                   "camera straight on, and put the light in front of you.")
        if rejected:
            message += " (%s)" % rejected[0]
        return S.EnrolResultOut(ok=False, message=message, accepted=0,
                                total_samples=total, trained=trained)

    required = int(runtime.config().get("face_enrolment_samples", 5))
    if total < required:
        message = ("Captured %d of %d. Keep going -- more angles make "
                   "recognition more reliable." % (total, required))
    else:
        message = "Face enrolled. You can sign in by camera from now on."

    return S.EnrolResultOut(ok=True, message=message, accepted=accepted,
                            total_samples=total, trained=trained)


@router.post("/face/login", response_model=S.TokenOut)
async def login_by_face(request: Request, frames: List[UploadFile] = File(...),
                        session: Session = Depends(db_session)):
    """Sign in by looking at the camera.

    No username is supplied: the recogniser identifies the account. That is
    the point of the feature, and it also means a caller cannot probe whether
    a particular username has a face enrolled.

    **Several frames, best match wins.** An earlier version took a single
    photograph, and that made signing in a coin toss. Measured against a
    trained model, three consecutive frames of the same enrolled person
    scored 0.774, 0.662 and 0.484 against a 0.62 threshold -- two would have
    signed in and one would have been turned away, for no reason the person
    could see or act on. Blinking, a slight turn, or a moment of motion blur
    is enough to produce that third frame.

    Scoring a short burst and keeping the best costs a few hundred
    milliseconds and removes the failure. It does not weaken rejection: a
    stranger's best frame still has to clear the same threshold, and the
    threshold is unchanged.
    """
    service = runtime.biometrics()
    if not service.face.available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            service.face.dependency_message())

    if not service.face.trained:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No face has been enrolled in this shop yet. Sign in with a "
            "password first, then enrol your face from your account page.")

    payloads = await _collect(frames)

    best = None
    best_confidence = -1.0
    for index, payload in enumerate(payloads):
        path = _write_temp(payload, frames[index].filename or "")
        try:
            attempt = service.face.identify_image(path)
        finally:
            os.unlink(path)

        confidence = float(getattr(attempt, "confidence", 0.0) or 0.0)
        # Seeded below zero rather than at zero: an unrecognised frame scores
        # 0.0, and `> 0.0` would then keep the placeholder instead of the
        # real result. That exact comparison hid a bug in this project once
        # already, where the app blamed the camera for an untrained model.
        if confidence > best_confidence:
            best_confidence = confidence
            best = attempt

    if best is None or not best.identified:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            getattr(best, "message", None)
            or "That face was not recognised.")

    identification = best

    try:
        user_id = int(identification.user_id)
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "That face was not recognised.")

    user = session.get(m.User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "That face was not recognised.")

    allowed, why = accounts.can_sign_in(user)
    if not allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, why)

    session.add(m.Event(
        kind="face_login", user_id=user.id,
        detail="confidence %.3f" % float(identification.confidence or 0)))

    access, expires = auth_service.create_access_token(user)
    refresh = auth_service.issue_refresh_token(session, user,
                                               client_label(request))
    samples = service.face.sample_count(str(user.id))
    return S.TokenOut(
        access_token=access, refresh_token=refresh, expires_at=expires,
        session=present.session_out(user, runtime.config(), samples))


@router.delete("/face", status_code=status.HTTP_204_NO_CONTENT)
def forget_face(user: m.User = Depends(require_user),
                session: Session = Depends(db_session)):
    """Delete this account's face samples.

    Biometric data a person cannot delete is biometric data they did not
    really consent to, so this removes the files and retrains without them.
    """
    service = runtime.biometrics()
    removed = service.face.clear_samples(str(user.id))

    for sample in list(user.face_samples):
        session.delete(sample)

    if removed:
        service.face.train()
        session.add(m.Event(kind="face_deleted", user_id=user.id,
                            detail="%d sample(s)" % removed))
    session.flush()
