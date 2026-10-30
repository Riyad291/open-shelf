"""The free resource catalogue: browse, filter, search and open."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ...db import models as m
from ...services import resources as resource_service
from .. import schemas as S
from ..deps import db_session

router = APIRouter(tags=["resources"])


def _out(resource: m.Resource) -> S.ResourceOut:
    return S.ResourceOut(
        id=resource.id, title=resource.title, authors=resource.authors,
        description=resource.description, url=resource.url,
        media=resource.media, language=resource.language,
        licence=resource.licence, kind=resource.kind,
        in_progress=resource.in_progress, click_count=resource.click_count,
        topics=[link.topic.name for link in resource.topics],
        source=resource.source.name if resource.source else "")


@router.get("/api/resources", response_model=S.ResourcePageOut)
def browse(topic: Optional[str] = None, media: Optional[str] = None,
           language: Optional[str] = None,
           q: Optional[str] = Query(None, max_length=200),
           sort: str = Query("title", pattern="^(relevance|title|newest|popular)$"),
           limit: int = Query(40, ge=1, le=100),
           offset: int = Query(0, ge=0),
           session: Session = Depends(db_session)):
    items, total = resource_service.list_resources(
        session, topic=topic, media=media, language=language, query=q,
        sort=sort, limit=limit, offset=offset)
    return S.ResourcePageOut(items=[_out(r) for r in items], total=total,
                             limit=limit, offset=offset)


@router.get("/api/resources/topics", response_model=List[S.TopicOut])
def topics(session: Session = Depends(db_session)):
    return resource_service.topics_with_counts(session)


@router.get("/api/resources/{resource_id}",
            response_model=S.ResourceDetailOut)
def detail(resource_id: int, session: Session = Depends(db_session)):
    resource = session.get(m.Resource, resource_id)
    if resource is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such resource.")
    base = _out(resource)
    return S.ResourceDetailOut(
        **base.model_dump(),
        attribution=resource.source.attribution if resource.source else "",
        source_homepage=resource.source.homepage if resource.source else "",
        also_available=[_out(r) for r in
                        resource_service.same_work(session, resource)])


@router.get("/api/resources/{resource_id}/open")
def open_resource(resource_id: int, session: Session = Depends(db_session)):
    """Count the click, then send the reader to where the book lives.

    303 rather than 302: it tells the browser to follow with a GET, and it
    is never cached, so every click is counted.
    """
    resource = session.get(m.Resource, resource_id)
    if resource is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such resource.")
    resource_service.record_click(session, resource.id)
    return RedirectResponse(resource.url,
                            status_code=status.HTTP_303_SEE_OTHER)
