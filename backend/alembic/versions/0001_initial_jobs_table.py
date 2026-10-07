"""Initial jobs table

Creates the `jobs` table — the central state machine of CSVForge.
Every CSV upload creates one row. Workers update that row through
the processing lifecycle.

Schema reference: design.md §6.1
State machine reference: design.md §6.2

Revision ID: 0001
Revises: None (this is the first migration)
Create Date: 2026-10-07

WHAT IS A MIGRATION FILE?
    A migration file is a Python script that describes one discrete, reversible
    change to the database schema. Each file has two functions:
      - upgrade(): applies the change (create a table, add a column, etc.)
      - downgrade(): reverses the change exactly

    Alembic executes these files in order, tracking which have been applied
    in a special `alembic_version` table it creates in your database.

HOW MIGRATIONS CHAIN TOGETHER:
    The `revision` and `down_revision` variables form a linked list (a DAG —
    directed acyclic graph) of migrations:

        None ← 0001 ← 0002 ← 0003  ...

    - `revision` = this migration's ID ("0001")
    - `down_revision` = the ID of the migration that must come before this one

    `alembic upgrade head` walks this chain from the current applied revision
    to the latest, executing each upgrade() in sequence.

    `alembic downgrade -1` reverses one step — runs the downgrade() of the
    currently applied migration.

    `alembic history` prints the full chain.

THE alembic_version TABLE:
    Alembic creates a single table in your database:

        alembic_version
        ───────────────
        version_num (VARCHAR(32)) — stores the latest applied revision ID

    After running this migration: version_num = "0001"
    After running downgrade -1: the row is deleted (no migrations applied)

    This is how Alembic knows which migrations to run — it compares
    version_num against the revision chain to find the gap.

server_default VS Python default:
    These are two different layers of "default value" that are easy to confuse:

    Python default (default=...)
        Set in the SQLAlchemy model definition.
        Evaluated in Python at INSERT time.
        Used when the ORM creates the INSERT statement.
        Does NOT affect INSERT statements made outside the ORM (raw SQL, psql, etc.).

    server_default (server_default=...)
        Set in the database column definition (DDL).
        Evaluated by PostgreSQL itself, not Python.
        Applies to ALL inserts — ORM, raw SQL, psql, any client.
        For `created_at`, server_default=sa.func.now() means:
            "PostgreSQL fills in NOW() if no value is provided"

    For primary keys and timestamps that should always be set by the database,
    server_default is the correct choice. It makes the column self-managing and
    correct regardless of how the row was inserted.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ── Revision identifiers ──────────────────────────────────────────────────────
# These variables are read by Alembic to build the migration chain.
#
# revision:
#   The unique ID of THIS migration. Alembic uses short revision IDs
#   (often hex hashes like "a1b2c3d4") — here we use "0001" for readability.
#
# down_revision:
#   The ID of the migration that must be applied BEFORE this one.
#   None means "this is the first migration — nothing must precede it."
#   If this were the second migration, down_revision would be "0001".
#
# branch_labels / depends_on:
#   Used for complex multi-branch migration graphs (rare in most applications).
#   None is correct for a simple linear chain.
revision: str = "0001"
down_revision: Union[str, None] = None   # first migration in the chain
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Create the jobs table with all columns, constraints, and indexes.

    WHAT:
        Creates one table, `jobs`, with 17 columns, 3 indexes, and appropriate
        constraints. This is a standard DDL CREATE TABLE statement executed
        through SQLAlchemy's `op` (operations) helper.

    WHY op.create_table() instead of raw SQL?
        The `op` module provides a database-agnostic API. Alembic translates
        `op.create_table()` calls to the correct SQL for the target dialect
        (PostgreSQL, MySQL, SQLite). This means the same migration file can
        theoretically run against any supported database — though in practice
        we use PostgreSQL-specific features (ARRAY, gen_random_uuid()) that
        tie this to PostgreSQL.

    COLUMN DECISIONS:

    id — UUID as VARCHAR(36):
        UUIDs prevent enumeration attacks: if primary keys were sequential
        integers (1, 2, 3, ...), an attacker who finds one job ID can guess
        neighboring job IDs. UUIDs are randomly distributed and unguessable.
        Stored as VARCHAR(36) (the canonical "8-4-4-4-12" text format) rather
        than PostgreSQL's native UUID type for easier portability.

        gen_random_uuid()::text
        ────────────────────────
        gen_random_uuid() is a PostgreSQL built-in that generates a UUID v4.
        ::text is a PostgreSQL CAST operator — it converts the UUID type to a
        text/varchar value. Equivalent to CAST(gen_random_uuid() AS text).
        We need this cast because the column is VARCHAR(36), not UUID.

    status — VARCHAR(32) instead of native PostgreSQL ENUM:
        PostgreSQL supports `CREATE TYPE job_status AS ENUM (...)` for a
        native enum column. This provides type safety at the database level.
        We chose VARCHAR(32) instead for one practical reason:

        Adding a new state to a PostgreSQL native ENUM requires:
            ALTER TYPE job_status ADD VALUE 'new_state';
        In PostgreSQL < 12, this statement acquires an ACCESS EXCLUSIVE LOCK
        on the table, blocking all reads and writes until it completes.
        Even in PostgreSQL 12+, it cannot be done inside a transaction.

        Using VARCHAR(32) means adding a new status is just a zero-downtime
        application change — no DDL, no lock, no migration required.
        The ORM (JobStatus enum in models/job.py) enforces valid values.

    column_names — postgresql.ARRAY(sa.TEXT):
        Stores an ordered list of CSV column headers without a separate table.
        Alternative: a `job_columns` table with (job_id, position, name).
        ARRAY is simpler and works well here because:
          - column lists are always read/written as a whole (never queried individually)
          - no need to join to get column metadata

    TIMESTAMPTZ vs TIMESTAMP:
        sa.TIMESTAMP(timezone=True) maps to PostgreSQL's TIMESTAMPTZ type.
        TIMESTAMPTZ stores the timestamp as UTC internally and converts to
        the session's timezone on read.
        TIMESTAMP (without timezone) stores the raw numbers with no timezone
        context — ambiguous in multi-region deployments (is "2025-01-01 12:00"
        US-East time or EU-West time?).
        Always use TIMESTAMPTZ for application timestamps. See AD-24.

    updated_at — server_default vs onupdate:
        server_default=sa.func.now()  →  PostgreSQL sets NOW() on INSERT
        onupdate=func.now()            →  SQLAlchemy adds `SET updated_at=NOW()`
                                          to every UPDATE statement
        The DDL here only sets server_default. The onupdate behavior is
        configured in the SQLAlchemy model (models/job.py) and lives at the
        ORM layer — not in the database schema. This is intentional:
        migrations define the DDL structure; the ORM layer adds behavior.
        See backend/models/job.py for the full explanation.
    """
    op.create_table(
        "jobs",

        # ── Primary key ───────────────────────────────────────
        # id is a UUID stored as a 36-character string.
        # server_default means PostgreSQL generates the UUID automatically
        # on INSERT — the application never needs to provide one.
        #
        # gen_random_uuid() generates a UUID v4 (random).
        # ::text casts the PostgreSQL-native UUID type to VARCHAR.
        # This is a PostgreSQL CAST expression (equivalent to CAST(... AS text)).
        sa.Column(
            "id",
            sa.VARCHAR(36),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()::text"),
            nullable=False,
            comment="UUID v4, generated by PostgreSQL",
        ),

        # ── Identity ──────────────────────────────────────────
        # user_id is the Cognito `sub` claim — the immutable unique identifier
        # for a Cognito user. It's a UUID (36 chars), but we use 128 chars
        # for forward-compatibility in case the auth system changes.
        sa.Column(
            "user_id",
            sa.VARCHAR(128),
            nullable=False,
            comment="Cognito sub claim — authoritative user identity",
        ),
        # user_email is stored at upload time for admin display and audit logging.
        # It is NOT used for authentication — user_id is the authoritative identity.
        # Storing email here avoids a Cognito API call every time an admin views jobs.
        sa.Column(
            "user_email",
            sa.VARCHAR(255),
            nullable=False,
            comment="Stored at upload time for admin display; not used for auth",
        ),

        # ── File metadata ─────────────────────────────────────
        # original_filename: what the browser told us the file was called.
        # Not trusted for processing (path traversal risk) but safe to display.
        sa.Column(
            "original_filename",
            sa.VARCHAR(255),
            nullable=False,
            comment="Original filename as provided by the browser",
        ),
        # original_s3_key: the S3 object key where the raw uploaded CSV is stored.
        # Format: uploads/{user_id}/{job_id}/original.csv
        # VARCHAR(512) accommodates long key paths.
        sa.Column(
            "original_s3_key",
            sa.VARCHAR(512),
            nullable=False,
            comment="S3 key for the uploaded CSV: uploads/{user_id}/{job_id}/original.csv",
        ),
        # processed_s3_key: null until the job reaches `completed` state.
        # The worker fills this in after processing the CSV.
        sa.Column(
            "processed_s3_key",
            sa.VARCHAR(512),
            nullable=True,
            comment="S3 key for the processed output (null until completed)",
        ),

        # ── State machine ─────────────────────────────────────
        # status tracks where this job is in the processing lifecycle.
        # Valid values: pending_upload → queued → processing → completed
        #                                                     ↘ failed
        # VARCHAR(32) instead of native PostgreSQL ENUM — see the upgrade()
        # docstring for the full explanation of why (ALTER TYPE locking problem).
        # server_default ensures new rows start in pending_upload state even
        # if the application code forgets to set it explicitly.
        sa.Column(
            "status",
            sa.VARCHAR(32),
            nullable=False,
            server_default=sa.text("'pending_upload'"),
            comment="Job state: pending_upload|queued|processing|completed|failed",
        ),
        # failure_reason is null for successful jobs.
        # Set by the worker when it catches an unrecoverable error during processing.
        sa.Column(
            "failure_reason",
            sa.TEXT,
            nullable=True,
            comment="Human-readable failure description (null unless status=failed)",
        ),

        # ── Processing results ────────────────────────────────
        # All result columns are nullable — they are filled in progressively
        # as the job moves through states. They start null and are populated
        # when the worker processes the CSV file.
        sa.Column(
            "file_size_bytes",
            sa.BIGINT,
            nullable=True,
            comment="Actual file size in bytes from S3 HeadObject",
        ),
        # row_count excludes the header row (data rows only).
        sa.Column(
            "row_count",
            sa.INTEGER,
            nullable=True,
            comment="Data rows (excludes header row)",
        ),
        sa.Column(
            "column_count",
            sa.INTEGER,
            nullable=True,
            comment="Number of columns",
        ),
        # column_names uses PostgreSQL's native ARRAY type.
        # This stores an ordered list ["col_a", "col_b", ...] in a single column.
        # Alternative: a separate `job_columns` join table with position and name.
        # ARRAY wins here because column lists are always read/written together
        # and are never queried or joined against individually.
        sa.Column(
            "column_names",
            postgresql.ARRAY(sa.TEXT),
            nullable=True,
            comment="Ordered list of column header names",
        ),
        sa.Column(
            "duplicate_rows_removed",
            sa.INTEGER,
            nullable=True,
            comment="Count of duplicate rows removed during cleaning",
        ),

        # ── Timestamps ────────────────────────────────────────
        # Both timestamps use TIMESTAMP(timezone=True) → PostgreSQL TIMESTAMPTZ.
        # See the upgrade() docstring for why TIMESTAMPTZ is preferred.
        #
        # created_at: set once on INSERT, never modified.
        # server_default=sa.func.now() → PostgreSQL fills in NOW() on INSERT.
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="Row creation timestamp (UTC)",
        ),
        # updated_at: refreshed on every state transition.
        # server_default sets the initial value on INSERT.
        # The ORM's onupdate=func.now() (in models/job.py) adds
        # `SET updated_at=NOW()` to every UPDATE statement the ORM generates.
        #
        # IMPORTANT: onupdate is an ORM concept, NOT a database trigger.
        # The migration only sets server_default. The onupdate behavior
        # is configured in the SQLAlchemy model class, which is why you
        # won't see it in this DDL — it lives at the application layer.
        # If you bypass the ORM and run raw SQL UPDATEs, updated_at will
        # NOT be refreshed automatically.
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            # Note: onupdate is handled by SQLAlchemy ORM at the application layer.
            # This column has server_default for the initial INSERT value.
            # The ORM's onupdate=func.now() adds updated_at=NOW() to every
            # UPDATE statement. See backend/models/job.py.
            comment="Last modification timestamp (UTC) — updated on every state transition",
        ),
    )

    # ── Indexes ───────────────────────────────────────────────────────────────
    # PostgreSQL uses B-tree indexes by default. A B-tree index is a balanced
    # tree structure where each leaf node stores a column value and a pointer
    # to the corresponding row in the table. This allows PostgreSQL to:
    #   - Find rows with a specific value in O(log N) time (vs O(N) full scan)
    #   - Efficiently sort results in index order (ORDER BY without sort step)
    #   - Efficiently satisfy range queries (BETWEEN, <, >)
    #
    # WITHOUT an index: PostgreSQL reads every row in the table (sequential scan).
    # WITH an index: PostgreSQL jumps directly to the matching rows (index scan).
    #
    # Three separate indexes for the three independent query patterns:
    #
    # idx_jobs_user_id:
    #   Used by: GET /jobs (list all jobs for the authenticated user)
    #   Query:   WHERE user_id = $1
    #   Why this column: user_id is the primary filter for all user-facing queries.
    #   Without it: a table scan through ALL jobs to find one user's jobs.
    #
    # idx_jobs_status:
    #   Used by: admin GET /admin/jobs?status=failed
    #   Query:   WHERE status = $1
    #   Why this column: admin monitoring filters by status across all users.
    #
    # idx_jobs_created_at:
    #   Used by: default newest-first ordering in list views
    #   Query:   ORDER BY created_at DESC
    #   Why this column: without it, PostgreSQL must sort all rows on every query.
    #
    # Why NOT a composite (user_id, status) index?
    #   A composite index only helps queries that filter on BOTH columns together.
    #   User listing queries: WHERE user_id = $1           (user_id only)
    #   Admin queries:         WHERE status = $1            (status only)
    #   Separate indexes serve each pattern independently.
    #   A composite index would help neither pattern as efficiently.
    op.create_index("idx_jobs_user_id", "jobs", ["user_id"])
    op.create_index("idx_jobs_status", "jobs", ["status"])
    op.create_index("idx_jobs_created_at", "jobs", ["created_at"])


