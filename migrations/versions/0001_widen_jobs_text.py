"""widen drifted jobs text columns

Revision ID: 0001_widen_jobs_text
Revises:
Create Date: 2026-09-13

The Job model declares these columns as Text, but any database created before
that change still held bounded varchars. init_db() only ever added columns, so
the widening never reached production: MongoDB failed every scrape with
StringDataRightTruncation on varchar(120), and because rows insert as a batch it
took that company's entire run with it.

varchar -> text is a catalogue-only change in Postgres, so there is no table
rewrite. The current type is checked first, which makes this safe to run against
a database that is already correct — including a fresh one, where create_all has
just built every column as TEXT and this becomes a no-op.

There is no meaningful downgrade. Narrowing back to varchar(120) would fail on
any row that has since exceeded it, which is the entire reason this exists.
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_widen_jobs_text"
down_revision = None
branch_labels = None
depends_on = None

_COLUMNS = (
    "title", "normalized_title", "location_raw", "location_country",
    "location_city", "description_raw", "description_summary",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return   # SQLite does not enforce declared widths

    narrow = {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'jobs' AND data_type <> 'text' "
                "AND column_name = ANY(:cols)"
            ),
            {"cols": list(_COLUMNS)},
        )
    }
    for column in narrow:
        op.alter_column("jobs", column, type_=sa.Text(), existing_nullable=True)


def downgrade() -> None:
    raise NotImplementedError(
        "Narrowing these columns would fail on rows that already exceed the old "
        "limit, which is the defect this migration fixed."
    )
