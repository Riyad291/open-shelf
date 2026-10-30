"""The catalogue: browsing, search, covers, recommendations and reviews.

Specification items 2, 5 and 7 are served from here.
"""

from __future__ import annotations

import os
import tempfile
from typing import List, Optional

from fastapi import (
    APIRouter, Depends, File, HTTPException, Query, UploadFile, status,
)
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import models as m
from ...services import catalog as catalog_service
from ...services import image_catalogue
from ...services import recommend as recommend_service
from ...services import reviews as review_service
from .. import present, runtime
from .. import schemas as S
from ..deps import current_user, db_session, require_user


router = APIRouter(tags=["catalogue"])

#: Uploads are read into memory before being handed to the matcher, so the
#: ceiling is deliberate. Without it, a single request can exhaust the server.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


def _resolve(path: str) -> Optional[str]:
    """Turn a stored relative path into a real one, or None.

    Resolved against the project root and checked to be inside the data
    directory: a stored path is data, and data that becomes a filesystem read
    is how path traversal happens.
    """
    if not path:
        return None

    data_dir = os.path.abspath(runtime.files().data_dir)
    candidate = path if os.path.isabs(path) else os.path.join(
        os.path.dirname(data_dir), path)
    candidate = os.path.abspath(candidate)

    if os.path.commonpath([candidate, data_dir]) != data_dir:
        return None
    return candidate if os.path.exists(candidate) else None


@router.get("/api/books", response_model=S.PageOut)
def list_books(
    category: Optional[str] = None,
    author: Optional[str] = None,
    decade: Optional[str] = Query(None, description="e.g. 1850s"),
    sort: str = Query("title", pattern="^(title|author|year|newest|popular)$"),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(current_user),
    session: Session = Depends(db_session),
):
    books, total = catalog_service.list_books(
        session, category=category, author=author, decade=decade,
        limit=limit, offset=offset, sort=sort)
    return S.PageOut(
        items=present.books_out(session, books, user, runtime.config()),
        total=total, limit=limit, offset=offset)


@router.get("/api/books/facets", response_model=S.FacetsOut)
def facets(session: Session = Depends(db_session)):
    return catalog_service.facets(session)


@router.get("/api/books/{book_id}", response_model=S.BookDetailOut)
def one_book(book_id: int, user=Depends(current_user),
             session: Session = Depends(db_session)):
    book = session.get(m.Book, book_id)
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such book.")

    catalog_service.record_browse(session, user, book)
    summary = review_service.rating_summary(session, book.id)
    base = present.book_out(book, user, runtime.config(), summary)

    return S.BookDetailOut(
        **base.model_dump(),
        plain_rating=summary["plain_average"],
        figures=[S.FigureOut(index=i,
                             url="/api/books/%d/figure/%d" % (book.id, i))
                 for i, _ in enumerate(book.figures)],
        reviews=[present.review_out(r)
                 for r in review_service.for_book(session, book.id)],
    )


@router.get("/api/books/{book_id}/cover")
def cover(book_id: int, session: Session = Depends(db_session)):
    book = session.get(m.Book, book_id)
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such book.")
    path = _resolve(book.cover_path)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No cover for that book.")
    return FileResponse(path)


@router.get("/api/books/{book_id}/figure/{index}")
def figure(book_id: int, index: int, session: Session = Depends(db_session)):
    book = session.get(m.Book, book_id)
    if book is None or index < 0 or index >= len(book.figures):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such figure.")
    path = _resolve(book.figures[index].path)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such figure.")
    return FileResponse(path)


# ---------------------------------------------------------------- search


@router.get("/api/search", response_model=List[S.SearchHitOut])
def search(q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=100),
           user=Depends(current_user),
           session: Session = Depends(db_session)):
    """Keyword search over titles, authors, descriptions and book texts."""
    hits = catalog_service.search(session, runtime.files().data_dir, q, limit)
    ratings = present.rating_map(session, [h.book.id for h in hits])
    return [
        S.SearchHitOut(
            book=present.book_out(h.book, user, runtime.config(),
                                  ratings.get(h.book.id)),
            score=h.score, matched=h.matched, where=h.where)
        for h in hits
    ]


