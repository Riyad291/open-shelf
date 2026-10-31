"""Feature items 3, 9, 12 and 13: the role-tiered assistant.

The assistant itself is unchanged -- the same intent routing, the same
privilege tiers, the same RAG-before-LLM dispatch, and the same rule that
every answer says where it came from. It reads the relational catalogue
through the facades in services/chat_adapter.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ...services import chat_adapter
from .. import runtime
from .. import schemas as S
from ..deps import current_user, db_session


router = APIRouter(tags=["assistant"])


@router.post("/api/chat", response_model=S.ChatOut)
def ask(body: S.ChatIn, user=Depends(current_user),
        session: Session = Depends(db_session)):
    service = chat_adapter.build_chat_service(
        session, runtime.config(), runtime.files(),
        runtime.retriever(), runtime.llm())

    answer = service.ask(chat_adapter.user_as_dict(user, session),
                         body.question)

    return S.ChatOut(
        text=answer.text,
        source=answer.source,
        citations=list(getattr(answer, "citations", []) or []),
        rag_score=float(getattr(answer, "rag_score", 0.0) or 0.0),
        denied_for_role=bool(getattr(answer, "denied_for_role", False)),
    )
