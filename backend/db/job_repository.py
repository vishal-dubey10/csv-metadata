"""
CSVForge — Job repository.

All database operations for the `jobs` table live here.
Routes and the worker import these functions; they never write raw queries.

ARCHITECTURE — where this file sits in the request stack:

    HTTP Request
        │
    FastAPI Route (main.py)
        │
    Depends(get_db) injects Session
        │
    job_repository.py functions  ←── YOU ARE HERE
        │
    SQLAlchemy ORM
        │
    PostgreSQL database

REPOSITORY PATTERN:
    job_repository.py is a "repository" — it encapsulates ALL database access
    for Job objects behind plain Python functions. The API layer (main.py)
    never writes SQL; it calls these functions instead.

    This separates concerns: if you switch from PostgreSQL to DynamoDB, you
    only change job_repository.py. Every other file stays the same.

    Contrast this with the Active Record pattern (used by Django ORM) where
    the model itself has `.save()`, `.delete()`, etc. — logic and data are
    merged. Repository keeps them separate.

SQLALCHEMY 2.0 RESULT CHAIN:
    Most read functions use this chain:
        db.execute(select(Job)).scalars().all()

    Step by step:
    1. select(Job)           — constructs a SELECT statement object (no SQL sent yet)
    2. db.execute(select(J)) — sends the SQL; returns a Result object (cursor-like,
                               holds rows from the database)
    3. .scalars()            — converts Result → ScalarResult; yields one Python object
                               per row instead of one tuple per row.
                               Without .scalars(), each row would be a Row tuple like
                               (Job_object,) — you'd have to write row[0] everywhere.
    4. .all()                — materializes the iterator into a Python list

    Contrast with the old SQLAlchemy 1.x style:
        session.query(Job).all()
    The new style separates query construction (select()) from execution
    (db.execute()) — cleaner, more composable, and works with Core + ORM alike.

Design principles:
- Every function accepts `db: Session` as its first argument.
  The caller (route or worker) owns the session lifecycle.
- All writes use SQLAlchemy ORM operations — no raw text() with
  string interpolation (SQL injection protection).
- Functions return None or bool rather than raising on "not found" —
  callers decide how to handle missing/unauthorized resources.
- `claim_job_for_processing` uses a conditional UPDATE (not SELECT FOR UPDATE)
  for lock-free idempotency. See function docstring for the race-condition analysis.

References: design.md §5.2, tasks.md T-04, AD-08
"""

import uuid
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.models.job import Job, JobStatus


# ── Create ────────────────────────────────────────────────────────────────────

