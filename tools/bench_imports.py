"""Race several importers on the same feed, two ways, and count the damage.

    python tools/bench_imports.py --database postgresql+psycopg://... \\
        --workers 8 --rows 500 --trials 5

Needs a PostgreSQL database it may overwrite (SQLite serialises writers,
so it cannot show the race). For each strategy, every trial starts from
empty tables, then --workers threads wait at a barrier and import the same
--rows entries at the same instant:

* check-then-insert: read which external ids exist, insert the rest. The
  obvious way to write an importer.
* upsert: the pipeline's INSERT ... ON CONFLICT DO UPDATE.

It reports duplicate rows and importers that died. The UNIQUE constraint on
(source_id, external_id) keeps duplicates at zero either way -- the point is
the second column.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, func, select           # noqa: E402
from sqlalchemy.exc import IntegrityError                    # noqa: E402
from sqlalchemy.orm import Session                           # noqa: E402

from bookstore.db.base import Base                           # noqa: E402
from bookstore.db import models as m                         # noqa: E402
from bookstore.ingest.parsers import ParsedResource          # noqa: E402
from bookstore.ingest.pipeline import (                      # noqa: E402
    get_or_create_source, reconcile,
)


def feed(rows: int):
    return [ParsedResource(external_id="ext-%d" % i, title="Book %d" % i,
                           url="https://example.org/book-%d" % i)
            for i in range(rows)]


def check_then_insert(session: Session, source_id: int, items) -> None:
    """The obvious importer: look first, then insert what is missing."""
    existing = set(session.execute(
        select(m.Resource.external_id)
        .where(m.Resource.source_id == source_id)).scalars())
    for item in items:
        if item.external_id not in existing:
            session.add(m.Resource(
                source_id=source_id, external_id=item.external_id,
                title=item.title, url=item.url,
                content_hash=item.content_hash()))
    session.commit()


def upsert(session: Session, source_id: int, items) -> None:
    reconcile(session, session.get(m.Source, source_id), items)
    session.commit()


def trial(engine, strategy, workers: int, rows: int):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source_id = get_or_create_source(session, "bench", name="Bench").id
        session.commit()

    items = feed(rows)
    barrier = threading.Barrier(workers)
    crashed = []

    def run():
        with Session(engine) as session:
            barrier.wait()
            try:
                strategy(session, source_id, items)
            except IntegrityError:
                crashed.append(1)
                session.rollback()

    threads = [threading.Thread(target=run) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with Session(engine) as session:
        total = session.scalar(select(func.count(m.Resource.id)))
    return total - rows, len(crashed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--rows", type=int, default=500)
    parser.add_argument("--trials", type=int, default=5)
    args = parser.parse_args()

    engine = create_engine(args.database, pool_size=args.workers + 2,
                           max_overflow=4)
    if engine.dialect.name != "postgresql":
        print("Give it a PostgreSQL database: SQLite cannot show the race.",
              file=sys.stderr)
        return 2

    print("%d importers, %d rows, %d trials each\n"
          % (args.workers, args.rows, args.trials))
    print("%-20s %18s %26s" % ("strategy", "duplicate rows",
                                "importers that crashed"))
    for name, strategy in [("check-then-insert", check_then_insert),
                           ("upsert", upsert)]:
        duplicates = crashes = 0
        for _ in range(args.trials):
            dup, crashed = trial(engine, strategy, args.workers, args.rows)
            duplicates += dup
            crashes += crashed
        print("%-20s %18d %19d of %d"
              % (name, duplicates, crashes, args.workers * args.trials))
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
