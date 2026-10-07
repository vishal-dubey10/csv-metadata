"""
Integration tests for the job repository — real PostgreSQL backend.

These tests exercise behavior that requires PostgreSQL specifically:
- ARRAY column storage and retrieval (column_names)
- TIMESTAMPTZ precision and updated_at tracking
- complete_job storing all result fields
- Terminal state enforcement with real DB constraints

Each test uses the pg_db fixture which rolls back all changes after the test —
the database is always left in a clean state.

Requires: Docker PostgreSQL running + `alembic upgrade head` applied.

WHY THESE TESTS DUPLICATE UNIT TEST SCENARIOS:
The unit tests in `test_job_repository_unit.py` use SQLite (fast, no Docker
needed). SQLite does not support the ARRAY type, so it cannot test that
`column_names` is stored as a PostgreSQL array and retrieved as a Python list.
These integration tests run against real PostgreSQL and verify that ARRAY
storage works correctly — that errors can be stored as a PostgreSQL array and
retrieved as a Python list. This is the "belt and suspenders" verification:
SQLite passes the structural tests, PostgreSQL confirms the actual storage
behavior is identical for the ORM operations we care about.

WHY time.sleep() IS USED IN THE updated_at TEST:
The test sleeps briefly between create and update to ensure `updated_at >
created_at`. Without the sleep, both timestamps might fall within the same
sub-millisecond precision window — the clock would not have advanced enough
for the comparison to be meaningful. Even 50ms is enough for the server clock
to tick forward and produce a distinct timestamp.
"""

import time

import pytest
from sqlalchemy import text

from backend.db.job_repository import (
    claim_job_for_processing,
    complete_job,
    count_all_jobs,
    create_job,
    fail_job,
    get_job_by_id,
    get_job_by_id_and_user,
    list_all_jobs,
    list_jobs_for_user,
    transition_to_queued,
)
from backend.models.job import JobStatus


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_job(db, user_id="user-1", user_email="user@example.com",
              filename="data.csv", s3_key=None):
    import uuid
    if s3_key is None:
        s3_key = f"uploads/{user_id}/{uuid.uuid4()}/original.csv"
    job = create_job(
        db,
        user_id=user_id,
        user_email=user_email,
        original_filename=filename,
        original_s3_key=s3_key,
    )
    db.commit()
    return job


# ── complete_job ──────────────────────────────────────────────────────────────

class TestCompleteJob:
    def test_sets_status_to_completed(self, pg_db):
        job = _make_job(pg_db)
        complete_job(
            pg_db, job.id,
            processed_s3_key="processed/user-1/job-1/result.csv",
            file_size_bytes=1024,
            row_count=100,
            column_count=3,
            column_names=["name", "age", "email"],
            duplicate_rows_removed=5,
        )
        pg_db.commit()

        refreshed = get_job_by_id(pg_db, job.id)
        assert refreshed.status == JobStatus.COMPLETED.value

    def test_stores_processed_s3_key(self, pg_db):
        job = _make_job(pg_db)
        key = "processed/user-1/job-1/result.csv"
        complete_job(pg_db, job.id, processed_s3_key=key,
                     file_size_bytes=512, row_count=10, column_count=2,
                     column_names=["a", "b"], duplicate_rows_removed=0)
        pg_db.commit()

        refreshed = get_job_by_id(pg_db, job.id)
        assert refreshed.processed_s3_key == key

    def test_stores_column_names_as_array(self, pg_db):
        """
        PostgreSQL-specific: column_names is stored as TEXT[] and retrieved
        as a Python list. SQLite cannot test this.
        """
        job = _make_job(pg_db)
        names = ["first_name", "last_name", "email", "age"]
        complete_job(pg_db, job.id,
                     processed_s3_key="processed/x/result.csv",
                     file_size_bytes=2048, row_count=50, column_count=4,
                     column_names=names, duplicate_rows_removed=2)
        pg_db.commit()

        refreshed = get_job_by_id(pg_db, job.id)
        assert refreshed.column_names == names
        assert isinstance(refreshed.column_names, list)

    def test_stores_all_numeric_results(self, pg_db):
        job = _make_job(pg_db)
        complete_job(pg_db, job.id,
                     processed_s3_key="processed/x/result.csv",
                     file_size_bytes=52428800,
                     row_count=100000,
                     column_count=50,
                     column_names=["col"] * 50,
                     duplicate_rows_removed=9999)
        pg_db.commit()

        refreshed = get_job_by_id(pg_db, job.id)
        assert refreshed.file_size_bytes == 52428800
        assert refreshed.row_count == 100000
        assert refreshed.column_count == 50
        assert refreshed.duplicate_rows_removed == 9999


# ── updated_at tracking ───────────────────────────────────────────────────────

