"""
CSVForge — Job SQLAlchemy ORM model.

WHAT:
    This module defines:
      - JobStatus: Python enum for the five valid job states.
      - Job: SQLAlchemy ORM class that maps to the `jobs` table in PostgreSQL.

WHY:
    The `jobs` table is the central state machine of CSVForge.
    Every file upload creates one row.  Every status transition updates that
    row.  The worker reads it, claims ownership atomically, and writes the
    processed result back.

    Defining the table structure as a Python class (rather than raw SQL) lets
    us:
      - Write type-annotated Python code that references column values.
      - Let Alembic compare Base.metadata with the live DB schema to generate
        migration files automatically.
      - Use SQLAlchemy's ORM features (session tracking, relationships, etc.).

ARCHITECTURE:
    backend/db/connection.py  defines Base (DeclarativeBase)
          ↓ (Job inherits from Base)
    backend/models/job.py     defines Job, JobStatus
          ↓ (imported by)
    backend/db/job_repository.py  issues SELECT/UPDATE/INSERT using Job
          ↓ (called by)
    backend/main.py / worker  drive the state machine

    Alembic's env.py also imports Job to ensure the table is registered
    in Base.metadata before Alembic compares it to the live schema.

ANALOGY:
    An ORM (Object-Relational Mapper) lets you work with the database using
    Python objects instead of raw SQL.  A SQLAlchemy model class IS the table
    definition — the class maps to a table, each instance maps to one row,
    and each class attribute maps to a column.

    Think of the Job class as a row template:
    - The class definition (`class Job(Base)`) is like the column headers
      on a spreadsheet: it names and types every field.
    - Each Job instance (e.g., `job = Job(id=..., status=...)`) is one row
      in that spreadsheet — a concrete record for one upload.
    - SQLAlchemy maps between the Python world (objects with attributes)
      and the SQL world (tables with columns) automatically.

    SQL equivalents for common ORM operations:
      `session.add(job)`
          → queues:  INSERT INTO jobs (id, status, ...) VALUES (...) [on flush/commit]
      `session.query(Job).filter(Job.user_id == uid).all()`
          → executes: SELECT * FROM jobs WHERE user_id = '<uid>'
      `job.status = JobStatus.QUEUED; session.commit()`
          → executes: UPDATE jobs SET status='queued' WHERE id='<id>'
      `session.delete(job)`
          → executes: DELETE FROM jobs WHERE id='<id>'  [on flush/commit]

IMPORTANT CONCEPTS:
    - ORM (Object-Relational Mapper): translates Python object operations
      (job.status = "queued") into SQL statements (UPDATE jobs SET status='queued').
    - Mapped[T] / mapped_column(): SQLAlchemy 2.0 typed mapping syntax.
      The older SQLAlchemy 1.x syntax used `Column(VARCHAR(32))` directly
      as class attributes.  The new syntax uses `mapped_column(VARCHAR(32))`
      with a `Mapped[str]` type annotation, which gives better IDE support
      and runtime type checking.  mypy/pyright can now infer that `job.id`
      is `str`, not `Any`.
    - server_default: a value computed by the DATABASE at INSERT time.
    - default: a value computed by PYTHON at INSERT time.
    - onupdate: a value computed by Python's ORM layer on every UPDATE.
    - index: a B-tree data structure that speeds up WHERE/ORDER BY queries
      at the cost of extra write overhead and storage.

Design reference: §6.1 (single jobs table), §6.2 (state machine)
"""

import enum