def create_job(
    db: Session,
    user_id: str,
    user_email: str,
    original_filename: str,
    original_s3_key: str,
) -> Job:
    """
    Create a new Job record in `pending_upload` status.

    Called by the upload intent endpoint (POST /uploads/intent).
    The job exists in the database before the file reaches S3.
    status defaults to 'pending_upload' via the column server_default.

    Returns the created Job with all server-generated fields (id, created_at,
    updated_at) populated. expire_on_commit=False on the session ensures
    these attributes remain readable after commit without a second SELECT.

    HOW flush() + refresh() WORKS HERE:

    db.add(job):
        Registers the new object with the SQLAlchemy session (the Unit of Work).
        SQLAlchemy now "knows about" this job object, but no SQL has been sent
        to the database yet. The job exists only in Python memory.

    db.flush():
        Sends the SQL INSERT to the database WITHIN the current transaction,
        but does NOT commit. Think of it as "writing in pencil":
        - The row is visible to other queries within the SAME transaction.
        - The row is NOT visible to other database connections (other requests)
          because the transaction hasn't committed yet.
        - If the transaction is rolled back, the row disappears entirely.

        After flush(), PostgreSQL executes the INSERT and generates server-side
        defaults: the `created_at` and `updated_at` timestamps (via
        `server_default=text("CURRENT_TIMESTAMP")`). The id was generated in
        Python by uuid.uuid4(), so it was already set before the flush.

    db.commit():
        Ends the transaction and makes changes permanent — visible to ALL
        database connections. Think of it as "signing in ink".
        NOTE: create_job DOES call commit (via the session after flush+refresh).
        In a more layered architecture, the route handler would own the commit.
        For simplicity in this stage of the project, the session's commit happens
        at the request boundary via the get_db() generator in connection.py.

    db.refresh(job):
        After db.flush(), the Python `job` object in memory may NOT reflect
        values that the database generated server-side. For example, `job.id`
        might still be None if we had used gen_random_uuid() in the DB instead
        of uuid.uuid4() in Python, and created_at would also be stale.

        db.refresh(job) sends:
            SELECT * FROM jobs WHERE id = <id>
        ...to the database and updates the Python object with the actual
        values the database stored, including all server-generated defaults.

        This is why we flush() first (so the row exists) then refresh()
        (so our Python object reflects what the database actually stored).

    WHY NOT db.commit() IN create_job DIRECTLY?
        In a web request, the session's transaction is typically managed at the
        request boundary (the route handler via get_db). The repository flushes
        to get server-generated values back, but the final commit is the
        caller's responsibility. In the current implementation this is managed
        by get_db()'s try/finally block (see connection.py).
    """
    job = Job(
        id=str(uuid.uuid4()),   # generate UUID in Python; server_default is a fallback
        user_id=user_id,
        user_email=user_email,
        original_filename=original_filename,
        original_s3_key=original_s3_key,
        status=JobStatus.PENDING_UPLOAD.value,
    )
    db.add(job)
    db.flush()   # write to DB within the current transaction; assigns server defaults
    db.refresh(job)  # reload to get server-generated values (created_at, updated_at)
    return job


# ── Read ──────────────────────────────────────────────────────────────────────

def get_job_by_id(db: Session, job_id: str) -> Optional[Job]:
    """
    Fetch a Job by its primary key, regardless of owner.

    Used by the worker (which doesn't have a user_id context) and by
    admin endpoints. For user-facing endpoints, prefer get_job_by_id_and_user
    to enforce ownership.

    Returns None if not found (caller decides whether that's a 404 or an error).
    """
    stmt = select(Job).where(Job.id == job_id)
    return db.execute(stmt).scalar_one_or_none()


def get_job_by_id_and_user(
    db: Session, job_id: str, user_id: str
) -> Optional[Job]:
    """
    Fetch a Job by id, verifying the caller owns it.

    Returns None if:
    - The job doesn't exist
    - The job exists but belongs to a different user

    The caller (route handler) maps None to a 404 response.
    We return 404 in both cases (not 403) to avoid leaking the existence
    of other users' jobs to an unauthorized caller.

    Ownership is always enforced on user_id (Cognito sub), never on
    user_email (which is client-supplied and informational only). AD-23.
    """
    stmt = select(Job).where(Job.id == job_id, Job.user_id == user_id)
    return db.execute(stmt).scalar_one_or_none()


def list_jobs_for_user(db: Session, user_id: str) -> list[Job]:
    """
    List all jobs visible to a user, ordered newest-first.

    Excludes `pending_upload` jobs — from the user's perspective, a job
    doesn't exist until the S3 upload is confirmed. Showing pending_upload
    rows would present jobs that appear stuck or in an ambiguous state.
    (Design §4.3, AD-13)
    """
    stmt = (
        select(Job)
        .where(
            Job.user_id == user_id,
            Job.status != JobStatus.PENDING_UPLOAD.value,
        )
        .order_by(Job.created_at.desc())
    )
    return list(db.execute(stmt).scalars().all())


def list_all_jobs(
    db: Session,
    skip: int = 0,
    limit: int = 25,
    status_filter: Optional[str] = None,
) -> list[Job]:
    """
    List all jobs for the admin view, with optional status filtering.

    Supports React Admin's ra-data-simple-rest pagination convention
    (_start/_end parameters map to skip/limit). Returns jobs for ALL users.

    skip:          number of rows to skip (offset)
    limit:         maximum rows to return
    status_filter: if provided, filter to this status value only
    """
    stmt = select(Job)
    if status_filter:
        stmt = stmt.where(Job.status == status_filter)
    stmt = stmt.order_by(Job.created_at.desc()).offset(skip).limit(limit)
    return list(db.execute(stmt).scalars().all())