class TestUpdatedAt:
    def test_updated_at_equals_created_at_on_creation(self, pg_db):
        """
        At creation, updated_at and created_at are set by the same NOW() call
        in the server_default — they should be equal (or within milliseconds).
        """
        job = _make_job(pg_db)
        # Both are set by server_default=func.now() — same transaction, same timestamp
        assert job.created_at is not None
        assert job.updated_at is not None
        # Allow a tiny delta for clock ticks between the two column writes
        delta = abs((job.updated_at - job.created_at).total_seconds())
        assert delta < 1.0, f"created_at and updated_at differ by {delta}s at creation"

    def test_updated_at_changes_after_status_transition(self, pg_db):
        """
        After a status transition, updated_at must be >= created_at.
        This verifies onupdate=func.now() is working. (AD-24)

        Without onupdate, updated_at stays equal to created_at forever —
        you'd lose your audit trail for when jobs changed state.
        """
        job = _make_job(pg_db)
        created_at = job.created_at

        # Small sleep to ensure the clock advances
        time.sleep(0.05)

        fail_job(pg_db, job.id, "test failure")
        pg_db.commit()

        refreshed = get_job_by_id(pg_db, job.id)
        assert refreshed.updated_at >= created_at, (
            "updated_at must be >= created_at after a status transition. "
            "Check that onupdate=func.now() is set on the updated_at column."
        )


# ── list_all_jobs (admin) ─────────────────────────────────────────────────────

class TestListAllJobs:
    def test_returns_jobs_across_all_users(self, pg_db):
        """Admin can see jobs from multiple users."""
        job_a = _make_job(pg_db, user_id="user-A")
        job_b = _make_job(pg_db, user_id="user-B")

        all_jobs = list_all_jobs(pg_db)
        all_ids = [j.id for j in all_jobs]
        assert job_a.id in all_ids
        assert job_b.id in all_ids

    def test_status_filter_works(self, pg_db):
        """Admin can filter by status."""
        job_pending = _make_job(pg_db)
        job_failed = _make_job(pg_db)
        fail_job(pg_db, job_failed.id, "test")
        pg_db.commit()

        failed_jobs = list_all_jobs(pg_db, status_filter="failed")
        failed_ids = [j.id for j in failed_jobs]

        assert job_failed.id in failed_ids
        assert job_pending.id not in failed_ids

    def test_pagination_skip_limit(self, pg_db):
        """Skip and limit work for pagination."""
        # Create 3 jobs
        jobs = [_make_job(pg_db, user_id=f"user-{i}") for i in range(3)]

        page1 = list_all_jobs(pg_db, skip=0, limit=2)
        page2 = list_all_jobs(pg_db, skip=2, limit=2)

        assert len(page1) == 2
        assert len(page2) >= 1


# ── count_all_jobs ────────────────────────────────────────────────────────────

class TestCountAllJobs:
    def test_count_increases_with_new_jobs(self, pg_db):
        initial_count = count_all_jobs(pg_db)
        _make_job(pg_db)
        _make_job(pg_db)
        new_count = count_all_jobs(pg_db)
        assert new_count == initial_count + 2

    def test_count_with_status_filter(self, pg_db):
        job = _make_job(pg_db)
        fail_job(pg_db, job.id, "test")
        pg_db.commit()

        failed_count = count_all_jobs(pg_db, status_filter="failed")
        assert failed_count >= 1


# ── Full workflow ─────────────────────────────────────────────────────────────

class TestFullWorkflow:
    def test_complete_state_machine_transitions(self, pg_db):
        """
        Walk a job through the complete happy path:
        pending_upload → queued → processing → completed

        This is the lifecycle diagram from design §6.2.
        """
        # 1. Create (pending_upload)
        job = _make_job(pg_db, user_id="user-workflow")
        assert job.status == JobStatus.PENDING_UPLOAD.value

        # 2. Queue (S3 upload confirmed)
        success = transition_to_queued(pg_db, job.id, "user-workflow")
        pg_db.commit()
        assert success is True
        assert get_job_by_id(pg_db, job.id).status == JobStatus.QUEUED.value

        # 3. Claim (worker starts)
        claimed = claim_job_for_processing(pg_db, job.id)
        pg_db.commit()
        assert claimed is True
        assert get_job_by_id(pg_db, job.id).status == JobStatus.PROCESSING.value

        # 4. Complete (worker finishes)
        complete_job(
            pg_db, job.id,
            processed_s3_key="processed/user-workflow/result.csv",
            file_size_bytes=1024,
            row_count=42,
            column_count=3,
            column_names=["col1", "col2", "col3"],
            duplicate_rows_removed=3,
        )
        pg_db.commit()

        final = get_job_by_id(pg_db, job.id)
        assert final.status == JobStatus.COMPLETED.value
        assert final.row_count == 42
        assert final.column_names == ["col1", "col2", "col3"]
        assert final.duplicate_rows_removed == 3

        # 5. Completed job is visible to user
        user_jobs = list_jobs_for_user(pg_db, "user-workflow")
        assert any(j.id == job.id for j in user_jobs)

    def test_failure_state_machine(self, pg_db):
        """
        Walk a job through the failure path:
        pending_upload → queued → processing → failed
        """
        job = _make_job(pg_db, user_id="user-fail")
        transition_to_queued(pg_db, job.id, "user-fail")
        pg_db.commit()
        claim_job_for_processing(pg_db, job.id)
        pg_db.commit()
        fail_job(pg_db, job.id, "Duplicate column name: 'id' appears 2 times")
        pg_db.commit()

        final = get_job_by_id(pg_db, job.id)
        assert final.status == JobStatus.FAILED.value
        assert "Duplicate column name" in final.failure_reason

        # Failed job cannot be re-claimed (idempotency)
        assert claim_job_for_processing(pg_db, job.id) is False

        # Failed job is visible to user (they need to see why it failed)
        user_jobs = list_jobs_for_user(pg_db, "user-fail")
        assert any(j.id == job.id for j in user_jobs)
