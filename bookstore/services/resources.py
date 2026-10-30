"""Reading the resource catalogue: browse, filter, search and click through.

Every list query here runs a fixed number of statements however many rows it
returns. Topics and sources are loaded with ``selectinload``, one extra query
each, rather than lazily, one query per row. ``tests/test_resources_api.py``
counts the statements to keep it that way.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from ..db import models as m

SORTS = {
    "title": m.Resource.title.asc(),
    "newest": m.Resource.created_at.desc(),
    "popular": m.Resource.click_count.desc(),
}


def list_resources(session: Session, *, topic: Optional[str] = None,
                   media: Optional[str] = None,
                   language: Optional[str] = None,
                   query: Optional[str] = None, include_dead: bool = False,
                   sort: str = "title", limit: int = 40,
                   offset: int = 0) -> Tuple[List[m.Resource], int]:
    """One page of resources and the total that match, for pagination."""

    def narrow(statement):
        # Shared by the count and the page, so the two can never disagree.
        if not include_dead:
            statement = statement.where(m.Resource.dead.is_(False))
        if language:
            statement = statement.where(m.Resource.language == language)
        if media:
            statement = statement.where(m.Resource.media.ilike("%" + media + "%"))
        if query:
            like = "%" + query.strip() + "%"
            statement = statement.where(or_(
                m.Resource.title.ilike(like),
                m.Resource.authors.ilike(like),
                m.Resource.description.ilike(like)))
        if topic:
            in_topic = (select(m.ResourceTopic.resource_id)
                        .join(m.Topic, m.Topic.id == m.ResourceTopic.topic_id)
                        .where(m.Topic.slug == topic))
            statement = statement.where(m.Resource.id.in_(in_topic))
        return statement

    total = session.scalar(narrow(select(func.count(m.Resource.id))))

    order = SORTS.get(sort, SORTS["title"])
    page = session.scalars(
        narrow(select(m.Resource))
        .options(selectinload(m.Resource.topics)
                 .selectinload(m.ResourceTopic.topic),
                 selectinload(m.Resource.source))
        .order_by(order, m.Resource.id)
        .limit(limit).offset(offset)).all()
    return list(page), int(total or 0)


def topics_with_counts(session: Session, limit: int = 60):
    """The topics people can browse, biggest first, ignoring dead links."""
    rows = session.execute(
        select(m.Topic.slug, m.Topic.name, func.count(m.Resource.id))
        .join(m.ResourceTopic, m.ResourceTopic.topic_id == m.Topic.id)
        .join(m.Resource, m.Resource.id == m.ResourceTopic.resource_id)
        .where(m.Resource.dead.is_(False))
        .group_by(m.Topic.slug, m.Topic.name)
        .order_by(func.count(m.Resource.id).desc(), m.Topic.name)
        .limit(limit))
    return [{"slug": slug, "name": name, "count": count}
            for slug, name, count in rows]


def media_facets(session: Session):
    rows = session.execute(
        select(m.Resource.media, func.count(m.Resource.id))
        .where(m.Resource.dead.is_(False), m.Resource.media != "")
        .group_by(m.Resource.media)
        .order_by(func.count(m.Resource.id).desc()))
    return [{"name": media, "count": count} for media, count in rows]


def same_work(session: Session, resource: m.Resource) -> List[m.Resource]:
    """Other listings of the same work: another edition or format."""
    if not resource.work_key:
        return []
    return list(session.scalars(
        select(m.Resource)
        .where(m.Resource.work_key == resource.work_key,
               m.Resource.id != resource.id,
               m.Resource.dead.is_(False))
        .options(selectinload(m.Resource.source))
        .limit(5)))


def record_click(session: Session, resource_id: int) -> None:
    """Count a click-through, as one UPDATE.

    ``resource.click_count += 1`` in Python would read the count, add one and
    write it back. Two clicks at the same moment both read 41 and both write
    42: a lost update. ``click_count = click_count + 1`` is done by the
    database, so it cannot lose one.
    """
    session.execute(update(m.Resource)
                    .where(m.Resource.id == resource_id)
                    .values(click_count=m.Resource.click_count + 1))
