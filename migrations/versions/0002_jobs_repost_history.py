"""add jobs repost history columns

Revision ID: 0002_jobs_repost_history
Revises: 0001_widen_jobs_text
Create Date: 2026-09-13

Adds repost_count, last_reposted_at and days_unlisted, which record a listing
that expires and later returns under the same external id.

Two are NOT NULL on a table with tens of thousands of rows. That is safe here
only because each carries a DEFAULT: Postgres 11+ stores the default in the
catalogue and does not rewrite the table. ADD COLUMN ... NOT NULL *without* a
default would fail outright on a populated table.

The existing column set is read first, so this is safe on a database where
create_all has already produced these columns — which is every fresh database
and both test suites.
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_jobs_repost_history"
down_revision = "0001_widen_jobs_text"
branch_labels = None
depends_on = None

_NEW = {
    "repost_count": sa.Column(
        "repost_count", sa.Integer(), nullable=False, server_default="0"
    ),
    "last_reposted_at": sa.Column(
        "last_reposted_at", sa.DateTime(timezone=True), nullable=True
    ),
    "days_unlisted": sa.Column(
        "days_unlisted", sa.Float(), nullable=False, server_default="0"
    ),
}


def _existing() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("jobs")}


def upgrade() -> None:
    have = _existing()
    for name, column in _NEW.items():
        if name not in have:
            op.add_column("jobs", column)


def downgrade() -> None:
    have = _existing()
    for name in reversed(list(_NEW)):
        if name in have:
            op.drop_column("jobs", name)
