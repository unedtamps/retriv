"""Grant the reader role on every run, so a role created late is not left without access.

``0002_csv_rows`` and ``0003_row_catalogue`` both grant to ``retriv_reader``
only if it already exists. That guard is correct and deliberate: a role's
password must never be committed to a migration, so the role is created
operally, and a GRANT against a role that was never created aborts the whole
upgrade.

The consequence was a deployment that reported success and could not start.
The role was missing, both migrations skipped their GRANT, and the app then
refused the connection with a message naming the DSN rather than the step that
had been skipped. Anything that creates the role after the fact -- a restore,
a new volume, someone following the setup by hand -- was left with a role that
could log in and read nothing, and no signal that the grants had never been
applied.

This revision does not fix the ordering by itself, because it inherits the
guard from the revisions above. It records the intent in one place instead of
three, and ``postgres-init`` applies the same grants operationally, so the two
paths converge whichever order they run in: this revision covers the tables it
finds for a role that already exists, and the init service covers a role that
appears after ``alembic upgrade head`` has already run past here.

Kept separate from ``0002``/``0003`` for the same reason as ``0003`` was kept
separate from ``0002``: both are already applied to running environments, and
editing them would not replay there.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_reader_grants"
down_revision: str | None = "0006_row_document_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

READER_ROLE = "retriv_reader"

# document_rows and documents: the tables a query may name. processing_runs is
# not in the model's allowlist, but the wrapper reads it to learn a file's
# column names, and a granted table the model cannot spell is narrower than one
# it could.
GRANTED_TABLES = ("document_rows", "documents", "processing_runs")


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{READER_ROLE}') THEN
                RETURN;
            END IF;
            GRANT USAGE ON SCHEMA public TO {READER_ROLE};
            GRANT SELECT ON {', '.join(GRANTED_TABLES)} TO {READER_ROLE};
        END $$
        """  # noqa: S608 - interpolates READER_ROLE and GRANTED_TABLES only, never input
    )


def downgrade() -> None:
    # REVOKE rather than DROP: the role and its access to everything else
    # belong to the deployment, not to this revision. Revoking only what this
    # revision grants keeps a downgrade from becoming a way to remove access
    # this revision never provided.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{READER_ROLE}') THEN
                REVOKE SELECT ON {', '.join(GRANTED_TABLES)} FROM {READER_ROLE};
            END IF;
        END $$
        """  # noqa: S608 - interpolates READER_ROLE and GRANTED_TABLES only, never input
    )