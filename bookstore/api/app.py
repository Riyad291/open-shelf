"""FastAPI application exposing the bookstore over HTTP.

Run it with::

    python -m bookstore.api            # http://127.0.0.1:8000
    uvicorn bookstore.api.app:app --reload

Interactive API documentation is generated from the type annotations and
served at ``/docs``.

Design notes
------------
*   **No business rules live here.**  Every endpoint is transport: it validates
    input, calls the same domain service the desktop app calls, and shapes the
    result.  Search this file for a threshold or a discount and you will not
    find one.
*   **Sessions are server-side and in-memory.**  A signed-in caller holds an
    opaque token; the account itself never crosses the wire.  In-memory is the
    right scope for a single-process teaching project, and it is the one thing
    that would have to change first for a real deployment.
*   **Role checks reuse the domain's ordering.**  ``require_role`` leans on
    ``models.role_at_least``, so the API cannot drift from the privilege rules
    the desktop app enforces.
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from typing import Dict, List, Optional

from fastapi import (Depends, FastAPI, File, Header, HTTPException, Query,
                     UploadFile, status)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import imagesearch, models
from ..biometrics import BiometricService
from ..catalog import Catalog
from ..config import PROJECT_ROOT, Config, ensure_dirs
from ..chat import ChatService
from ..finance import FinanceService
from ..imagesearch import ImageIndex
from ..llm import OllamaClient
from ..models import verify_password
from ..rag import Retriever
from ..recommend import Recommender
from ..rules import RuleError, RulesEngine
from ..storage import Storage
from . import schemas as S

WEB_DIR = os.path.join(PROJECT_ROOT, "web")


# ---------------------------------------------------------------------------
# Services, built once and shared
# ---------------------------------------------------------------------------


class Services:
    def __init__(self, data_dir: Optional[str] = None,
                 config_path: Optional[str] = None):
        ensure_dirs()
        self.config = Config(config_path) if config_path else Config()
        self.storage = Storage(data_dir) if data_dir else Storage()
        self.rules = RulesEngine(self.storage, self.config)
        self.catalog = Catalog(self.storage)
        self.recommender = Recommender(self.storage, self.rules)
        self.retriever = Retriever(self.storage)
        self.llm = OllamaClient(self.config)
        self.finance = FinanceService(self.storage)
        self.chat = ChatService(self.storage, self.config, self.rules,
                                self.catalog, self.recommender,
                                self.retriever, self.llm)
        self.images = ImageIndex(self.storage)
        self.biometrics = BiometricService(self.storage)


def create_app(data_dir: Optional[str] = None,
               config_path: Optional[str] = None) -> FastAPI:
    """Build the application.

    The optional paths exist so tests can run a whole API against a temporary
    directory rather than the live shop data.
    """
    services = Services(data_dir=data_dir, config_path=config_path)
    sessions: Dict[str, str] = {}          # token -> user_id

    app = FastAPI(
        title="Inkwell & Co. Smart Bookstore API",
        version="1.0.0",
        description=(
            "HTTP interface to an AI-enabled multimodal bookstore.\n\n"
            "The same business rules serve a Tkinter desktop client and this "
            "API; no rule is implemented twice. Roles are visitor, registered "
            "customer, VIP customer and super-user, and privilege affects "
            "almost every response -- prices carry the VIP discount, "
            "recommendations are personalised, and the assistant answers more "
            "as privilege rises."
        ),
    )
    app.state.services = services
    app.state.sessions = sessions

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],           # single-user local app
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- session helpers ---------------------------------------------------

    def current_user(authorization: Optional[str] = Header(None)) -> Optional[dict]:
        """Resolve the bearer token to an account, or None for a visitor."""
        if not authorization:
            return None
        token = authorization.removeprefix("Bearer ").strip()
        user_id = sessions.get(token)
        if not user_id:
            return None
        user = services.storage.get_user(user_id)
        if not user:
            sessions.pop(token, None)
            return None
        return models.normalise_user(user)

    def require_user(user: Optional[dict] = Depends(current_user)) -> dict:
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                                "Sign in to do that.")
        allowed, why = services.rules.can_log_in(user)
        if not allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, why)
        return user

    def require_role(minimum: str):
        def dependency(user: dict = Depends(require_user)) -> dict:
            if not models.role_at_least(user.get("role", ""), minimum):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "This needs a %s account." % models.role_name(minimum),
                )
            return user
        return dependency

    require_super_user = require_role(models.SUPER_USER)

    # -- shaping -----------------------------------------------------------

    def cover_url(book: dict) -> Optional[str]:
        return "/api/books/%s/cover" % book["book_id"] if book.get("cover_path") else None

    def book_out(book: dict, user: Optional[dict]) -> S.BookOut:
        summary = services.rules.rating_summary(book["book_id"])
        return S.BookOut(
            book_id=book["book_id"],
            title=book.get("title", ""),
            author=book.get("author", ""),
            category=book.get("category", ""),
            year=int(book.get("year") or 0),
            price=round(float(book.get("price", 0.0)), 2),
            your_price=services.rules.price_for(user, book),
            stock=int(book.get("stock", 0)),
            description=book.get("description", ""),
            cover_url=cover_url(book),
            rating=summary.weighted_average if summary.count else None,
            rating_count=summary.count,
            list_status=services.rules.list_status(book["book_id"]),
        )

    def review_out(review: dict) -> S.ReviewOut:
        author_id = review.get("user_id", "")
        if author_id == RulesEngine.ANONYMOUS_AUTHOR:
            author = "A visitor"
        else:
            account = services.storage.get_user(author_id)
            author = (account.get("display_name") or account.get("username")
                      if account else "A former customer")
        return S.ReviewOut(
            review_id=review.get("review_id", ""),
            author=author,
            stars=int(review.get("stars", 0)),
            comment=review.get("comment", ""),
            verified_purchase=bool(review.get("verified_purchase")),
            taboo_hits=int(review.get("taboo_hits", 0)),
            created_at=str(review.get("created_at", "")),
        )

    def session_out(user: Optional[dict]) -> S.SessionOut:
        if user is None:
            return S.SessionOut(signed_in=False, role=models.VISITOR,
                                role_name=models.role_name(models.VISITOR))
        return S.SessionOut(
            signed_in=True,
            user_id=user["user_id"],
            username=user.get("username"),
            display_name=user.get("display_name"),
            role=user.get("role", ""),
            role_name=models.role_name(user.get("role", "")),
            balance=round(float(user.get("balance", 0.0)), 2),
            warnings=int(user.get("warnings", 0)),
            purchase_count=int(user.get("purchase_count", 0)),
            total_spent=round(float(user.get("total_spent", 0.0)), 2),
            vip_discount=(services.config.vip_discount
                          if user.get("role") == models.VIP else 0.0),
            face_enrolled=services.biometrics.face.sample_count(
                user["user_id"]) > 0,
        )

    def account_out(user: dict) -> S.AccountOut:
        models.normalise_user(user)
        return S.AccountOut(
            user_id=user["user_id"],
            username=user.get("username", ""),
            display_name=user.get("display_name", ""),
            role=user.get("role", ""),
            role_name=models.role_name(user.get("role", "")),
            balance=round(float(user.get("balance", 0.0)), 2),
            total_spent=round(float(user.get("total_spent", 0.0)), 2),
            purchase_count=int(user.get("purchase_count", 0)),
            total_fines=round(float(user.get("total_fines", 0.0)), 2),
            warnings=int(user.get("warnings", 0)),
            suspended=bool(user.get("suspended")),
            suspend_reason=user.get("suspend_reason", ""),
            face_samples=services.biometrics.face.sample_count(user["user_id"]),
        )

    def find_book(book_id: str) -> dict:
        book = services.storage.get_book(book_id)
        if not book:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "No book with id %s." % book_id)
        return models.normalise_book(book)

    # =====================================================================
    # Health and session
    # =====================================================================

    @app.get("/api/health", response_model=S.HealthOut, tags=["system"])
    def health():
        """Service state: data loaded, indexes built, model availability."""
        llm_ok, llm_status = services.llm.status()
        return S.HealthOut(
            status="ok",
            books=len(services.storage.books),
            accounts=len(services.storage.users),
            knowledge_base_chunks=services.retriever.size,
            covers_indexed=services.images.size,
            llm_ready=llm_ok,
            llm_status=llm_status,
            face_model_trained=services.biometrics.face.trained,
            face_detector=services.biometrics.face.backend_description,
        )

    @app.post("/api/auth/register", status_code=status.HTTP_201_CREATED,
              tags=["auth"])
    def register(body: S.RegisterIn):
        """Create a registered-customer account and sign it in.

        The desktop client additionally enrols a face at this point, because a
        camera is available to it. Over HTTP the face is enrolled separately,
        so a browser without camera permission can still create an account.
        """
        try:
            user = services.rules.create_account(
                body.username, body.password, role=models.REGISTERED,
                balance=body.opening_deposit,
                display_name=body.display_name,
            )
        except RuleError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

        token = secrets.token_urlsafe(24)
        sessions[token] = user["user_id"]
        return {"token": token, "session": session_out(user).model_dump()}

    @app.post("/api/auth/login", tags=["auth"])
    def login(body: S.LoginIn):
        """Exchange a username and password for a session token."""
        user = services.storage.find_user_by_username(body.username)
        if not user or not verify_password(body.password, user.get("salt", ""),
                                           user.get("password_hash", "")):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                                "No account matches that username and password.")
        models.normalise_user(user)
        allowed, why = services.rules.can_log_in(user)
        if not allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, why)

        token = secrets.token_urlsafe(24)
        sessions[token] = user["user_id"]
        return {"token": token, "session": session_out(user).model_dump()}

    @app.post("/api/auth/logout", tags=["auth"])
    def logout(authorization: Optional[str] = Header(None)):
        if authorization:
            sessions.pop(authorization.removeprefix("Bearer ").strip(), None)
        return {"ok": True}

    @app.get("/api/auth/session", response_model=S.SessionOut, tags=["auth"])
    def whoami(user: Optional[dict] = Depends(current_user)):
        """Who the caller is. A visitor is a valid answer, not an error."""
        return session_out(user)

    @app.get("/api/account/purchases",
             response_model=List[S.PurchaseRecordOut], tags=["auth"])
    def my_purchases(user: dict = Depends(require_user)):
        """The caller's own purchases, newest first.

        Scoped to the caller by the dependency, never by a query parameter --
        there is no way to ask for somebody else's history on this route.
        """
        out = []
        for entry in reversed(user.get("purchase_history", []) or []):
            book = services.storage.get_book(entry.get("book_id", "")) or {}
            out.append(S.PurchaseRecordOut(
                book_id=entry.get("book_id", ""),
                title=book.get("title", "A book we no longer stock"),
                author=book.get("author", ""),
                price_paid=float(entry.get("price_paid", 0.0)),
                at=str(entry.get("at", "")),
                cover_url=("/api/books/%s/cover" % entry["book_id"]
                           if book else None),
            ))
        return out

    # =====================================================================
    # Catalogue
    # =====================================================================

    @app.get("/api/books", response_model=List[S.BookOut], tags=["catalogue"])
    def list_books(
        category: Optional[str] = None,
        author: Optional[str] = None,
        decade: Optional[str] = Query(None, description="e.g. 1850s"),
        user: Optional[dict] = Depends(current_user),
    ):
        """Browse by category, author or decade -- the three axes in the spec."""
        if category:
            books = services.catalog.by_category(category)
        elif author:
            books = services.catalog.by_author(author)
        elif decade:
            books = services.catalog.by_decade(decade)
        else:
            books = services.catalog.all_books()
        return [book_out(b, user) for b in books]

    @app.get("/api/books/facets", tags=["catalogue"])
    def facets():
        """The values available on each browsing axis, with counts."""
        return {
            "categories": [{"value": v, "count": c}
                           for v, c in services.catalog.categories()],
            "authors": [{"value": v, "count": c}
                        for v, c in services.catalog.authors()],
            "decades": [{"value": v, "count": c}
                        for v, c in services.catalog.decades()],
        }

    @app.get("/api/books/{book_id}", response_model=S.BookDetail,
             tags=["catalogue"])
    def get_book(book_id: str, user: Optional[dict] = Depends(current_user)):
        """One book, and viewing it counts as a browse (it feeds R2)."""
        book = find_book(book_id)
        services.rules.record_browse(user, book)
        summary = services.rules.rating_summary(book_id)
        base = book_out(book, user)
        return S.BookDetail(
            **base.model_dump(),
            plain_rating=summary.plain_average if summary.count else None,
            verified_buyers=summary.purchaser_count,
            browse_count=int(book.get("browse_count", 0)),
            purchase_count=int(book.get("purchase_count", 0)),
            figures=["/api/books/%s/figure/%d" % (book_id, i)
                     for i in range(len(book.get("figure_paths") or []))],
            reviews=[review_out(r) for r in
                     sorted(services.storage.reviews_for_book(book_id),
                            key=lambda r: r.get("created_at", ""), reverse=True)],
        )

    def _resolve(path: str) -> Optional[str]:
        if not path:
            return None
        candidate = path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)
        return candidate if os.path.exists(candidate) else None

    @app.get("/api/books/{book_id}/cover", tags=["catalogue"])
    def cover(book_id: str):
        resolved = _resolve(find_book(book_id).get("cover_path", ""))
        if not resolved:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No cover image.")
        return FileResponse(resolved)

    @app.get("/api/books/{book_id}/figure/{index}", tags=["catalogue"])
    def figure(book_id: str, index: int):
        figures = find_book(book_id).get("figure_paths") or []
        if not 0 <= index < len(figures):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such figure.")
        resolved = _resolve(figures[index])
        if not resolved:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Figure missing.")
        return FileResponse(resolved)

    @app.get("/api/search", response_model=List[S.SearchHitOut],
             tags=["catalogue"])
    def search(q: str = Query(..., min_length=1), limit: int = 20,
               user: Optional[dict] = Depends(current_user)):
        """Keyword search over titles, authors, descriptions and book text."""
        return [
            S.SearchHitOut(book=book_out(h.book, user), score=h.score,
                           matched=h.matched, where=h.where)
            for h in services.catalog.search(q, limit=limit)
        ]

    @app.post("/api/search/image", response_model=List[S.ImageHitOut],
              tags=["catalogue"])
    async def search_by_image(file: UploadFile = File(...),
                              user: Optional[dict] = Depends(current_user)):
        """Find a book from a photograph of its cover or an internal figure."""
        if not imagesearch.available():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                imagesearch.missing_dependency_message())
        suffix = os.path.splitext(file.filename or "")[1] or ".png"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = tmp.name
        try:
            services.images.build()
            hits = services.images.search(
                tmp_path,
                limit=int(services.config.get("image_search_results") or 8))
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return [S.ImageHitOut(book=book_out(h.book, user),
                              similarity=h.similarity, kind=h.kind)
                for h in hits]

    @app.get("/api/recommendations", response_model=List[S.RecommendationOut],
             tags=["catalogue"])
    def recommendations(user: Optional[dict] = Depends(current_user)):
        """Top three: personalised for RC/VC, store-wide for SU and visitors."""
        count = services.config.recommendation_count
        return [S.RecommendationOut(book=book_out(r.book, user), reason=r.reason)
                for r in services.recommender.top_for(user, count)]

    # =====================================================================
    # Transactions and reviews
    # =====================================================================

    @app.post("/api/books/{book_id}/purchase",
              response_model=S.PurchaseResponse, tags=["shopping"])
    def purchase(book_id: str, user: dict = Depends(require_user)):
        """Buy a book. A purchase beyond the balance is refused and fined."""
        book = find_book(book_id)
        result = services.rules.purchase(user, book)
        return S.PurchaseResponse(
            ok=result.ok, message=result.message,
            price_paid=result.price_paid, fine_applied=result.fine_applied,
            new_balance=result.new_balance, promoted=result.promoted,
            suspended=result.suspended,
        )

    @app.post("/api/books/{book_id}/reviews", response_model=S.ReviewResponse,
              tags=["shopping"])
    def review(book_id: str, body: S.ReviewIn,
               user: Optional[dict] = Depends(current_user)):
        """Rate a book. Any user may, including a visitor (R7)."""
        book = find_book(book_id)
        result = services.rules.submit_review(user, book, body.stars,
                                              body.comment)
        return S.ReviewResponse(
            ok=result.ok, message=result.message, blocked=result.blocked,
            taboo_hits=result.taboo_hits, warning_issued=result.warning_issued,
            warnings_total=result.warnings_total, suspended=result.suspended,
            demoted=result.demoted,
        )

    # =====================================================================
    # Assistant
    # =====================================================================

    @app.post("/api/chat", response_model=S.ChatOut, tags=["assistant"])
    def chat(body: S.ChatIn, user: Optional[dict] = Depends(current_user)):
        """Ask a question.

        The answer is scoped to the caller's role and labelled with where it
        came from: store records, retrieval over local files, or the language
        model.
        """
        answer = services.chat.ask(user, body.question)
        return S.ChatOut(
            text=answer.text, source=answer.source,
            citations=answer.citations, rag_score=answer.rag_score,
            denied_for_role=answer.denied_for_role,
        )

    # =====================================================================
    # Super-user
    # =====================================================================

    @app.post("/api/admin/books", response_model=S.BookOut,
              status_code=status.HTTP_201_CREATED, tags=["admin"])
    def add_book(body: S.BookIn, user: dict = Depends(require_super_user)):
        try:
            book = services.catalog.add_book(
                body.title, body.author, body.category, body.year, body.price,
                stock=body.stock, description=body.description,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        return book_out(book, user)

    @app.patch("/api/admin/books/{book_id}", response_model=S.BookOut,
               tags=["admin"])
    def update_book(book_id: str, stock: Optional[int] = None,
                    price: Optional[float] = None,
                    user: dict = Depends(require_super_user)):
        find_book(book_id)
        changes = {}
        if stock is not None:
            changes["stock"] = max(0, int(stock))
        if price is not None:
            changes["price"] = round(max(0.0, float(price)), 2)
        if not changes:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Pass stock or price.")
        return book_out(services.catalog.update_book(book_id, **changes), user)

    @app.delete("/api/admin/books/{book_id}",
                status_code=status.HTTP_204_NO_CONTENT, tags=["admin"])
    def delete_book(book_id: str, user: dict = Depends(require_super_user)):
        find_book(book_id)
        services.catalog.delete_book(book_id)

    @app.get("/api/admin/accounts", response_model=List[S.AccountOut],
             tags=["admin"])
    def accounts(user: dict = Depends(require_super_user)):
        return [account_out(u) for u in services.storage.list_users()]

    @app.post("/api/admin/accounts/{user_id}/deposit",
              response_model=S.AccountOut, tags=["admin"])
    def deposit(user_id: str, body: S.DepositIn,
                user: dict = Depends(require_super_user)):
        target = services.storage.get_user(user_id)
        if not target:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such account.")
        try:
            services.rules.deposit(target, body.amount, by_super_user=True)
        except RuleError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        return account_out(target)

    @app.post("/api/admin/accounts/{user_id}/reopen",
              response_model=S.AccountOut, tags=["admin"])
    def reopen(user_id: str, body: S.DepositIn,
               user: dict = Depends(require_super_user)):
        """Lift a suspension, which requires the balance to end up positive."""
        target = services.storage.get_user(user_id)
        if not target:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such account.")
        try:
            services.rules.reopen_account(target, body.amount)
        except RuleError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        return account_out(target)

    @app.get("/api/admin/analytics", tags=["admin"])
    def analytics(user: dict = Depends(require_super_user)):
        """Reports computed in SQL over a throwaway projection of the files.

        Built fresh on each request. The projection lives in memory, is never
        written back, and the text files in data/ stay the system of record --
        see bookstore/analytics.py. For a shop this size rebuilding costs a
        few milliseconds and removes any chance of a stale report.
        """
        from ..analytics import Analytics
        with Analytics(services.storage) as sql:
            return sql.report()

    @app.get("/api/admin/finance", response_model=S.FinanceOut, tags=["admin"])
    def finance(user: dict = Depends(require_super_user)):
        """Every figure derived from the transaction ledger."""
        report = services.finance.report()
        return S.FinanceOut(
            revenue_sales=report.revenue_sales,
            revenue_fines=report.revenue_fines,
            refunds=report.refunds,
            total_revenue=report.total_revenue,
            purchase_count=report.purchase_count,
            fine_count=report.fine_count,
            average_order=report.average_order,
            customer_liability=report.customer_liability,
            inventory_value=report.inventory_value,
            suspended_count=report.suspended_count,
            negative_balance_count=report.negative_balance_count,
            top_books=[{"title": t, "units": u, "revenue": r}
                       for t, u, r in report.top_books],
            revenue_by_category=[{"category": c, "revenue": v}
                                 for c, v in report.revenue_by_category],
            user_counts=report.user_counts,
        )

    @app.get("/api/admin/order-lists", response_model=S.OrderListsOut,
             tags=["admin"])
    def order_lists(user: dict = Depends(require_super_user)):
        lists = services.rules.order_lists()

        def row(entry):
            return S.OrderListRow(
                book=book_out(entry["book"], user), count=entry["count"],
                average=entry["average"], plain_average=entry["plain_average"],
            )

        return S.OrderListsOut(
            no_order=[row(e) for e in lists["no_order"]],
            to_order=[row(e) for e in lists["to_order"]],
        )

    @app.get("/api/admin/taboo", tags=["admin"])
    def taboo_words(user: dict = Depends(require_super_user)):
        return {"words": services.config.taboo_words}

    @app.post("/api/admin/taboo", tags=["admin"])
    def add_taboo(body: S.TabooIn, user: dict = Depends(require_super_user)):
        added = services.config.add_taboo_word(body.word)
        return {"added": added, "words": services.config.taboo_words}

    @app.delete("/api/admin/taboo/{word}", tags=["admin"])
    def remove_taboo(word: str, user: dict = Depends(require_super_user)):
        removed = services.config.remove_taboo_word(word)
        if not removed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not on the list.")
        return {"removed": True, "words": services.config.taboo_words}

    # =====================================================================
    # Static web client
    # =====================================================================

    if os.path.isdir(WEB_DIR):
        app.mount("/app", StaticFiles(directory=WEB_DIR, html=True), name="web")

        @app.get("/", include_in_schema=False)
        def root():
            return RedirectResponse("/app/")

    return app


app = create_app()
