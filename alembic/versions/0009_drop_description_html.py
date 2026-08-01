"""Drop `jobs.description_html`.

**This deletes data.** It is the first migration here that does, so it is worth
stating plainly rather than leaving in the diff: the stored HTML bodies are
removed and no downgrade can bring them back.

The loss is bounded to formatting. `Job.__post_init__` derives
`description_text` from the HTML *in memory*, before anything is written, so
every row that had HTML already has the text — measured across the archive,
`description_text` is populated on every row where `description_html` was.
Nothing downstream read the HTML: the fingerprint prefers the text, the
dashboard renders the text, and the export's `--include-html` flag existed only
to serve this column and went with it.

The dataclass field survives, because it is the input to that derivation.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    # Report what is being dropped, and prove the text survived it — a row with
    # HTML but no text would be a real loss, and the count says whether any
    # exists before the column is gone.
    row = conn.execute(
        sa.text(
            "SELECT count(*) AS total, "
            "       count(description_html) AS with_html, "
            "       count(description_text) AS with_text, "
            "       sum(CASE WHEN description_html IS NOT NULL "
            "                 AND description_text IS NULL THEN 1 ELSE 0 END) AS html_only "
            "FROM jobs"
        )
    ).mappings().one()
    print(f"description_html: dropping from {row['total']:,} rows")
    print(f"    had html          {row['with_html']:,}")
    print(f"    have text         {row['with_text']:,}")
    print(f"    html but no text  {row['html_only'] or 0:,}   <- what this loses")

    op.drop_column("jobs", "description_html")


def downgrade() -> None:
    """Recreates the column, empty. The content is not recoverable.

    A refetch repopulates `description_text` for any posting still listed; the
    HTML has no consumer left to repopulate.
    """
    op.add_column("jobs", sa.Column("description_html", sa.Text(), nullable=True))