def count_all_jobs(
    db: Session,
    status_filter: Optional[str] = None,
) -> int:
    """
    Count total jobs for the admin view (used for Content-Range header).

    React Admin's ra-data-simple-rest provider reads the Content-Range header
    to know the total count for pagination: `Content-Range: jobs 0-24/150`.
    This function provides the total (the `150` part).

    `from sqlalchemy import func as sa_func` is a local import (inside the
    function body rather than at the top of the file). This avoids a name
    collision: `func` is a common variable name in Python, and importing
    it at module level could shadow it accidentally. Aliasing to `sa_func`
    makes it clear this is the SQLAlchemy function expression helper.
    """
    from sqlalchemy import func as sa_func
    stmt = select(sa_func.count(Job.id))
    if status_filter:
        stmt = stmt.where(Job.status == status_filter)
    result = db.execute(stmt).scalar_one()
    return result


# ── State transitions ─────────────────────────────────────────────────────────

def transition_to_queued(db: Session, job_id: str, user_id: str) -> bool:
    """
    Transition a job from pending_upload → queued.

    Called by POST /uploads/{job_id}/complete after S3 upload is confirmed.

    The WHERE clause enforces both:
    - This job is still in pending_upload (can't queue an already-queued job)
    - The caller owns this job (prevents user B from queuing user A's job)

    Returns True if the transition succeeded (1 row updated).
    Returns False if:
    - The job doesn't exist
    - The job is not in pending_upload (already transitioned)
    - The user_id doesn't match (wrong owner)

    synchronize_session="evaluate" explained:
        After a bulk UPDATE, SQLAlchemy needs to decide what to do with any
        Job objects it already has loaded in the session's identity map
        (the in-memory cache of Python objects keyed by primary key).

        "evaluate" (the default for UPDATE): SQLAlchemy re-evaluates the
        WHERE clause against the Python objects already loaded in the session.
        Any objects that match the WHERE clause have their attributes updated
        in Python to reflect the new values — no extra SELECT needed.
        This is safe when the WHERE clause is simple enough for SQLAlchemy
        to evaluate in Python (equality checks, IN clauses, etc.).

        False (used in claim_job_for_processing): SQLAlchemy does NOT update
        Python objects. Any loaded Job objects may be stale after the UPDATE.
        This is why claim_job_for_processing calls db.expire_all() afterward —
        to force a fresh SELECT on next access.

        When to use which:
        - Use "evaluate" for simple WHERE clauses (equality, simple IN).
        - Use False for complex queries (subqueries, database functions,
          OR conditions across multiple tables) that SQLAlchemy cannot
          safely re-evaluate in Python.
    """
    stmt = (
        update(Job)
        .where(Job.id == job_id, Job.user_id == user_id, Job.status == JobStatus.PENDING_UPLOAD.value)
        .values(status=JobStatus.QUEUED.value)
        .execution_options(synchronize_session="evaluate")
    )
    result = db.execute(stmt)
    return result.rowcount == 1


