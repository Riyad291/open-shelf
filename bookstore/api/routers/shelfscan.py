"""Shelf Scan, in a browser.

Photograph a bookshelf. The scanner segments the photo into individual books,
matches each region against the cover index, reads a taste profile from what
it recognises, and recommends three more.

The segmentation and matching are unchanged from the desktop version. What is
new is that the annotated image -- the one with boxes drawn on it -- has to
reach a browser, so it is written to a per-scan file and served back by id
rather than handed over as an in-memory array.
"""

from __future__ import annotations

import os
import secrets
import tempfile
import time
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from types import SimpleNamespace

from ...db import models as m
from ...services import image_catalogue
from ...services import recommend as recommend_service
from ...services import reviews as review_service
from .. import present, runtime
from .. import schemas as S
from ..deps import current_user, db_session


router = APIRouter(tags=["shelf scan"])

MAX_PHOTO_BYTES = 12 * 1024 * 1024

#: Annotated scans are transient. They are kept long enough for the page that
#: requested one to load it, then swept. Without the sweep this directory
#: grows without limit, which is the sort of thing nobody notices until a
#: disk fills.
SCAN_TTL_SECONDS = 15 * 60


def _scan_dir() -> str:
    path = os.path.join(runtime.files().data_dir, "scans")
    os.makedirs(path, exist_ok=True)
    return path


def _sweep() -> None:
    cutoff = time.time() - SCAN_TTL_SECONDS
    try:
        for name in os.listdir(_scan_dir()):
            full = os.path.join(_scan_dir(), name)
            try:
                if os.path.isfile(full) and os.path.getmtime(full) < cutoff:
                    os.unlink(full)
            except OSError:
                pass
    except OSError:
        pass


class _RecommenderShim:
    """Shelf Scan asks its recommender for similar books. That now lives in
    the SQL service, so this bridges the two without touching shelfscan.py."""

    def __init__(self, session, config):
        self._session = session
        self._config = config

    def similar_to(self, book_ids, count=3, exclude=None):
        ids = []
        for raw in book_ids:
            try:
                ids.append(int(str(raw).lstrip("b") or 0))
            except (TypeError, ValueError):
                continue
        picks = recommend_service.similar_to(self._session, ids,
                                             self._config, count)
        return [SimpleNamespace(book=image_catalogue.book_as_dict(p.book),
                                reason=p.reason, score=p.score)
                for p in picks]


class _RulesShim:
    """Shelf Scan flags a recognised book that is on the to-order list."""

    def __init__(self, session, config):
        self._session = session
        self._config = config

    def list_status(self, book_id):
        try:
            key = int(str(book_id).lstrip("b") or 0)
        except (TypeError, ValueError):
            return "normal"
        summary = review_service.rating_summary(self._session, key)
        return review_service.list_status(summary, self._config)


def _scanner(session):
    """A scanner wired to the SQL catalogue through adapters.

    imagesearch and shelfscan are untouched by the storage migration: they
    read books through the same five names Storage gave them, now served by
    ImageCatalogue.
    """
    from ...imagesearch import ImageIndex
    from ...shelfscan import ShelfScanner

    config = runtime.config()
    catalogue = image_catalogue.ImageCatalogue(session, runtime.files())
    index = ImageIndex(catalogue)
    return ShelfScanner(index, _RecommenderShim(session, config),
                        _RulesShim(session, config), config=config)


@router.post("/api/shelf-scan", response_model=S.ShelfScanOut)
async def scan_shelf(photo: UploadFile = File(...),
                     user=Depends(current_user),
                     session: Session = Depends(db_session)):
    payload = await photo.read(MAX_PHOTO_BYTES + 1)
    if len(payload) > MAX_PHOTO_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            "That photo is larger than 12 MB.")

    from ...imagesearch import available as imaging_available
    if not imaging_available():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Shelf Scan needs numpy and Pillow: pip install -r requirements.txt")

    suffix = os.path.splitext(photo.filename or "")[1][:8] or ".png"
    handle, source = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(handle, "wb") as out:
        out.write(payload)

    _sweep()
    token = secrets.token_urlsafe(16)
    annotated_path = os.path.join(_scan_dir(), "%s.png" % token)

    try:
        scanner = _scanner(session)
        result = scanner.scan(source, None)
        annotated = scanner.annotate(source, result, annotated_path)
    finally:
        os.unlink(source)

    if not getattr(result, "ok", False):
        return S.ShelfScanOut(
            ok=False,
            message=getattr(result, "message",
                            "Nothing book-shaped was found in that photo."))

    config = runtime.config()
    matches: List[S.ShelfMatchOut] = []
    recognised_ids: List[int] = []

    for match in getattr(result, "matches", []):
        book = None
        raw_id = (match.book or {}).get("book_id") if getattr(match, "book", None) else None
        if raw_id:
            try:
                book_id = int(str(raw_id).lstrip("b") or 0)
            except ValueError:
                book_id = 0
            if book_id:
                book = session.get(m.Book, book_id)
                if book is not None:
                    recognised_ids.append(book.id)

        matches.append(S.ShelfMatchOut(
            book=present.book_out(book, user, config) if book else None,
            confidence=round(float(getattr(match, "similarity", 0.0)), 3),
            region=[int(v) for v in (getattr(match, "region", None) or [])],
            recognised=book is not None,
        ))

    picks = recommend_service.similar_to(session, recognised_ids, config,
                                         config.recommendation_count)
    ratings = present.rating_map(session, [p.book.id for p in picks])

    seeds = session.execute(
        select(m.Book).where(m.Book.id.in_(recognised_ids))
    ).scalars().all() if recognised_ids else []
    taste = sorted({book.category for book in seeds})

    return S.ShelfScanOut(
        ok=True,
        regions_found=int(getattr(result, "regions_found", len(matches))),
        recognised_count=len(recognised_ids),
        segmentation=str(getattr(result, "backend", "")),
        matches=matches,
        recommendations=[present.book_out(p.book, user, config,
                                          ratings.get(p.book.id))
                         for p in picks],
        taste_profile=taste,
        annotated_url="/api/shelf-scan/%s" % token if annotated else None,
        message="Recognised %d of %d book-shaped regions."
                % (len(recognised_ids), len(matches)),
    )


@router.get("/api/shelf-scan/{token}")
def annotated(token: str):
    """Serve one annotated scan.

    The token is validated as hex-ish before it touches the filesystem: it
    arrives from the URL, and a path built from unvalidated input is how
    directory traversal gets in.
    """
    if not token or not all(c.isalnum() or c in "-_" for c in token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such scan.")

    path = os.path.join(_scan_dir(), "%s.png" % token)
    if not os.path.exists(path):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "That scan has expired. Scans are kept for 15 minutes.")
    return FileResponse(path, media_type="image/png")
