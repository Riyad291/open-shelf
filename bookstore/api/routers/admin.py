"""Administration: catalogue, accounts, insights and moderation.

Specification items 5, 6 and 7. Every route here depends on
``require_super_user``, so privilege is declared rather than remembered.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...db import models as m
from ...services import accounts, catalog as catalog_service
from ...services import insights as insight_service
from ...services import reviews as review_service
from .. import present, runtime
from .. import schemas as S
from ..deps import db_session, require_super_user


router = APIRouter(prefix="/api/admin", tags=["admin"],
                   dependencies=[Depends(require_super_user)])


# ------------------------------------------------------- books (full CRUD)


@router.get("/books", response_model=S.PageOut)
def list_books(limit: int = Query(100, ge=1, le=200),
               offset: int = Query(0, ge=0),
               session: Session = Depends(db_session)):
    books, total = catalog_service.list_books(session, limit=limit,
                                              offset=offset)
    return S.PageOut(
        items=present.books_out(session, books, None, runtime.config()),
        total=total, limit=limit, offset=offset)


@router.post("/books", response_model=S.BookOut,
             status_code=status.HTTP_201_CREATED)
def create_book(body: S.BookCreateIn, session: Session = Depends(db_session)):
    book = m.Book(
        title=body.title.strip(), author=body.author.strip(),
        category=body.category.strip().lower(), year=body.year,
        description=body.description or "")
    session.add(book)
    session.flush()
    return present.book_out(book, None, runtime.config())


@router.patch("/books/{book_id}", response_model=S.BookOut)
def update_book(book_id: int, body: S.BookUpdateIn,
                session: Session = Depends(db_session)):
    book = session.get(m.Book, book_id)
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such book.")

    # exclude_unset is what distinguishes "set this field" from "leave it
    # alone". Without it a PATCH blanks every field the client omitted.
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(book, field, value)

    session.flush()
    summary = review_service.rating_summary(session, book.id)
    return present.book_out(book, None, runtime.config(), summary)


@router.delete("/books/{book_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_book(book_id: int, session: Session = Depends(db_session)):
    book = session.get(m.Book, book_id)
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such book.")

    reviewed = session.execute(
        select(func.count(m.Review.id)).where(m.Review.book_id == book_id)
    ).scalar_one()

    # Reviews are other people's writing. Deleting a book would cascade to
    # them, so it is refused with an explanation rather than done silently.
    if reviewed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "'%s' carries %d review(s) and cannot be deleted without taking "
            "them with it." % (book.title, reviewed))

    session.delete(book)


# --------------------------------------------------- accounts (full CRUD)


@router.get("/accounts", response_model=List[S.AccountOut])
def list_accounts(session: Session = Depends(db_session)):
    users = session.execute(
        select(m.User).order_by(m.User.username)).scalars().all()
    biometrics = runtime.face_recogniser()
    return [present.account_out(u, biometrics.sample_count(str(u.id)))
            for u in users]


@router.get("/accounts/{user_id}", response_model=S.AccountOut)
def one_account(user_id: int, session: Session = Depends(db_session)):
    user = session.get(m.User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such account.")
    return present.account_out(
        user, runtime.face_recogniser().sample_count(str(user.id)))


@router.patch("/accounts/{user_id}", response_model=S.AccountOut)
def update_account(user_id: int, body: S.AccountUpdateIn,
                   session: Session = Depends(db_session)):
    user = session.get(m.User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such account.")

    changes = body.model_dump(exclude_unset=True)
    if "display_name" in changes:
        user.display_name = changes["display_name"]
    if "role" in changes and changes["role"] != user.role:
        session.add(m.Event(kind="role_changed", user_id=user.id,
                            detail="%s -> %s by super-user"
                                   % (user.role, changes["role"])))
        user.role = changes["role"]

    session.flush()
    return present.account_out(user)


@router.delete("/accounts/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(user_id: int, actor: m.User = Depends(require_super_user),
                   session: Session = Depends(db_session)):
    user = session.get(m.User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such account.")
    if user.id == actor.id:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "You cannot delete the account you are using.")

    authored = session.execute(
        select(func.count(m.Review.id)).where(m.Review.user_id == user_id)
    ).scalar_one()
    if authored:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "That account has written %d review(s). Suspend it instead -- "
            "deleting it would take the reviews with it." % authored)

    session.delete(user)


@router.post("/accounts/{user_id}/reopen", response_model=S.AccountOut)
def reopen(user_id: int, body: S.ReopenIn | None = None,
           session: Session = Depends(db_session)):
    """Specification item 6: only an administrator reopens an account."""
    user = session.get(m.User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such account.")
    accounts.reopen(session, user,
                    clear_warnings=bool(body and body.clear_warnings))
    return present.account_out(user)


@router.post("/accounts/{user_id}/suspend", response_model=S.AccountOut)
def suspend(user_id: int, body: S.SuspendIn,
            actor: m.User = Depends(require_super_user),
            session: Session = Depends(db_session)):
    user = session.get(m.User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such account.")
    if user.id == actor.id:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "You cannot suspend the account you are using.")
    accounts.suspend(session, user, body.reason)
    return present.account_out(user)


# ---------------------------------------------------------------- reports


@router.get("/summary")
def summary(session: Session = Depends(db_session)):
    """Specification item 3: the administrator's report on the site."""
    return insight_service.summary(session)


@router.get("/analytics")
def analytics(session: Session = Depends(db_session)):
    """Reporting queries: CTEs, window functions and deliberate outer joins."""
    return insight_service.analytics(session)


@router.get("/order-lists", response_model=S.OrderListsOut)
def order_lists(session: Session = Depends(db_session)):
    """Specification item 7: the to-order and no-order lists."""
    lists = review_service.order_lists(session, runtime.config())
    return S.OrderListsOut(**lists)


# ------------------------------------------------- taboo words (full CRUD)


@router.get("/taboo", response_model=List[str])
def list_taboo(session: Session = Depends(db_session)):
    rows = session.execute(
        select(m.TabooWord.word).order_by(m.TabooWord.word)).scalars().all()
    return list(rows) or list(runtime.config().taboo_words)


@router.post("/taboo", response_model=List[str],
             status_code=status.HTTP_201_CREATED)
def add_taboo(body: S.TabooIn, session: Session = Depends(db_session)):
    word = body.word.strip().lower()
    session.add(m.TabooWord(word=word))
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "'%s' is already on the list." % word)

    _sync_taboo(session)
    return list_taboo(session)


@router.delete("/taboo/{word}", response_model=List[str])
def remove_taboo(word: str, session: Session = Depends(db_session)):
    row = session.get(m.TabooWord, word.strip().lower())
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "'%s' is not on the list." % word)
    session.delete(row)
    session.flush()
    _sync_taboo(session)
    return list_taboo(session)


def _sync_taboo(session: Session) -> None:
    """Keep the running config in step with the table.

    The moderation screen reads its word list from the config object, so a
    word added through the API has to reach it or the change would only take
    effect after a restart.
    """
    words = session.execute(
        select(m.TabooWord.word).order_by(m.TabooWord.word)).scalars().all()
    runtime.config().set("taboo_words", list(words))
