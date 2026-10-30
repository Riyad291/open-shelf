"""What the administrator sees: catalogue health, community and imports.

This replaces the finance report. Three ideas from it were worth keeping,
because they are the reason a report is written in SQL rather than in a
Python loop:

* **Outer joins where absence is the answer.** A topic with no resources, or
  a member who has written nothing, must still appear, with a zero. An inner
  join would quietly drop them.
* **A window function for a running total.** Catalogue growth per month,
  with the cumulative size beside it, is one query, not a loop.
* **A partitioned rank.** "The most opened resource in each topic" is
  ``rank() over (partition by topic)``; without it, it is one query per topic.
"""

from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import models as m


def _count(session: Session, statement) -> int:
    return int(session.execute(statement).scalar_one() or 0)


def summary(session: Session) -> Dict[str, Any]:
    """The headline figures, each one a single aggregate."""
    last = session.execute(
        select(m.ImportRun).order_by(m.ImportRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()

    return {
        "resources": _count(session, select(func.count(m.Resource.id))
                            .where(m.Resource.dead.is_(False))),
        "dead_links": _count(session, select(func.count(m.Resource.id))
                             .where(m.Resource.dead.is_(True))),
        "total_opens": _count(session, select(
            func.coalesce(func.sum(m.Resource.click_count), 0))),
        "sources": _count(session, select(func.count(m.Source.id))),
        "topics": _count(session, select(func.count(m.Topic.id))),
        "accounts": _count(session, select(func.count(m.User.id))),
        "suspended_accounts": _count(session, select(func.count(m.User.id))
                                     .where(m.User.suspended.is_(True))),
        "trusted_contributors": _count(session, select(func.count(m.User.id))
                                       .where(m.User.role == m.VIP)),
        "reviews": _count(session, select(func.count(m.Review.id))
                          .where(m.Review.blocked.is_(False))),
        "blocked_reviews": _count(session, select(func.count(m.Review.id))
                                  .where(m.Review.blocked.is_(True))),
        "last_import": None if last is None else {
            "status": last.status,
            "started_at": last.started_at.isoformat(),
            "inserted": last.inserted,
            "updated": last.updated,
            "unchanged": last.unchanged,
            "skipped": last.skipped,
        },
    }


def _month(session: Session, column):
    """A 'YYYY-MM' label. The two databases spell it differently."""
    if session.get_bind().dialect.name == "sqlite":
        return func.strftime("%Y-%m", column)
    return func.to_char(column, "YYYY-MM")


def catalogue_growth(session: Session) -> List[Dict[str, Any]]:
    """Resources added per month, with the running total beside each."""
    month = _month(session, m.Resource.created_at).label("month")
    monthly = (select(month, func.count(m.Resource.id).label("added"))
               .group_by(month).cte("monthly"))
    running = func.sum(monthly.c.added).over(
        order_by=monthly.c.month, rows=(None, 0))
    rows = session.execute(
        select(monthly.c.month, monthly.c.added,
               running.label("running_total"))
        .order_by(monthly.c.month))
    return [{"month": month, "added": int(added), "total": int(total)}
            for month, added, total in rows]


def topic_coverage(session: Session, limit: int = 25) -> List[Dict[str, Any]]:
    """Each topic's size, opens and dead links, including empty topics."""
    rows = session.execute(
        select(m.Topic.name,
               func.count(m.Resource.id),
               func.coalesce(func.sum(m.Resource.click_count), 0),
               func.count(m.Resource.id).filter(m.Resource.dead.is_(True)))
        .select_from(m.Topic)
        .outerjoin(m.ResourceTopic, m.ResourceTopic.topic_id == m.Topic.id)
        .outerjoin(m.Resource, m.Resource.id == m.ResourceTopic.resource_id)
        .group_by(m.Topic.name)
        .order_by(func.count(m.Resource.id).desc(), m.Topic.name)
        .limit(limit))
    return [{"topic": name, "resources": int(count), "opens": int(opens),
             "dead": int(dead)}
            for name, count, opens, dead in rows]


def most_opened(session: Session, limit: int = 15) -> List[Dict[str, Any]]:
    """The most opened resource in each topic."""
    position = func.rank().over(
        partition_by=m.Topic.name,
        order_by=m.Resource.click_count.desc()).label("position")
    ranked = (select(m.Topic.name.label("topic"),
                     m.Resource.title.label("title"),
                     m.Resource.click_count.label("opens"), position)
              .join(m.ResourceTopic, m.ResourceTopic.topic_id == m.Topic.id)
              .join(m.Resource, m.Resource.id == m.ResourceTopic.resource_id)
              .where(m.Resource.dead.is_(False))
              .cte("ranked"))
    rows = session.execute(
        select(ranked.c.topic, ranked.c.title, ranked.c.opens)
        .where(ranked.c.position == 1, ranked.c.opens > 0)
        .order_by(ranked.c.opens.desc(), ranked.c.topic)
        .limit(limit))
    return [{"topic": topic, "title": title, "opens": int(opens)}
            for topic, title, opens in rows]


def undiscovered(session: Session, limit: int = 15) -> List[Dict[str, Any]]:
    """Live resources nobody has opened yet: candidates to feature."""
    rows = session.execute(
        select(m.Resource.id, m.Resource.title, m.Source.name)
        .join(m.Source, m.Source.id == m.Resource.source_id)
        .where(m.Resource.click_count == 0, m.Resource.dead.is_(False))
        .order_by(m.Resource.title)
        .limit(limit))
    return [{"id": rid, "title": title, "source": source}
            for rid, title, source in rows]


def contributors(session: Session, limit: int = 10) -> List[Dict[str, Any]]:
    """Who writes the reviews, and how many of theirs were blocked."""
    rows = session.execute(
        select(m.User.username, m.User.role,
               func.count(m.Review.id),
               func.count(m.Review.id).filter(m.Review.blocked.is_(True)))
        .select_from(m.User)
        .outerjoin(m.Review, m.Review.user_id == m.User.id)
        .group_by(m.User.username, m.User.role)
        .having(func.count(m.Review.id) > 0)
        .order_by(func.count(m.Review.id).desc(), m.User.username)
        .limit(limit))
    return [{"username": name, "role": m.role_name(role),
             "reviews": int(total), "blocked": int(blocked)}
            for name, role, total, blocked in rows]


def moderation(session: Session) -> List[Dict[str, Any]]:
    """Taboo words caught per account, worst first."""
    rows = session.execute(
        select(m.User.username, m.User.warnings, m.User.suspended,
               func.coalesce(func.sum(m.Review.taboo_hits), 0))
        .join(m.Review, m.Review.user_id == m.User.id)
        .group_by(m.User.username, m.User.warnings, m.User.suspended)
        .having(func.coalesce(func.sum(m.Review.taboo_hits), 0) > 0)
        .order_by(func.sum(m.Review.taboo_hits).desc())
        .limit(20))
    return [{"username": name, "warnings": int(warnings),
             "suspended": bool(suspended), "taboo_hits": int(hits)}
            for name, warnings, suspended, hits in rows]


def import_history(session: Session,
                   limit: int = 10) -> List[Dict[str, Any]]:
    rows = session.execute(
        select(m.ImportRun, m.Source.name)
        .join(m.Source, m.Source.id == m.ImportRun.source_id)
        .order_by(m.ImportRun.started_at.desc())
        .limit(limit))
    return [{"source": source, "status": run.status,
             "started_at": run.started_at.isoformat(),
             "inserted": run.inserted, "updated": run.updated,
             "unchanged": run.unchanged, "skipped": run.skipped,
             "detail": run.detail}
            for run, source in rows]


def analytics(session: Session) -> Dict[str, Any]:
    return {
        "catalogue_growth": catalogue_growth(session),
        "topic_coverage": topic_coverage(session),
        "most_opened": most_opened(session),
        "undiscovered": undiscovered(session),
        "contributors": contributors(session),
        "moderation": moderation(session),
        "import_history": import_history(session),
    }