@router.post("/api/search/image", response_model=List[S.ImageHitOut])
async def search_by_image(file: UploadFile = File(...),
                          user=Depends(current_user),
                          session: Session = Depends(db_session)):
    """Specification item 5: hand it a picture and it finds the book."""
    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            "That image is larger than 8 MB.")

    from ...imagesearch import ImageIndex, available as imaging_available
    if not imaging_available():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Picture search needs numpy and Pillow: pip install -r requirements.txt")

    # Built against the SQL catalogue through the adapter, so imagesearch.py
    # itself did not have to change when the records became relational.
    index = ImageIndex(image_catalogue.ImageCatalogue(session, runtime.files()))

    suffix = os.path.splitext(file.filename or "")[1][:8] or ".png"
    handle, temp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(payload)
        matches = index.search(temp_path,
                              limit=runtime.config().image_search_results)
    finally:
        os.unlink(temp_path)

    out: List[S.ImageHitOut] = []
    book_ids = []
    # ImageHit carries the matched book as a dict and its score as
    # `similarity`. This loop used to read match.book_id and match.score,
    # which ImageHit never had, so every picture search with a match was a
    # 500. No test uploaded an image that matched anything; the evaluation
    # in tools/evaluate_ai.py does, and caught it.
    for match in matches:
        try:
            book_ids.append(int(str(match.book.get("book_id", "")) or 0))
        except (AttributeError, ValueError):
            book_ids.append(0)

    books = {b.id: b for b in session.execute(
        select(m.Book).where(m.Book.id.in_(book_ids))
    ).scalars()}
    ratings = present.rating_map(session, list(books))

    for match, book_id in zip(matches, book_ids):
        book = books.get(book_id)
        if book is None:
            continue
        out.append(S.ImageHitOut(
            book=present.book_out(book, user, runtime.config(),
                                  ratings.get(book.id)),
            score=round(float(match.similarity), 3),
            matched_on=getattr(match, "kind", "cover")))
    return out


# ------------------------------------------------------- recommendations


@router.get("/api/recommendations", response_model=List[dict])
def recommendations(user=Depends(current_user),
                    session: Session = Depends(db_session)):
    """Specification item 2: three picks, personalised by privilege."""
    config = runtime.config()
    picks = recommend_service.top_for(session, user, config,
                                      config.recommendation_count)
    ratings = present.rating_map(session, [p.book.id for p in picks])
    return [{
        "book": present.book_out(p.book, user, config,
                                 ratings.get(p.book.id)).model_dump(),
        "reason": p.reason,
    } for p in picks]


# --------------------------------------------------------------- reviews


@router.get("/api/books/{book_id}/reviews", response_model=List[S.ReviewOut])
def book_reviews(book_id: int, session: Session = Depends(db_session)):
    return [present.review_out(r)
            for r in review_service.for_book(session, book_id)]


@router.post("/api/books/{book_id}/reviews", response_model=S.ReviewResultOut)
def post_review(book_id: int, body: S.ReviewIn, user=Depends(current_user),
                session: Session = Depends(db_session)):
    """Specification item 7: any user can comment on any book."""
    book = session.get(m.Book, book_id)
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such book.")

    try:
        outcome = review_service.submit(session, user, book, body.stars,
                                        body.comment, runtime.config())
    except review_service.ReviewError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    return S.ReviewResultOut(
        ok=outcome.ok, message=outcome.message,
        review=present.review_out(outcome.review) if outcome.review
        and not outcome.blocked else None,
        blocked=outcome.blocked, taboo_hits=outcome.taboo_hits,
        warning_issued=outcome.warning_issued,
        warnings_total=outcome.warnings_total,
        suspended=outcome.suspended, demoted=outcome.demoted)


@router.patch("/api/reviews/{review_id}", response_model=S.ReviewOut)
def edit_review(review_id: int, body: S.ReviewUpdateIn,
                user: m.User = Depends(require_user),
                session: Session = Depends(db_session)):
    """Edit your own review. Re-screened, because an edit is a new comment."""
    review = session.get(m.Review, review_id)
    if review is None or review.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such review.")

    changes = body.model_dump(exclude_unset=True)
    outcome = None

    # The comment is screened before anything is modified. An edit carrying
    # both a new rating and a blocked comment must not leave the rating
    # applied: the whole edit is refused, as one act.
    if "comment" in changes:
        config = runtime.config()
        book = session.get(m.Book, review.book_id)

        # An edit is a new comment, so it costs what a new comment costs.
        # This endpoint used to screen the text itself and merely refuse a
        # blocked one, which made the ladder in item 7 avoidable: post one
        # clean review, then edit it with taboo words forever.
        outcome = review_service.screen_or_warn(
            session, user, changes["comment"], config,
            book.title if book else "a book")

        if outcome.blocked:
            detail = ("That edit was blocked: it contains %d taboo words."
                      % outcome.hits)
            if outcome.warning_issued:
                detail += (" You have received a warning (%d of %d allowed)."
                           % (user.warnings, config.warning_limit))
            if outcome.suspended:
                detail += " Your account has been suspended."
            elif outcome.demoted:
                detail += " Your VIP status has been revoked."

            # Committed before raising, deliberately. The session dependency
            # rolls back when a request raises, which would undo the warning
            # and leave the API claiming a penalty it did not apply -- the
            # same mistake that made refresh-token reuse detection useless.
            session.commit()
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail)

    if "stars" in changes:
        review.stars = changes["stars"]
    if outcome is not None:
        review.raw_comment = outcome.screened.original
        review.comment = outcome.screened.masked
        review.taboo_hits = outcome.screened.hits

    session.flush()
    return present.review_out(review)


@router.delete("/api/reviews/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_review(review_id: int, user: m.User = Depends(require_user),
                  session: Session = Depends(db_session)):
    review = session.get(m.Review, review_id)
    # A super-user may remove any review; anyone else only their own. 404
    # rather than 403 for somebody else's, so the response does not confirm
    # that the review exists.
    if review is None or (review.user_id != user.id
                          and user.role != m.SUPER_USER):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such review.")
    session.delete(review)
