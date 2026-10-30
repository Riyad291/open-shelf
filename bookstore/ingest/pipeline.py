"""Write parsed records into the catalogue, safely and repeatably.

The guarantees, each with a test:

* **Idempotent.** Running the same import twice inserts nothing the second
  time. Identity is ``(source_id, external_id)``, enforced by a UNIQUE
  constraint, and rows are written with an upsert.
* **Safe under concurrency.** Two imports of the same source running at once
  neither duplicate rows nor crash. A check-then-insert does not give you
  the second half: measured with eight overlapping importers, it produced no
  duplicates but seven of the eight died on an IntegrityError. The upsert
  turns the race into an update.
* **Ours stays ours.** Columns upstream does not control -- clicks, link
  health -- are never in the upsert's SET list, so an import cannot reset
  them.
* **Nothing vanishes.** An entry removed upstream is not deleted here; it
  may have reviews or clicks attached. Deciding what to do with it is a
  separate, deliberate step.
* **Audited.** Every run leaves an ``import_runs`` row, including failures.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, Iterable, List

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from ..db import models as m
from .parsers import ParsedResource

BATCH_SIZE = 500

# The columns an import is allowed to overwrite. Anything not listed here
# -- click_count, dead, last_status -- belongs to this site.
UPSTREAM_COLUMNS = ("title", "authors", "description", "url", "kind",
                    "media", "language", "licence", "in_progress",
                    "work_key", "content_hash", "updated_at")


class IngestError(RuntimeError):
    pass


@dataclass
class ImportReport:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0

    @property
    def written(self) -> int:
        return self.inserted + self.updated

    def __str__(self) -> str:
        return "%d inserted, %d updated, %d unchanged, %d skipped" % (
            self.inserted, self.updated, self.unchanged, self.skipped)


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:96] or "other"


def _insert_for(session: Session):
    """The dialect's INSERT, which is the one that has ON CONFLICT."""
    name = session.get_bind().dialect.name
    if name == "postgresql":
        return postgresql.insert
    if name == "sqlite":
        return sqlite.insert
    raise IngestError(
        "Importing needs ON CONFLICT, which %s does not have. (MySQL has "
        "ON DUPLICATE KEY UPDATE; it would need its own branch.)" % name)


def get_or_create_source(session: Session, slug: str, name: str,
                         **fields) -> m.Source:
    source = session.scalar(select(m.Source).where(m.Source.slug == slug))
    if source is None:
        source = m.Source(slug=slug, name=name, **fields)
        session.add(source)
        session.flush()
        return source
    for key, value in dict(fields, name=name).items():
        if value and getattr(source, key) != value:
            setattr(source, key, value)
    session.flush()
    return source


def _existing_hashes(session: Session, source: m.Source) -> Dict[str, str]:
    """Every stored row's fingerprint, in one query rather than one each."""
    rows = session.execute(
        select(m.Resource.external_id, m.Resource.content_hash)
        .where(m.Resource.source_id == source.id))
    return {external_id: digest for external_id, digest in rows}


def _as_row(source: m.Source, item: ParsedResource, now) -> dict:
    return {
        "source_id": source.id,
        "external_id": item.external_id[:255],
        "title": item.title[:512],
        "authors": item.authors[:512],
        "description": item.description,
        "url": item.url[:1024],
        "kind": item.kind,
        "media": item.media[:64],
        "language": item.language[:8],
        "licence": item.licence[:64],
        "in_progress": item.in_progress,
        "work_key": item.work_key[:64],
        "content_hash": item.content_hash(),
        "updated_at": now,
    }


def _upsert_resources(session: Session, rows: List[dict]) -> None:
    insert = _insert_for(session)
    statement = insert(m.Resource.__table__).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[m.Resource.source_id, m.Resource.external_id],
        set_={column: getattr(statement.excluded, column)
              for column in UPSTREAM_COLUMNS})
    session.execute(statement)