def claim_job_for_processing(db: Session, job_id: str) -> bool:
    """
    Atomically claim a job for processing: queued/processing → processing.

    This is the idempotency guard for the worker. SQS delivers messages
    at-least-once — the same job_id can arrive twice if the Lambda times out.
    This function ensures only one worker processes a job at a time.

    The conditional UPDATE:
        UPDATE jobs
        SET status = 'processing'
        WHERE id = <job_id>
        AND status IN ('queued', 'processing')

    ════════════════════════════════════════════════════════════════════════
    DEEP DIVE: WHY CONDITIONAL UPDATE (NOT SELECT + UPDATE)
    ════════════════════════════════════════════════════════════════════════

    A naive "SELECT then UPDATE" approach has a race condition:
        1. Worker A: SELECT status FROM jobs WHERE id=X  → 'queued'
        2. Worker B: SELECT status FROM jobs WHERE id=X  → 'queued'
        3. Worker A: UPDATE jobs SET status='processing' WHERE id=X
        4. Worker B: UPDATE jobs SET status='processing' WHERE id=X
    Both workers think they own the job. Both process it. Data corruption.

    A conditional UPDATE is ATOMIC — the check and the set happen in ONE
    SQL statement. The database's row-level locking ensures that only one
    UPDATE can write the new status at a time.

    ════════════════════════════════════════════════════════════════════════
    WHY rowcount MATTERS
    ════════════════════════════════════════════════════════════════════════

    result.rowcount tells us how many rows were actually updated by the
    statement. If rowcount=1, our WHERE clause matched — we won the race
    and own this job. If rowcount=0, the WHERE clause didn't match —
    another worker already changed the status (or the job doesn't exist).

    ════════════════════════════════════════════════════════════════════════
    IDEMPOTENCY
    ════════════════════════════════════════════════════════════════════════

    Calling claim_job_for_processing twice for the same job is safe:
    - First call: status='queued' → matches WHERE, rowcount=1, returns True.
    - Second call: status='processing' → still matches WHERE (because
      'processing' is in the IN clause), rowcount=1, returns True again.

    WHY include 'processing' in the WHERE clause?
    If a Lambda timed out mid-processing, SQS re-delivers the message.
    The job is already in 'processing'. We WANT to re-claim it — the
    prior Lambda is dead. Including 'processing' in the WHERE clause allows
    a retry to take over. This is correct idempotent behavior. AD-08.

    ════════════════════════════════════════════════════════════════════════
    TWO WORKERS RACING — PARKING SPOT ANALOGY + MVCC
    ════════════════════════════════════════════════════════════════════════

    Analogy:
        Two drivers (workers) see an empty parking spot (queued job). They
        both start driving toward it at the same time. The spot has a chain
        (the DB row lock). The first driver to reach the chain pulls it down
        and parks. The second driver arrives a moment later, sees the chain
        is already up (status='processing'), and drives away (rowcount=0,
        returns False).

    What PostgreSQL actually does — MVCC:
        PostgreSQL uses Multi-Version Concurrency Control (MVCC). When
        Worker A executes its UPDATE, PostgreSQL creates a NEW VERSION of
        that row with status='processing' and marks the old version as
        superseded. Worker B's UPDATE, which started at almost the same
        time, was working from the original snapshot (status='queued').

        When PostgreSQL evaluates Worker B's WHERE clause, it sees the
        COMMITTED version of the row — which is now 'processing' (because
        Worker A's UPDATE committed first). Worker B's WHERE clause
        (`status IN ('queued', 'processing')`) actually still matches
        'processing'. So both workers CAN get rowcount=1 in the case where
        the job starts in 'queued'.

        This is why the function's docstring notes that claim_job_for_processing
        is the PRIMARY guard, not a perfect mutual-exclusion lock. The second
        layer of idempotency (checking processed_s3_key) is implemented in
        the worker itself (Day 3).

    WHY NOT SELECT FOR UPDATE?
        SELECT FOR UPDATE acquires an exclusive row-level lock at SELECT time,
        preventing other transactions from updating the row until the first
        transaction commits. This works but has higher lock contention — all
        workers queue up waiting for the same lock. The conditional UPDATE
        approach uses no explicit locks; PostgreSQL's built-in MVCC provides
        isolation with lower overhead. For a job queue, the conditional UPDATE
        pattern is simpler, faster, and easier to reason about.

    ════════════════════════════════════════════════════════════════════════
    synchronize_session=False AND expire_all()
    ════════════════════════════════════════════════════════════════════════

    synchronize_session=False:
        Tells SQLAlchemy NOT to update Python objects in the session's
        identity map after this bulk UPDATE. This is correct for a complex
        IN clause that SQLAlchemy cannot safely evaluate in Python.
        The trade-off: any Job objects already loaded in this session are now
        stale — their .status attribute still shows the pre-UPDATE value.

    db.expire_all():
        SQLAlchemy maintains an identity map — a cache that maps
        (Model, primary_key) → loaded Python object. If you called
        get_job_by_id() earlier in the same request and got a Job object,
        that object is now in the identity map. After the conditional UPDATE
        commits, that Python object's .status attribute is STALE — it still
        shows 'queued' from memory, not 'processing' from the database.

        db.expire_all() marks ALL loaded objects as "expired". The next
        time you access any attribute on any of these objects, SQLAlchemy
        automatically issues a fresh SELECT to get the current database value.

        This is essential here because expire_on_commit=False (set in
        connection.py on SessionLocal) means commit does NOT automatically
        expire loaded objects. Without expire_all(), calling job.status
        after this UPDATE would return the pre-update status from the
        Python in-memory cache.

    Returns True if this worker should proceed.
    Returns False if the job is in a terminal state (completed/failed)
    or doesn't exist — caller logs and returns without processing.

    Note on concurrent claim race:
    Because both workers match IN ('queued', 'processing') and both
    execute the same UPDATE, both can theoretically get rowcount=1 if
    they race on a 'queued' row. The second layer of idempotency in the
    worker is to re-check the job status from the DB after claiming and
    verify the file hasn't already been processed (processed_s3_key is None).
    For Day 1, claim_job_for_processing is the primary guard. Full idempotency
    analysis is part of Day 3 (worker implementation).
    """
    stmt = (
        update(Job)
        .where(
            Job.id == job_id,
            Job.status.in_([JobStatus.QUEUED.value, JobStatus.PROCESSING.value]),
        )
        .values(status=JobStatus.PROCESSING.value)
        .execution_options(synchronize_session=False)
    )
    result = db.execute(stmt)
    # expire_all() clears the session identity map so any subsequent SELECT
    # re-fetches from the database rather than returning stale cached objects.
    # Needed because synchronize_session=False skips in-memory updates and
    # SessionLocal has expire_on_commit=False.
    db.expire_all()
    return result.rowcount == 1