from sqlalchemy import (
    BIGINT,
    INTEGER,
    TEXT,
    VARCHAR,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from backend.db.connection import Base


# ── Job status enum ───────────────────────────────────────────────────────────

class JobStatus(str, enum.Enum):
    """
    The five states a Job can be in during its lifecycle.

    WHAT:
        An enum that defines every legal value for the `status` column.
        Using an enum prevents typos like `job.status = "compleeted"` and
        makes valid states discoverable through IDE autocomplete.

    WHY `str` mixin:
        `class JobStatus(str, enum.Enum)` means each member IS a string.
        For example: `JobStatus.QUEUED == "queued"` is True.

        Without the `str` mixin:
            JobStatus.PENDING_UPLOAD == 'pending_upload'   → False
            JobStatus.PENDING_UPLOAD.value == 'pending_upload'  → True (must use .value)

        With the `str` mixin:
            JobStatus.PENDING_UPLOAD == 'pending_upload'   → True  (works directly)

        This matters most when the ORM reads a status value back from the
        database.  PostgreSQL stores the status column as VARCHAR(32) —
        a plain string like 'pending_upload'.  When SQLAlchemy loads a Job
        row, `job.status` may be the raw string `'pending_upload'` rather
        than `JobStatus.PENDING_UPLOAD`.  With the `str` mixin:
            job.status == JobStatus.PENDING_UPLOAD   → True
            (even if job.status is the raw string, not the enum member)
        Without the mixin, this comparison would be False, causing subtle bugs
        in conditional logic like `if job.status == JobStatus.PENDING_UPLOAD`.

        The `str` mixin also means the enum serializes naturally to a string,
        which is important for JSON API responses (Pydantic serializes enum
        members to their string values automatically when `str` is a base class).

        SQLAlchemy stores the column as VARCHAR(32), so it needs a plain
        string.  The `str` mixin means we can pass JobStatus.QUEUED directly
        to `.values(status=JobStatus.QUEUED)` — no `.value` needed.

        In this codebase you will see `.value` used explicitly in some places
        (e.g., `status=JobStatus.PENDING_UPLOAD.value`) — both forms work
        because of the `str` mixin.

    State machine (see design §6.2):

        pending_upload          (internal only — never shown to users)
               │
               │  POST /uploads/{id}/complete  (S3 confirmed upload)
               ▼
            queued              ← first user-visible state
               │
               │  Worker: claim_job_for_processing() conditional UPDATE
               ▼
          processing
               │
        ┌──────┴──────┐
        │             │
        ▼             ▼
    completed       failed

    State descriptions:
        pending_upload: upload intent created; S3 upload not yet confirmed.
                        Hidden from users (list_jobs_for_user excludes it).
        queued:         S3 upload confirmed; SQS message published; worker
                        hasn't claimed the job yet.
        processing:     worker claimed the job via atomic conditional UPDATE
                        and is actively running CSV analysis.
        completed:      worker finished; metadata written; processed file in S3.
        failed:         permanent error (invalid CSV, unrecoverable processing
                        failure).  The SQS message is deleted (not retried).

    Terminal states:
        completed and failed are terminal — no transitions out of them.
        The worker checks for this before claim_job_for_processing().

    Interview concept:
        "What is a finite state machine?"
        A system with a fixed set of states where transitions between states
        are explicitly defined and controlled.  Representing job lifecycle as
        a state machine (rather than a boolean `is_processed` flag) makes
        invalid transitions impossible and makes the system's behavior
        predictable — you always know what a job IS doing, not just what
        it has done.
    """

    PENDING_UPLOAD = "pending_upload"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


# ── Job ORM model ─────────────────────────────────────────────────────────────

class Job(Base):
    """
    SQLAlchemy ORM model for the `jobs` table.

    WHAT:
        Defines the 15-column schema for the `jobs` table and maps each column
        to a typed Python attribute.  Every ORM operation on the jobs table
        (SELECT, INSERT, UPDATE) goes through this class definition.

    WHY:
        ORM models let us work with rows as Python objects rather than raw
        dictionaries.  They also give Alembic enough information to
        auto-generate CREATE TABLE / ALTER TABLE migrations.

    HOW the ORM mapping works:
        1. `class Job(Base)` registers Job in `Base.metadata` at class
           definition time (when this module is first imported).
        2. `__tablename__ = "jobs"` tells SQLAlchemy which table this class
           maps to.  Without this, SQLAlchemy would use the class name lowercase.
        3. Each `mapped_column(...)` attribute corresponds to one column.
           SQLAlchemy generates the correct SQL type (e.g., VARCHAR(32)) from
           the mapped_column argument.
        4. When you write `job = Job(id=..., status=...)`, SQLAlchemy creates
           a Python object.  The INSERT does not happen until `db.add(job)`
           followed by `db.flush()` or `db.commit()`.

    Columns (15 total):
        id, user_id, user_email, original_filename, original_s3_key,
        processed_s3_key, status, failure_reason, file_size_bytes,
        row_count, column_count, column_names, duplicate_rows_removed,
        created_at, updated_at

    Indexes:
        idx_jobs_user_id    — speeds up "list all jobs for a user" query
        idx_jobs_status     — speeds up admin "filter by status" query
        idx_jobs_created_at — speeds up "order by newest first" query

    Interview concept:
        "What is an ORM?"
        An Object-Relational Mapper translates between the object-oriented
        world (Python classes and instances) and the relational world (SQL
        tables and rows).  Benefits: less boilerplate SQL, type safety, easier
        testing.  Drawbacks: ORM-generated SQL can be inefficient, and ORMs
        add a layer of abstraction that can hide performance problems.
    """

    __tablename__ = "jobs"

    # ── Primary key ───────────────────────────────────────────────────────────
    # Python type: str  (stored as VARCHAR(36) in PostgreSQL — the UUID format
    #                    xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx is 36 chars)
    # SQL type:    VARCHAR(36)
    # Nullable:    No (primary keys are always NOT NULL)
    # Default:     Generated in Python via uuid.uuid4() in create_job()
    # Server default: gen_random_uuid()::text in the Alembic migration
    #                 (fallback for raw SQL inserts that bypass the ORM)
    #
    # SQLAlchemy 2.0 typed mapping — Mapped[str] + mapped_column():
    #   The pre-2.0 (SQLAlchemy 1.x) syntax was:
    #       id = Column(String(36), primary_key=True)
    #   There was no type annotation — your IDE treated `job.id` as `Any`.
    #
    #   The SQLAlchemy 2.0 syntax is:
    #       id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #   `Mapped[str]` is the PYTHON-level type annotation — mypy/pyright
    #   knows that `job.id` is a `str`, not `Any`, so it can catch type errors.
    #   `mapped_column(String(36), primary_key=True)` is the DATABASE-level
    #   descriptor — it tells SQLAlchemy the SQL type, nullability, constraints,
    #   and defaults.  They work together:
    #     - `Mapped[str]`          → Python sees a str
    #     - `mapped_column(VARCHAR(36))` → DB sees VARCHAR(36)
    #     - `Mapped[str | None]`   → Python sees Optional[str]; DB sees nullable
    #
    # WHY UUID instead of a sequential integer (SERIAL / BIGSERIAL)?
    #   1. Privacy: sequential IDs let users enumerate resources.
    #      With UUID, knowing job ID "abc-123" gives you no information
    #      about whether "abc-124" exists or belongs to you.
    #   2. Distributed generation: UUIDs can be generated in Python (or any
    #      service) without asking the database for the next sequence value.
    #      This avoids a round-trip and enables offline ID generation.
    #   3. Merge safety: if you ever merge two databases, UUID primary keys
    #      won't collide; sequential integers almost certainly will.
    #   Downside: 36-character strings use more storage than a 4-byte integer,
    #   and random UUID ordering causes new rows to scatter across the B-tree
    #   index (page splits) rather than always appending to the end.
    #
    # WHY generate in Python rather than relying on server_default?
    #   We need the ID immediately after creating the Python object (before
    #   the INSERT is sent) so we can construct the S3 key
    #   (`uploads/{user_id}/{job_id}/original.csv`) in the same request.
    #   Server defaults are only visible after the INSERT round-trip.
    #
    # Interview concept:
    #   "UUID vs sequential integer primary key trade-offs?"
    #   Sequential: smaller (8 bytes vs 16 bytes UUID), better B-tree locality
    #   (new rows always at the end of the index → fewer index page splits),
    #   human-readable.  UUID: non-enumerable, globally unique, no DB round-trip
    #   for generation.  For user-facing IDs at the scale of CSVForge, UUID
    #   is the right choice.
    id: Mapped[str] = mapped_column(
        VARCHAR(36),
        primary_key=True,
        comment="UUID v4 primary key, generated in Python (see create_job) or by PostgreSQL server_default in the migration",
    )

    # ── Identity ──────────────────────────────────────────────────────────────
    # user_id = Cognito `sub` claim (UUID-format string, up to 128 chars).
    # This is the authoritative identity field.
    #
    # WHY Cognito sub rather than email?
    #   Email can change.  The Cognito `sub` claim is an immutable UUID
    #   assigned at account creation time.  If a user changes their email,
    #   their `sub` stays the same, so all their jobs remain accessible. AD-23.
    user_id: Mapped[str] = mapped_column(
        VARCHAR(128),
        nullable=False,
        comment="Cognito sub claim — authoritative user identity",
    )

    # user_email is stored only for admin display purposes.
    # The authorization layer NEVER uses email to gate access (AD-23).
    # Email is captured at upload time; if the user changes their email
    # later, existing jobs will still show the old email — acceptable
    # because this field is informational only.
    user_email: Mapped[str] = mapped_column(
        VARCHAR(255),
        nullable=False,
        comment="Stored at upload time for admin display only — not used for auth (AD-23)",
    )

    # ── File metadata ──────────────────────────────────────────────────────────
    # original_filename: what the user's browser reported.
    # This is user-supplied data and should never be used to construct
    # file system paths without sanitisation.  We store it only for display.
    original_filename: Mapped[str] = mapped_column(
        VARCHAR(255),
        nullable=False,
        comment="Original filename as provided by the browser",
    )

    # S3 key for the raw uploaded file.
    # Format: uploads/{user_id}/{job_id}/original.csv
    # This key is deterministic: given a job record, you always know where
    # the original file lives.  The worker uses this key to read the file.
    original_s3_key: Mapped[str] = mapped_column(
        VARCHAR(512),
        nullable=False,
        comment="S3 key for the uploaded CSV: uploads/{user_id}/{job_id}/original.csv",
    )

    # S3 key for the processed output file.
    # NULL until the worker successfully completes processing.
    # Format: processed/{user_id}/{job_id}/result.csv
    # The frontend uses this key (via a presigned URL) to offer the
    # download link.
    processed_s3_key: Mapped[str | None] = mapped_column(
        VARCHAR(512),
        nullable=True,  # NULL until worker completes
        comment="S3 key for the processed output: processed/{user_id}/{job_id}/result.csv",
    )

    # ── State machine ──────────────────────────────────────────────────────────
    # WHY VARCHAR(32) instead of a PostgreSQL native ENUM type?
    #   PostgreSQL ENUMs are stored as integers internally and require ALTER TYPE
    #   to add new values.  ALTER TYPE on an ENUM acquires an ACCESS EXCLUSIVE
    #   lock on the table — no reads or writes during the migration.  For a
    #   high-traffic jobs table, this is unacceptable.
    #   VARCHAR(32) allows adding new status values with a zero-downtime migration
    #   (just deploy new code; no ALTER TABLE needed).
    #
    # The ORM always sets status explicitly (via JobStatus.X.value).
    # The server_default in the migration ("pending_upload") is a safety net
    # for direct SQL inserts that bypass the ORM.
    status: Mapped[str] = mapped_column(
        VARCHAR(32),
        nullable=False,
        comment="Current job state: pending_upload | queued | processing | completed | failed",
    )

    # Human-readable description of why the job failed.
    # NULL for all non-failed jobs.  Set by fail_job() in job_repository.py.
    failure_reason: Mapped[str | None] = mapped_column(
        TEXT,
        nullable=True,
        comment="Human-readable failure description, set when status=failed",
    )

    # ── Processing results (populated by worker on completion) ─────────────────
    # All four result columns are NULL until the job reaches `completed`.
    # They are written in a single UPDATE by complete_job().

    # BIGINT because files can theoretically be up to 2^63 bytes.
    # In practice uploads are capped at a smaller limit, but BIGINT is
    # the right choice for byte counts to avoid INT overflow on large files.
    file_size_bytes: Mapped[int | None] = mapped_column(
        BIGINT,
        nullable=True,
        comment="Actual file size in bytes, verified via S3 HeadObject (AD-27)",
    )
    row_count: Mapped[int | None] = mapped_column(
        INTEGER,
        nullable=True,
        comment="Number of data rows (not counting header)",
    )
    column_count: Mapped[int | None] = mapped_column(
        INTEGER,
        nullable=True,
        comment="Number of columns",
    )

    # column_names stores the list of CSV header column names.
    # PostgreSQL can store this natively as ARRAY(TEXT) — a variable-length
    # array of text values in a single column, without needing a separate
    # join table.
    #
    # WHY ARRAY(TEXT).with_variant(TEXT, "sqlite") — full explanation:
    #
    #   1. PostgreSQL supports the ARRAY type natively.
    #      `errors TEXT[]` (or `ARRAY(TEXT)` in SQLAlchemy) stores a list of
    #      strings in one column.  You can query it with array operators:
    #        SELECT * FROM jobs WHERE 'col_name' = ANY(column_names)
    #      This is efficient and avoids the overhead of a join table.
    #
    #   2. SQLite (used in unit tests) does NOT support ARRAY.
    #      If SQLAlchemy tried to CREATE TABLE with ARRAY(TEXT) on SQLite, it
    #      would raise an error because SQLite has no equivalent type.
    #
    #   3. `.with_variant(TEXT, "sqlite")` tells SQLAlchemy:
    #      "When the database dialect is SQLite, use TEXT instead of ARRAY(TEXT)."
    #      This means:
    #        - Unit tests (SQLite): column_names is a plain TEXT column.
    #          SQLAlchemy's type coercion will serialize the Python list to/from
    #          a text representation automatically.
    #        - Production (PostgreSQL): column_names is a real ARRAY(TEXT) column.
    #          `.with_variant()` has zero effect on the PostgreSQL schema.
    #
    #   4. The trade-off: tests don't exercise ARRAY storage.
    #      Unit tests with SQLite can't test PostgreSQL-specific ARRAY behavior
    #      (array operators, indexing, ANY() queries).  That's what integration
    #      tests (against real PostgreSQL) are for.
    #      See `tests/integration/test_job_repository_integration.py` for the
    #      ARRAY-specific tests that only run against PostgreSQL.
    #
    # Interview concept:
    #   "Why not store column names in a separate junction table?"
    #   A junction table (job_columns with job_id FK) would be more relational
    #   but adds JOIN overhead.  The column_names array is read-only after
    #   completion and accessed in its entirety every time — ARRAY avoids the
    #   join.  PostgreSQL's ARRAY type is well-suited for this small, ordered,
    #   read-only list.
    column_names: Mapped[list[str] | None] = mapped_column(
        ARRAY(TEXT).with_variant(TEXT, "sqlite"),
        nullable=True,
        comment="Column names from the header row",
    )

    duplicate_rows_removed: Mapped[int | None] = mapped_column(
        INTEGER,
        nullable=True,
        comment="Count of duplicate rows removed during cleaning",
    )

    # ── Timestamps ─────────────────────────────────────────────────────────────
    # WHY TIMESTAMP(timezone=True) (TIMESTAMPTZ in PostgreSQL)?
    #   TIMESTAMP stores a "wall clock" time with no timezone information.
    #   If your server is in UTC and you store "2024-01-15 10:00:00", then
    #   move the server to EST, the stored value now represents a different
    #   instant in real time (5 hours off).
    #
    #   TIMESTAMPTZ stores the instant in time as UTC internally.  When you
    #   read it back, PostgreSQL converts it to the session's timezone.
    #   Since we always use UTC everywhere, TIMESTAMPTZ is always correct
    #   regardless of server/database timezone settings.
    #
    # Interview concept:
    #   "TIMESTAMP vs TIMESTAMPTZ?"
    #   TIMESTAMP: stores date+time as-is, no timezone awareness.  Risky when
    #   servers or databases operate in different timezones.
    #   TIMESTAMPTZ (or TIMESTAMP WITH TIME ZONE): stores as UTC internally,
    #   returns in session timezone.  Always use TIMESTAMPTZ for production.
    #
    # Three default mechanisms — how values get set on INSERT and UPDATE:
    #
    #   1. server_default=text("gen_random_uuid()::text")  [used for id]
    #      The DATABASE computes the value when INSERT is executed.
    #      SQLAlchemy sends no value for this column in the INSERT statement.
    #      The Python object has `None` for this attribute until after
    #      db.flush() + db.refresh(), at which point the DB-generated value
    #      is retrieved and set on the Python object.
    #      Used for `id` because we want the DB to generate IDs consistently
    #      with the migration (though in practice Python also generates them).
    #
    #   2. server_default=text("CURRENT_TIMESTAMP")  [used for created_at, updated_at default]
    #      Same mechanism: the DB generates the timestamp at INSERT time.
    #      `CURRENT_TIMESTAMP` is ANSI SQL and works on both PostgreSQL and
    #      SQLite (unit tests).  The migration uses `sa.func.now()` which
    #      emits `NOW()` — PostgreSQL-specific but functionally equivalent.
    #
    #   3. onupdate=func.now()  [used for updated_at]
    #      SQLAlchemy adds `SET updated_at = now()` to every UPDATE statement
    #      for this row automatically.  This is a Python/ORM layer mechanism —
    #      SQLAlchemy modifies the SQL it generates.  It is NOT a PostgreSQL
    #      trigger; it only fires for ORM-mediated UPDATEs.
    #      The migration cannot replicate this behavior because `onupdate` is
    #      not DDL (Data Definition Language) — it has no SQL equivalent that
    #      can be expressed in a CREATE TABLE statement.  It's purely ORM behavior.
    #
    #   Summary table:
    #     Mechanism         | When it fires       | Who computes value | In migration?
    #     ------------------|---------------------|--------------------|---------------
    #     server_default    | INSERT              | Database           | Yes (DDL)
    #     Python default    | Python object creation | Python          | No
    #     onupdate          | ORM UPDATE          | Python/ORM         | No (ORM only)

    # A local import here keeps the `TIMESTAMP` name out of the module's top-level
    # namespace (it would shadow any other `TIMESTAMP` import or variable name).
    # The `from sqlalchemy import TIMESTAMP` in the class body is visible
    # throughout the class definition.  This is a stylistic choice in the
    # original implementation — not a Python requirement.
    from sqlalchemy import TIMESTAMP  # local import to keep namespace clean

    # created_at: the moment the Job row was inserted.
    # Set ONCE at INSERT time via server_default and never changed.
    # `server_default=text("CURRENT_TIMESTAMP")` means the DATABASE computes
    # this value at INSERT time.  CURRENT_TIMESTAMP is ANSI SQL, supported by
    # both PostgreSQL ("now() as TIMESTAMPTZ") and SQLite (unit tests).
    # The Alembic migration uses `sa.func.now()` which emits `NOW()` in
    # PostgreSQL DDL — functionally equivalent but PostgreSQL-specific.
    #
    # Distinction: server_default vs Python default
    #   server_default: the database computes the value during SQL execution.
    #                   The Python object has `None` until after db.flush()
    #                   returns (since the INSERT SQL has run by then, and
    #                   db.refresh() retrieves the server-computed value).
    #   default:        Python computes the value before sending SQL.
    #                   The Python object has the value immediately.
    created_at: Mapped[object] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        comment="Row creation time (UTC)",
    )

    # updated_at: the moment the row was last modified.
    # Set at INSERT (server_default) AND updated on every subsequent UPDATE
    # via `onupdate=func.now()`.
    #
    # HOW `onupdate` works:
    #   When SQLAlchemy executes an UPDATE statement for a Job object through
    #   the ORM, it automatically adds `updated_at = NOW()` to the SET clause
    #   of the UPDATE.  This happens at the Python/ORM layer — it's NOT a
    #   PostgreSQL trigger.  It only fires for ORM-mediated UPDATEs; direct
    #   SQL `UPDATE jobs SET status=...` via `db.execute(text(...))` would NOT
    #   update this column automatically.
    #
    # WHY onupdate instead of a PostgreSQL trigger?
    #   Triggers are database-level code that must be included in migrations.
    #   `onupdate=func.now()` is a Python-level convenience that works for
    #   the ORM-based updates this application uses.  The Alembic migration
    #   does not define a trigger for updated_at.
    #
    # IMPORTANT: for the bulk UPDATE statements in job_repository.py that use
    #   `update(Job).values(...)`, onupdate does NOT fire because those are
    #   Core UPDATE statements (not ORM object mutation).  In this codebase,
    #   the worker explicitly includes `updated_at = func.now()` in the VALUES
    #   clause of such bulk updates (see complete_job, fail_job) to ensure the
    #   column is kept accurate.
    updated_at: Mapped[object] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=func.now(),
        comment="Last modification time (UTC) — updated on every status transition",
    )

    # ── Table-level indexes ────────────────────────────────────────────────────
    # B-tree indexes allow PostgreSQL to find matching rows in O(log n) time
    # instead of O(n) (a full table scan).  The trade-off is extra storage
    # and slightly slower INSERT/UPDATE (the index must be kept in sync).
    #
    # Three separate single-column indexes (not a composite) because each
    # query filters on exactly one column at a time:
    #   - GET /jobs?user_id=X      → WHERE user_id = X
    #   - GET /admin/jobs?status=Y → WHERE status = Y
    #   - ORDER BY created_at DESC → needs created_at to be indexed
    #
    # Composite indexes (e.g., user_id + created_at) would only help queries
    # that filter on BOTH columns simultaneously.  We don't have that access
    # pattern in Day 1.
    #
    # Defined here (in `__table_args__`) so Alembic picks them up automatically
    # from Base.metadata when generating migration files.
    __table_args__ = (
        Index("idx_jobs_user_id", "user_id"),
        Index("idx_jobs_status", "status"),
        Index("idx_jobs_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        """
        Return a developer-friendly string representation of the Job.

        WHAT:
            Called by Python when you print() a Job object or inspect it in
            a REPL / debugger.  Example output:
                <Job id='abc-123' status='queued' user_id='cognito-sub-xyz'>

        WHY:
            The default `__repr__` from Python shows `<backend.models.job.Job
            object at 0x7f...>` — the memory address — which is useless for
            debugging.  A custom `__repr__` shows the fields that uniquely
            identify the record.

        SIDE EFFECTS:
            None.  `__repr__` should never trigger database queries.
            (Accessing an expired attribute would issue a SELECT.  Since
            we use expire_on_commit=False, this is safe, but it's still
            good practice to only reference attributes that are always loaded.)
        """
        return (
            f"<Job id={self.id!r} status={self.status!r} user_id={self.user_id!r}>"
        )
