"""full-text search on resources (PostgreSQL)

Revision ID: b3f1c9d2a7e4
Revises: 7a64ad26d3b9
Create Date: 2026-10-30 20:00:00

A generated tsvector column and a GIN index over it. PostgreSQL only:
SQLite has no tsvector, and the application falls back to ILIKE there.
The SQL is repeated here rather than imported from the models, because a
migration must keep meaning what it meant when it was written.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'b3f1c9d2a7e4'
down_revision: Union[str, Sequence[str], None] = '7a64ad26d3b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _postgresql():
        return
    # Adding a STORED generated column computes it for every existing row,
    # so on a large table this rewrites the table once.
    op.execute(
        "ALTER TABLE resources ADD COLUMN search_vector tsvector "
        "GENERATED ALWAYS AS ("
        "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
        "setweight(to_tsvector('english', coalesce(authors, '')), 'B') || "
        "setweight(to_tsvector('english', coalesce(description, '')), 'C')"
        ") STORED")
    op.execute("CREATE INDEX ix_resources_search ON resources "
               "USING gin (search_vector)")


def downgrade() -> None:
    if not _postgresql():
        return
    op.execute("DROP INDEX IF EXISTS ix_resources_search")
    op.execute("ALTER TABLE resources DROP COLUMN IF EXISTS search_vector")
