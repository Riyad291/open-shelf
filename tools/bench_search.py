"""Measure search at scale: ILIKE against the full-text index.

    python tools/bench_search.py --database postgresql+psycopg://... \\
        --repo ../free-programming-books

Needs an empty PostgreSQL database it may overwrite. It imports the real
catalogue, grows it by copying every row --copies times (each copy with its
own identity), and then times the same searches two ways with EXPLAIN
ANALYZE: ILIKE '%term%' across title, authors and description, and the
generated tsvector column with its GIN index. Each figure is the median of
--runs runs after one warm-up, so a cold cache does not decide the result.

This is the script behind the search numbers in the README. Run it again
and the figures will differ a little by machine; the ratio is the point.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text                 # noqa: E402
from sqlalchemy.orm import Session                         # noqa: E402

from bookstore.db.base import Base                         # noqa: E402
from bookstore.db import models                            # noqa: E402,F401
from bookstore.ingest.pipeline import (                    # noqa: E402
    get_or_create_source, reconcile,
)
from bookstore.ingest.sources import free_programming_books as fpb  # noqa: E402

ILIKE = """
SELECT id FROM resources
WHERE NOT dead AND (title ILIKE :like OR authors ILIKE :like
                    OR description ILIKE :like)
"""
FULL_TEXT = """
SELECT id FROM resources
WHERE NOT dead AND search_vector @@ websearch_to_tsquery('english', :term)
"""
BROWSE = """
SELECT id FROM resources WHERE NOT dead ORDER BY title, id LIMIT 40
"""


def grow(connection, copies: int) -> None:
    """Copy every imported row `copies` times, as distinct resources."""
    connection.execute(text("""
        INSERT INTO resources (source_id, external_id, title, authors,
            description, url, kind, media, language, licence, in_progress,
            work_key, content_hash, dead, click_count, created_at)
        SELECT source_id, external_id || '-' || g, title, authors,
            description, url || '#copy-' || g, kind, media, language,
            licence, in_progress, work_key, content_hash, dead, 0, now()
        FROM resources, generate_series(1, :copies) AS g
    """), {"copies": copies})
    connection.execute(text("ANALYZE resources"))


def explain(connection, sql: str, params: dict, runs: int) -> dict:
    """Median execution time, rows returned, and the plan's top node."""
    plan_sql = "EXPLAIN (ANALYZE, FORMAT JSON) " + sql
    times, plan = [], None
    for attempt in range(runs + 1):
        plan = connection.execute(text(plan_sql), params).scalar()
        if isinstance(plan, str):
            plan = json.loads(plan)
        if attempt:                      # the first run only warms the cache
            times.append(plan[0]["Execution Time"])
    top = plan[0]["Plan"]
    return {"ms": statistics.median(times), "rows": top.get("Actual Rows"),
            "node": _scan_nodes(top)}


def _scan_nodes(node) -> str:
    """The scans in a plan, which say whether an index was used."""
    found = []

    def walk(n):
        if "Scan" in n.get("Node Type", ""):
            found.append(n["Node Type"] + (" on " + n["Index Name"]
                                           if n.get("Index Name") else ""))
        for child in n.get("Plans", []):
            walk(child)

    walk(node)
    return ", ".join(found)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", required=True,
                        help="an empty PostgreSQL database to overwrite")
    parser.add_argument("--repo", required=True,
                        help="a clone of free-programming-books")
    parser.add_argument("--copies", type=int, default=40)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--terms", nargs="+",
                        default=["python", "machine learning", "database"])
    args = parser.parse_args()

    engine = create_engine(args.database)
    if engine.dialect.name != "postgresql":
        print("This measures PostgreSQL's full-text index; give it a "
              "PostgreSQL database.", file=sys.stderr)
        return 2

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)      # creates the search column too
    with Session(engine) as session:
        source = get_or_create_source(session, fpb.SLUG, **fpb.SOURCE_FIELDS)
        report = reconcile(session, source,
                           fpb.parse(fpb.read_local(args.repo)))
        session.commit()
    print("Imported %d resources" % report.inserted)

    with engine.begin() as connection:
        small = {"browse": explain(connection, BROWSE, {}, args.runs)}
        for term in args.terms:
            small[term] = explain(connection, ILIKE,
                                  {"like": "%" + term + "%"}, args.runs)
        grow(connection, args.copies)

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT count(*) FROM resources")).scalar()
        sizes = connection.execute(text("""
            SELECT pg_size_pretty(pg_total_relation_size('resources')),
                   pg_size_pretty(pg_relation_size('ix_resources_search'))
        """)).one()
        print("Grown to %s rows (table with indexes %s, full-text index %s)\n"
              % (format(rows, ","), sizes[0], sizes[1]))

        print("%-28s %12s %12s   %s" % ("query", "small", "large", "plan"))
        browse = explain(connection, BROWSE, {}, args.runs)
        print("%-28s %10.2f ms %9.2f ms   %s"
              % ("browse, first page", small["browse"]["ms"], browse["ms"],
                 browse["node"]))
        for term in args.terms:
            ilike = explain(connection, ILIKE, {"like": "%" + term + "%"},
                            args.runs)
            fts = explain(connection, FULL_TEXT, {"term": term}, args.runs)
            print("%-28s %10.2f ms %9.2f ms   %s"
                  % ("ILIKE '%s'" % term, small[term]["ms"], ilike["ms"],
                     ilike["node"]))
            print("%-28s %12s %9.2f ms   %s"
                  % ("full text '%s'" % term, "", fts["ms"], fts["node"]))
            print("%-28s %12s %12s   ILIKE %s rows, full text %s rows "
                  "(%.0fx faster)\n"
                  % ("", "", "", format(ilike["rows"], ","),
                     format(fts["rows"], ","), ilike["ms"] / fts["ms"]))
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
