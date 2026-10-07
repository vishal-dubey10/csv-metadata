"""
Unit tests for the Job model and JobStatus enum.

These tests verify the structure of the model — column count, enum values,
constraints — without connecting to any database. They are pure Python
introspection tests.

Why test model structure?
- A missing column is caught before it causes a runtime error in a route.
- If someone adds a column and forgets to update the migration, these tests
  catch the discrepancy (the model has 16 columns but the migration creates 15).
- Enums are the documented API contract: the exact string values must match
  what the database stores.

HOW SQLAlchemy INTROSPECTION WORKS:
`sa_inspect(Job)` returns a SQLAlchemy Mapper object that exposes the ORM
mapping for the `Job` class — its mapped columns, relationships, primary keys,
etc. This is SQLAlchemy's introspection API: instead of reading the `Job` class
definition manually, the test uses the SQLAlchemy API to verify that the
mapping was set up correctly. Useful for verifying that `mapped_column()` was
called with the right arguments.

`Job.__table__` accesses the underlying SQLAlchemy `Table` object that was
created when `Job` was defined. `Job.__table__.columns` (also accessible as
`Job.__table__.c`) is a mapping of column name to `Column` object. This lets
tests inspect column types, nullability, defaults, and constraints
programmatically — without querying the actual database.

`col.onupdate` is a Python-layer property, not a database-level property — it
is stored on the Python `Column` object in SQLAlchemy's in-memory
representation. Checking `col.onupdate is not None` verifies that SQLAlchemy
will add the `onupdate` expression to UPDATE statements. This does NOT test
database trigger behavior (there is none) — it tests that the ORM-level
behavior is configured correctly.
"""

import pytest
from sqlalchemy import inspect as sa_inspect

from backend.models.job import Job, JobStatus


# ── JobStatus enum ────────────────────────────────────────────────────────────

class TestJobStatus:
    def test_has_exactly_five_members(self):
        """
        The state machine has exactly 5 states (design §6.2).
        Adding a state without updating this test is intentional —
        but it forces a conscious decision.
        """
        assert len(JobStatus) == 5

    def test_all_expected_values_present(self):
        """Verify the exact string values stored in the database."""
        values = {s.value for s in JobStatus}
        assert values == {
            "pending_upload",
            "queued",
            "processing",
            "completed",
            "failed",
        }

    def test_pending_upload_value(self):
        assert JobStatus.PENDING_UPLOAD.value == "pending_upload"

    def test_queued_value(self):
        assert JobStatus.QUEUED.value == "queued"

    def test_processing_value(self):
        assert JobStatus.PROCESSING.value == "processing"

    def test_completed_value(self):
        assert JobStatus.COMPLETED.value == "completed"

    def test_failed_value(self):
        assert JobStatus.FAILED.value == "failed"

    def test_is_str_enum(self):
        """
        JobStatus inherits from str — members are valid strings.
        This means you can pass JobStatus.QUEUED where a str is expected
        without calling .value explicitly.
        """
        assert isinstance(JobStatus.QUEUED, str)
        assert JobStatus.QUEUED == "queued"


# ── Job model structure ───────────────────────────────────────────────────────

class TestJobModelStructure:
    def test_tablename(self):
        """Table is named 'jobs' — matches the migration and design §6.1."""
        assert Job.__tablename__ == "jobs"

    def test_has_exactly_15_columns(self):
        """
        The jobs table has exactly 15 columns (design §6.1).
        If this fails after adding a column, the migration must be updated too.
        """
        columns = [c.name for c in Job.__table__.columns]
        assert len(columns) == 15, (
            f"Expected 15 columns, got {len(columns)}: {columns}"
        )

    def test_column_names(self):
        """All 15 column names match the design §6.1 spec exactly."""
        columns = {c.name for c in Job.__table__.columns}
        expected = {
            "id",
            "user_id",
            "user_email",
            "original_filename",
            "original_s3_key",
            "processed_s3_key",
            "status",
            "failure_reason",
            "file_size_bytes",
            "row_count",
            "column_count",
            "column_names",
            "duplicate_rows_removed",
            "created_at",
            "updated_at",
        }
        assert columns == expected

    def test_id_is_primary_key(self):
        col = Job.__table__.c["id"]
        assert col.primary_key is True

    def test_nullable_columns(self):
        """
        Columns that may be NULL until the worker completes.
        These are all result columns — they're NULL in pending_upload/queued/processing.
        """
        nullable_cols = {
            c.name for c in Job.__table__.columns if c.nullable
        }
        expected_nullable = {
            "processed_s3_key",
            "failure_reason",
            "file_size_bytes",
            "row_count",
            "column_count",
            "column_names",
            "duplicate_rows_removed",
        }
        assert expected_nullable.issubset(nullable_cols), (
            f"Expected these columns to be nullable: "
            f"{expected_nullable - nullable_cols}"
        )

    def test_required_non_nullable_columns(self):
        """
        Core identity and state columns must never be NULL.
        A job without a user_id or status is meaningless.
        """
        non_nullable = {
            c.name for c in Job.__table__.columns if not c.nullable
        }
        required = {"id", "user_id", "user_email", "original_filename",
                    "original_s3_key", "status", "created_at", "updated_at"}
        assert required.issubset(non_nullable), (
            f"Expected these columns to be non-nullable: "
            f"{required - non_nullable}"
        )

    def test_has_three_indexes(self):
        """
        Three indexes defined on __table_args__: user_id, status, created_at.
        These are the three independent access patterns (design §6.1).
        """
        index_names = {idx.name for idx in Job.__table__.indexes}
        assert "idx_jobs_user_id" in index_names
        assert "idx_jobs_status" in index_names
        assert "idx_jobs_created_at" in index_names

    def test_updated_at_has_onupdate(self):
        """
        updated_at must have onupdate set so it changes on every UPDATE.
        Without this, updated_at equals created_at forever. (AD-24)
        """
        col = Job.__table__.c["updated_at"]
        assert col.onupdate is not None, (
            "updated_at is missing onupdate=func.now() — "
            "status transitions won't update the timestamp"
        )