def _sync_topics(session: Session, source: m.Source,
                 items: List[ParsedResource]) -> None:
    """Make sure every topic exists, then link resources to them.

    Both inserts use ON CONFLICT DO NOTHING, for the same reason the
    resource insert is an upsert: another import may be creating the same
    topic at the same moment.
    """
    insert = _insert_for(session)

    names: Dict[str, str] = {}
    for item in items:
        for topic in item.topics:
            names.setdefault(slugify(topic), topic[:96])
    if not names:
        return

    existing = {slug for (slug,) in session.execute(
        select(m.Topic.slug).where(m.Topic.slug.in_(names)))}
    missing = [{"slug": slug, "name": name, "kind": "subject"}
               for slug, name in names.items() if slug not in existing]
    if missing:
        session.execute(insert(m.Topic.__table__).values(missing)
                        .on_conflict_do_nothing(index_elements=[m.Topic.slug]))

    # A Result has a .keys() method, so dict(result) would treat it as a
    # mapping and fail. The comprehension says what is meant.
    topic_ids = {slug: topic_id for slug, topic_id in session.execute(
        select(m.Topic.slug, m.Topic.id).where(m.Topic.slug.in_(names)))}
    resource_ids = {external_id: resource_id
                    for external_id, resource_id in session.execute(
                        select(m.Resource.external_id, m.Resource.id)
                        .where(m.Resource.source_id == source.id,
                               m.Resource.external_id.in_(
                                   [i.external_id for i in items])))}

    links = []
    for item in items:
        resource_id = resource_ids.get(item.external_id)
        if resource_id is None:
            continue
        for topic in item.topics:
            links.append({"resource_id": resource_id,
                          "topic_id": topic_ids[slugify(topic)]})
    if links:
        session.execute(
            insert(m.ResourceTopic.__table__).values(links)
            .on_conflict_do_nothing(
                index_elements=[m.ResourceTopic.resource_id,
                                m.ResourceTopic.topic_id]))


def reconcile(session: Session, source: m.Source,
              parsed_rows: Iterable[ParsedResource], *,
              dry_run: bool = False) -> ImportReport:
    """Bring the stored catalogue in line with what the source lists.

    Resources that have vanished upstream are not deleted (see the module
    docstring).
    """
    report = ImportReport()
    stored = _existing_hashes(session, source)
    seen = set()
    to_write: List[ParsedResource] = []

    for item in parsed_rows:
        if not item.external_id or not item.url:
            report.skipped += 1
            continue
        if item.external_id in seen:
            # The same link listed twice in the feed: keep the first.
            report.skipped += 1
            continue
        seen.add(item.external_id)

        digest = stored.get(item.external_id)
        if digest is None:
            report.inserted += 1
            to_write.append(item)
        elif digest != item.content_hash():
            report.updated += 1
            to_write.append(item)
        else:
            report.unchanged += 1

    if dry_run or not to_write:
        return report

    now = m.utcnow()
    for start in range(0, len(to_write), BATCH_SIZE):
        batch = to_write[start:start + BATCH_SIZE]
        _upsert_resources(session, [_as_row(source, i, now) for i in batch])
        session.flush()
        _sync_topics(session, source, batch)
        session.flush()
    return report


def run_import(session: Session, source: m.Source,
               parsed_rows: Iterable[ParsedResource], *,
               dry_run: bool = False) -> ImportReport:
    """``reconcile`` with an audit row around it."""
    run = m.ImportRun(source_id=source.id, status="running")
    session.add(run)
    session.flush()
    try:
        report = reconcile(session, source, parsed_rows, dry_run=dry_run)
    except Exception as exc:
        run.status = "failed"
        run.finished_at = m.utcnow()
        run.detail = ("%s: %s" % (type(exc).__name__, exc))[:2000]
        session.flush()
        raise

    run.status = "ok"
    run.finished_at = m.utcnow()
    run.inserted = report.inserted
    run.updated = report.updated
    run.unchanged = report.unchanged
    run.skipped = report.skipped
    run.detail = "dry run" if dry_run else ""
    session.flush()
    return report