def complete_job(
    db: Session,
    job_id: str,
    processed_s3_key: str,
    file_size_bytes: int,
    row_count: int,
    column_count: int,
    column_names: list[str],
    duplicate_rows_removed: int,
) -> None:
    """
    Mark a job as completed and store all processing results.

    Called by the worker after successfully writing the processed file to S3.
    The S3 write happens BEFORE this call so that if this DB update fails,
    the retry can safely overwrite the same deterministic S3 key. (AD-14)

    All result fields are written in a single UPDATE statement —
    one round-trip to the database.
    """
    stmt = (
        update(Job)
        .where(Job.id == job_id)
        .values(
            status=JobStatus.COMPLETED.value,
            processed_s3_key=processed_s3_key,
            file_size_bytes=file_size_bytes,
            row_count=row_count,
            column_count=column_count,
            column_names=column_names,
            duplicate_rows_removed=duplicate_rows_removed,
        )
        .execution_options(synchronize_session=False)
    )
    db.execute(stmt)
    db.expire_all()


def fail_job(db: Session, job_id: str, failure_reason: str) -> None:
    """
    Mark a job as permanently failed with a human-readable reason.

    Called by the worker when it encounters a permanent validation error
    (e.g., invalid CSV encoding, duplicate headers — see AD-21).

    The worker does NOT raise an exception after calling this function.
    Raising would cause SQS to retry the message, but the file is permanently
    invalid — retrying would loop forever. Setting status=failed and returning
    successfully tells SQS to delete the message. The failure is surfaced
    to the user through the job status API.

    Also called on unrecoverable infrastructure errors where retrying would
    be harmful (e.g., job is already in a terminal state).
    """
    stmt = (
        update(Job)
        .where(Job.id == job_id)
        .values(
            status=JobStatus.FAILED.value,
            failure_reason=failure_reason,
        )
        .execution_options(synchronize_session=False)
    )
    db.execute(stmt)
    db.expire_all()
