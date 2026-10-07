"""
Integration test: Alembic migration and schema verification.

Requires Docker PostgreSQL to be running with `alembic upgrade head` already applied.

What this tests:
- The jobs table exists in the database
- All 15 columns exist with the correct names
- The three expected indexes exist
- The alembic_version table exists and contains our revision
- The status column has the correct default value

These tests DO NOT apply the migration themselves — they verify that a migration
that has already been applied produced the expected schema. This mirrors how you'd
verify a production migration: apply it, then check the schema.

INFORMATION_SCHEMA AND pg_indexes:
`information_schema.tables` and `information_schema.columns` are PostgreSQL
system views that describe the database's structure. Querying them lets tests
verify the migration created the right schema without querying the `jobs` table
directly (which might not exist if the migration failed).

`pg_indexes` is a PostgreSQL catalog view listing all indexes in the database.
It is used here instead of `information_schema` because information_schema
does not reliably expose index details in all PostgreSQL versions.

ALEMBIC_VERSION TABLE:
Alembic creates a table called `alembic_version` in the database that stores
the current revision ID. Querying it tells you which migration was last applied.
This test verifies that `alembic upgrade head` populated this table correctly.
If it's empty or missing, migrations didn't run (or ran without completing).

MIRRORING PRODUCTION VERIFICATION:
Before deploying a new version of CSVForge, the deployment process verifies
that the database schema matches what the application expects. These tests are
the automated version of that check — run them after applying a migration to
confirm the schema is correct before deploying the application code.
"""

import pytest
from sqlalchemy import text


# ── Schema verification ───────────────────────────────────────────────────────

def test_jobs_table_exists(pg_db):
    """
    The jobs table must exist after `alembic upgrade head`.
    If this fails, the migration hasn't been applied.
    """
    result = pg_db.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'jobs'"
    )).fetchone()
    assert result is not None, (
        "jobs table not found — did you run `alembic upgrade head`?"
    )


def test_jobs_table_has_15_columns(pg_db):
    """
    The jobs table must have exactly 15 columns.
    This validates that the migration matches the model definition.
    """
    result = pg_db.execute(text(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'jobs' AND table_schema = 'public'"
    )).fetchone()
    assert result[0] == 15, (
        f"Expected 15 columns in jobs table, got {result[0]}"
    )


def test_all_column_names_present(pg_db):
    """
    Verify every expected column name exists in the database schema.
    Order doesn't matter — we check the set.
    """
    result = pg_db.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'jobs' AND table_schema = 'public'"
    )).fetchall()
    actual_columns = {row[0] for row in result}

    expected_columns = {
        "id", "user_id", "user_email",
        "original_filename", "original_s3_key", "processed_s3_key",
        "status", "failure_reason",
        "file_size_bytes", "row_count", "column_count",
        "column_names", "duplicate_rows_removed",
        "created_at", "updated_at",
    }
    assert expected_columns == actual_columns, (
        f"Column mismatch. Missing: {expected_columns - actual_columns}. "
        f"Extra: {actual_columns - expected_columns}"
    )


def test_status_column_has_default(pg_db):
    """
    The status column must have server_default = 'pending_upload'.
    Without this, inserting a row without specifying status would fail.
    """
    result = pg_db.execute(text(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'jobs' AND column_name = 'status'"
    )).fetchone()
    assert result is not None
    assert "pending_upload" in result[0], (
        f"Expected status default to contain 'pending_upload', got: {result[0]}"
    )


def test_timestamps_use_timezone(pg_db):
    """
    created_at and updated_at must be TIMESTAMPTZ (timestamp with time zone).
    TIMESTAMP without timezone would make timestamps ambiguous across timezones.
    """
    result = pg_db.execute(text(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'jobs' "
        "AND column_name IN ('created_at', 'updated_at')"
    )).fetchall()

    assert len(result) == 2, "Both created_at and updated_at must exist"
    for col_name, data_type in result:
        assert data_type == "timestamp with time zone", (
            f"{col_name} has type '{data_type}', expected 'timestamp with time zone'. "
            "Use TIMESTAMPTZ to store UTC timestamps safely."
        )


def test_column_names_is_array_type(pg_db):
    """
    column_names must be a TEXT[] array, not TEXT or VARCHAR.
    This stores the ordered list of column headers from the CSV.
    """
    result = pg_db.execute(text(
        "SELECT data_type, udt_name FROM information_schema.columns "
        "WHERE table_name = 'jobs' AND column_name = 'column_names'"
    )).fetchone()
    assert result is not None
    # PostgreSQL reports ARRAY columns with data_type = 'ARRAY' and udt_name = '_text'
    assert result[0] == "ARRAY", (
        f"column_names has data_type '{result[0]}', expected 'ARRAY'"
    )


def test_three_indexes_exist(pg_db):
    """
    The three query-optimizing indexes must exist after migration.
    Missing indexes won't cause errors but will cause slow queries.
    """
    result = pg_db.execute(text(
        "SELECT indexname FROM pg_indexes "
        "WHERE tablename = 'jobs' "
        "AND indexname NOT LIKE '%_pkey'"  # exclude primary key index
    )).fetchall()
    index_names = {row[0] for row in result}

    assert "idx_jobs_user_id" in index_names, "Missing index on user_id"
    assert "idx_jobs_status" in index_names, "Missing index on status"
    assert "idx_jobs_created_at" in index_names, "Missing index on created_at"


# ── Alembic version tracking ──────────────────────────────────────────────────

def test_alembic_version_table_exists(pg_db):
    """
    Alembic's version tracking table must exist.
    If it's missing, `alembic upgrade head` was never run.
    """
    result = pg_db.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name = 'alembic_version'"
    )).fetchone()
    assert result is not None, (
        "alembic_version table not found — did you run `alembic upgrade head`?"
    )


def test_alembic_current_revision_is_0001(pg_db):
    """
    The current applied migration must be revision '0001'.
    This confirms the migration was applied, not skipped.
    """
    result = pg_db.execute(text(
        "SELECT version_num FROM alembic_version"
    )).fetchone()
    assert result is not None, "No migration version found in alembic_version"
    assert result[0] == "0001", (
        f"Expected alembic revision '0001', got '{result[0]}'"
    )
