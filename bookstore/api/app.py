"""The application.

One rules engine, one database, one HTTP surface. The Tkinter client is gone;
everything the specification asks for is served from here and rendered by the
browser client in ``web/``.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError

from ..config import PROJECT_ROOT, ensure_dirs
from ..db import models as m
from ..db.base import Base, configure as configure_db, engine
from ..db.base import session_factory
from . import runtime
from . import schemas as S
from .routers import (
    admin, auth, biometrics, cart, catalog, chat, resources, shelfscan,
)


DESCRIPTION = """
An AI-enabled multimodal bookstore.

Four privilege tiers decide almost every response: prices carry the VIP
discount, recommendations are personalised, and the assistant answers more as
privilege rises.

Sign in at `/api/auth/login` for an access token and a refresh token. Send the
access token as `Authorization: Bearer <token>`; it lasts fifteen minutes, and
`/api/auth/refresh` exchanges the refresh token for a new pair.
"""


def create_app(database: Optional[str] = None,
               config_path: Optional[str] = None,
               data_dir: Optional[str] = None,
               create_tables: bool = False) -> FastAPI:
    """Build the application.

    The optional arguments exist so tests can run a whole API against a
    throwaway database and data directory rather than the live shop.
    """
    ensure_dirs()
    runtime.configure(config_path=config_path, data_dir=data_dir)
    if database:
        configure_db(database)
    if create_tables:
        Base.metadata.create_all(engine())

    app = FastAPI(
        title="Inkwell & Co. Smart Bookstore API",
        version="2.0.0",
        description=DESCRIPTION,
    )

    app.add_middleware(
        CORSMiddleware,
        # The browser client is served from the same origin, so this is for
        # local development against a separate front-end dev server. A
        # deployment behind a real domain should name it.
        allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for router in (auth.router, catalog.router, cart.router, chat.router,
                   biometrics.router, shelfscan.router, admin.router,
                   resources.router):
        app.include_router(router)

    # -- error handling ---------------------------------------------------

    @app.exception_handler(IntegrityError)
    def _integrity(request: Request, exc: IntegrityError):
        """A constraint refused the write.

        These are the schema's guarantees firing -- duplicate usernames,
        negative stock, a duplicate cart line. They are conflicts with the
        current state, not server faults, and the database message is not
        shown because it names tables and columns.
        """
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "That conflicts with something already stored."},
        )

    @app.exception_handler(OperationalError)
    def _operational(request: Request, exc: OperationalError):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "The database is not reachable right now."},
        )

    # -- health -----------------------------------------------------------

    @app.get("/api/health", response_model=S.HealthOut, tags=["meta"])
    def health():
        """What this process is actually serving.

        Reports the shop it can see, not merely that the port is open: a
        container that cannot read its own data is unhealthy even though it
        answers.
        """
        service = runtime.face_recogniser()
        llm = runtime.llm()
        llm_ready = llm.is_up()
        llm_note = ("Ollama is answering at %s." % llm.base_url if llm_ready
                    else "Ollama is not reachable at %s. Start it with "
                         "'ollama serve', then 'ollama pull %s'."
                         % (llm.base_url, llm.model))

        session = session_factory()()
        try:
            books = session.execute(select(func.count(m.Book.id))).scalar_one()
            users = session.execute(select(func.count(m.User.id))).scalar_one()
            orders = session.execute(
                select(func.count(m.Order.id))
                .where(m.Order.status == "paid")).scalar_one()
            database_state = "ok"
        except OperationalError:
            books = users = orders = 0
            database_state = "unreachable"
        finally:
            session.close()

        return S.HealthOut(
            status="ok" if database_state == "ok" else "degraded",
            database=database_state,
            books=int(books), accounts=int(users), orders=int(orders),
            knowledge_base_chunks=len(runtime.retriever().chunks),
            face_model_trained=service.trained,
            face_detector=service.backend_description,
            llm_ready=bool(llm_ready), llm_status=llm_note,
        )

    # -- the browser client -----------------------------------------------

    web_dir = os.path.join(PROJECT_ROOT, "web")
    if os.path.isdir(web_dir):
        app.mount("/app", StaticFiles(directory=web_dir, html=True),
                  name="web")

        @app.get("/", include_in_schema=False)
        def root():
            return RedirectResponse("/app/")

    return app


app = create_app()