def downgrade() -> None:
    """
    Drop the jobs table and its indexes — the exact inverse of upgrade().

    WHAT:
        Removes everything that upgrade() created, in reverse order.
        Alembic runs this when you execute `alembic downgrade -1`.

    WHY must downgrade() be the exact inverse?
        The migration chain must be fully reversible. If upgrade() creates a
        table and downgrade() does something different (or nothing), you cannot
        safely roll back. In production, the ability to downgrade is a safety net:
        if a new deployment has a bug, you can roll back the code AND the schema
        in one operation.

    INDEX CLEANUP:
        PostgreSQL drops indexes automatically when a table is dropped. Explicit
        op.drop_index() calls are included here for clarity and for protection
        against partial failure scenarios (e.g., if drop_table() were called
        before all indexes existed, making the state recoverable).

    ANALOGY:
        upgrade() is "build the house."
        downgrade() is "demolish the house, clean the lot, restore it to the state
        before construction started."
        If the demolition crew does something different from the reverse of
        the construction, the lot is in an unknown state.
    """
    # Drop indexes first — technically redundant (drop_table cascades)
    # but explicit for clarity and partial-failure safety.
    op.drop_index("idx_jobs_created_at", table_name="jobs")
    op.drop_index("idx_jobs_status", table_name="jobs")
    op.drop_index("idx_jobs_user_id", table_name="jobs")
    op.drop_table("jobs")
