"""Import free resources from a catalogue.

    python tools/import_resources.py free-programming-books
    python tools/import_resources.py free-programming-books --repo ../free-programming-books
    python tools/import_resources.py free-programming-books --dry-run

Safe to run as often as you like: a second run reports everything unchanged.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bookstore.db import engine, session_factory          # noqa: E402
from bookstore.ingest.pipeline import (                    # noqa: E402
    get_or_create_source, run_import,
)
from bookstore.ingest.sources import REGISTRY              # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", choices=sorted(REGISTRY))
    parser.add_argument("--repo", metavar="DIR",
                        help="read from a local clone instead of fetching")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args(argv)

    module = REGISTRY[args.source]
    session = session_factory()()
    try:
        source = get_or_create_source(session, module.SLUG,
                                      **module.SOURCE_FIELDS)
        if args.repo:
            documents = module.read_local(args.repo)
            where = args.repo
        else:
            documents = module.read_remote()
            where = "GitHub"
        print("Importing %s from %s..." % (source.name, where))
        report = run_import(session, source, module.parse(documents),
                            dry_run=args.dry_run)
        session.commit()
    except Exception as exc:
        session.rollback()
        print("Import failed: %s" % exc, file=sys.stderr)
        return 1
    finally:
        session.close()

    print(report)
    print(module.SOURCE_FIELDS["attribution"])
    return 0


if __name__ == "__main__":
    engine()
    sys.exit(main())
