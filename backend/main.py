"""
CSVForge — FastAPI application entry point.

This module creates the FastAPI app instance, registers middleware,
and defines all HTTP endpoints.

WSGI vs ASGI:
    WSGI (Web Server Gateway Interface) is the traditional Python web protocol —
    synchronous, one request at a time per thread. Django and Flask use WSGI.

    ASGI (Asynchronous Server Gateway Interface) is the modern async equivalent.
    FastAPI is an ASGI application — it can handle many concurrent connections
    without spinning up a new thread for each one.

UVICORN:
    Uvicorn is an ASGI server (analogous to gunicorn for WSGI). It listens for
    incoming HTTP connections and speaks the ASGI protocol to FastAPI.
    Used for local development and container deployments:
        uvicorn backend.main:app --reload

MANGUM:
    Mangum wraps a FastAPI/ASGI app in an AWS Lambda handler. When Lambda
    receives an API Gateway event (a JSON dict), Mangum translates it into an
    ASGI request, passes it to FastAPI, and packages the ASGI response back
    into the Lambda response format. Same FastAPI app — different transport layer.
        from mangum import Mangum
        handler = Mangum(app)

FULL REQUEST LIFECYCLE:

    Browser
       │ HTTP Request (with Authorization: Bearer <access_token>)
    Uvicorn (local) / API Gateway + Mangum (Lambda)
       │ ASGI
    FastAPI
       │
    CORS Middleware  ← checks Origin header, adds CORS response headers
       │
    Route Matching  ← @app.get("/jobs") etc.
       │
    Dependency Resolution:
       │   Depends(get_current_user) → extracts user identity from JWT or header
       │   Depends(get_db)           → creates a SQLAlchemy Session
       │
    Route Handler  ← your function runs here
       │
    Repository Functions  ← job_repository.py (ownership-checked queries)
       │
    SQLAlchemy ORM
       │
    PostgreSQL
       │ response
    Route Handler returns Pydantic model
       │
    FastAPI serializes to JSON
       │
    CORS Middleware adds headers
       │
    Uvicorn / Mangum
       │ HTTP Response
    Browser

ENDPOINT OVERVIEW:

    Public:
        GET  /health                         — liveness probe

    User-facing (authenticated):
        POST /jobs                           — create job + presigned upload URL (alias for /uploads/intent)
        POST /uploads/intent                 — same as POST /jobs (original route name)
        POST /uploads/{job_id}/complete      — confirm S3 upload, transition to queued
        GET  /jobs                           — list all jobs for the authenticated user
        GET  /jobs/{job_id}                  — get one job (ownership-checked)
        GET  /jobs/{job_id}/download         — presigned S3 download URL (ownership-checked)

    Admin-only (requires csvforge-admins group):
        GET  /admin/jobs                     — list all jobs for all users (paginated)

AUTHENTICATION:
    All user-facing endpoints require authentication via get_current_user().
    See backend/auth.py for the full explanation of the local-dev bypass vs
    the production API Gateway JWT claims path.

AUTHORIZATION (ownership enforcement):
    Routes that access a specific job ALWAYS verify the caller owns it.
    This prevents IDOR (Insecure Direct Object Reference) vulnerabilities.
    See AD-23: user_id from sub claim is the only authoritative identity.
    See AD-13: missing or wrong-user lookups return 404, not 403.

INTERVIEW CONCEPT — 404 vs 403 for ownership failures (AD-13):
    When a user requests a job that doesn't exist OR belongs to another user,
    we return 404 (not 403). This is a security choice: returning 403 would
    reveal that the job EXISTS (just not accessible), enabling enumeration
    attacks. With 404 for both cases, an attacker learns nothing from a
    failed request. The trade-off: genuine "job not found" errors and
    "wrong owner" errors look identical to the caller — acceptable because
    legitimate callers always know their own job IDs.
"""

import os
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from backend.auth import CurrentUser, get_current_user, require_admin
from backend.db.connection import get_db
from backend.db.job_repository import (
    count_all_jobs,
    create_job,
    get_job_by_id,
    get_job_by_id_and_user,
    list_all_jobs,
    list_jobs_for_user,
    transition_to_queued,
)
from backend.s3_client import (
    generate_presigned_download_url,
    generate_presigned_upload_url,
    get_object_size,
    object_exists,
)
from backend.schemas import (
    AdminJobResponse,
    JobDownloadResponse,
    JobListResponse,
    JobResponse,
    PostJobRequest,
    UploadCompleteResponse,
    UploadIntentRequest,
    UploadIntentResponse,
)
from backend.sqs_client import send_job_message

# ── App instance ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="CSVForge API",
    version="0.2.0",
    description=(
        "CSV upload, processing, and download service. "
        "Day 3/4: full job lifecycle endpoints with authentication and ownership enforcement."
    ),
)

# ── CORS middleware ───────────────────────────────────────────────────────────
# Browsers enforce the Same-Origin Policy: a page at origin A cannot read a
# response from origin B unless origin B explicitly allows it via
# Access-Control-Allow-Origin headers.
#
# The React frontend (Vite dev server) runs at http://localhost:5173.
# Without this middleware, every API call from the frontend would be blocked.
#
# allow_credentials=True:
#   Required when the frontend sends an Authorization header (Cognito JWT)
#   from a different origin. Browsers refuse to send credentials cross-origin
#   unless the server explicitly permits it.
#
# expose_headers=["Content-Range"]:
#   React Admin reads the Content-Range header for pagination
#   (format: "jobs 0-24/150"). Without this, the browser hides the header
#   from JavaScript and pagination breaks silently.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Range"],
)


# ── Helper: serialise a Job ORM object to a JobResponse dict ──────────────────
def _job_to_response(job) -> JobResponse:
    """
    Convert a SQLAlchemy Job ORM object to a JobResponse Pydantic model.

    WHAT:
        Maps ORM attribute names to API response field names. The field
        mapping is intentional: the ORM uses 'id' while the API exposes
        'job_id' (clearer for clients; avoids confusion with other 'id' fields).
        Timestamps are converted from Python datetime objects to ISO-8601 strings.

    WHY A HELPER (not model_validate directly):
        The ORM's primary key is `job.id`; the schema field is `job_id`.
        `model_validate` with `from_attributes=True` would try to read
        `job.job_id`, which doesn't exist. This helper bridges the naming gap.

        Timestamps: SQLAlchemy returns Python datetime objects for TIMESTAMP
        columns. Pydantic v2 serialises datetime to ISO-8601 automatically,
        but the schema uses Optional[str] to stay SQLite-compatible (SQLite
        returns string timestamps in unit tests). The `.isoformat()` call
        ensures consistent ISO-8601 output regardless of the DB backend.
    """
    return JobResponse(
        job_id=job.id,
        status=job.status,
        original_filename=job.original_filename,
        user_email=job.user_email,
        file_size_bytes=job.file_size_bytes,
        row_count=job.row_count,
        column_count=job.column_count,
        column_names=job.column_names if job.column_names else None,
        duplicate_rows_removed=job.duplicate_rows_removed,
        failure_reason=job.failure_reason,
        created_at=job.created_at.isoformat() if job.created_at else None,
        updated_at=job.updated_at.isoformat() if job.updated_at else None,
    )


def _job_to_admin_response(job) -> AdminJobResponse:
    """
    Convert a Job ORM object to an AdminJobResponse — includes internal fields.

    The admin response adds original_s3_key, processed_s3_key, and user_id
    that are omitted from the user-facing JobResponse. These are needed for
    admin debugging but must never appear in user-facing responses.
    """
    return AdminJobResponse(
        job_id=job.id,
        user_id=job.user_id,
        user_email=job.user_email,
        status=job.status,
        original_filename=job.original_filename,
        original_s3_key=job.original_s3_key,
        processed_s3_key=job.processed_s3_key,
        file_size_bytes=job.file_size_bytes,
        row_count=job.row_count,
        column_count=job.column_count,
        column_names=job.column_names if job.column_names else None,
        duplicate_rows_removed=job.duplicate_rows_removed,
        failure_reason=job.failure_reason,
        created_at=job.created_at.isoformat() if job.created_at else None,
        updated_at=job.updated_at.isoformat() if job.updated_at else None,
    )


# ── Health endpoint ───────────────────────────────────────────────────────────
@app.get("/health")
def health_check() -> dict:
    """
    Health check endpoint.

    Returns the service status and current runtime environment.
    Used by load balancer health probes and manual smoke tests.

    Always returns 200 — if this endpoint fails, the process itself is broken.
    """
    return {
        "status": "ok",
        "environment": os.getenv("ENVIRONMENT", "production"),
    }


# ── POST /jobs ─────────────────────────────────────────────────────────────────
# This is the REST-conventional alias for POST /uploads/intent.
# Both routes call the same underlying logic.
# Exposed via the /jobs prefix to follow "resource-first" REST naming.
@app.post("/jobs", response_model=UploadIntentResponse, status_code=201)
def create_upload_job(
    body: PostJobRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> UploadIntentResponse:
    """
    Create a new upload job and return a presigned S3 PUT URL.

    WHAT:
        REST-conventional alias for POST /uploads/intent (see below).
        Returns HTTP 201 Created (more semantically correct than 200 for
        resource creation). The response body is identical to /uploads/intent.

    Returns 201 with UploadIntentResponse on success.
    Returns 422 if the request body fails Pydantic validation.
    """
    return _create_upload_job_logic(
        user_id=current_user.user_id,
        user_email=body.user_email,
        original_filename=body.original_filename,
        db=db,
    )


# ── POST /uploads/intent ──────────────────────────────────────────────────────
# Original route name from Day 1/2. Kept for backwards compatibility and
# because all existing tests and the task spec reference this path.
@app.post("/uploads/intent", response_model=UploadIntentResponse)
def upload_intent(
    body: UploadIntentRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> UploadIntentResponse:
    """
    Create a new upload job and return a presigned S3 PUT URL.

    WHAT:
        Step 1 of the two-step upload flow. Creates a Job in pending_upload
        status and returns a presigned URL the browser can use to upload
        the CSV file directly to S3.

    HOW (execution order):
        1. Extract authenticated user identity (via get_current_user dependency).
        2. Create Job in PostgreSQL (status=pending_upload).
        3. Build the deterministic S3 key: uploads/{user_id}/{job_id}/original.csv
        4. Generate a presigned PUT URL for that key (expires in 15 min, ≤50 MiB).
        5. Return {job_id, upload_url, s3_key}.

    Returns 200 with UploadIntentResponse on success.
    Returns 401 if not authenticated (production only).
    Returns 422 if the request body fails Pydantic validation.
    """
    return _create_upload_job_logic(
        user_id=current_user.user_id,
        user_email=body.user_email,
        original_filename=body.original_filename,
        db=db,
    )


def _create_upload_job_logic(
    user_id: str,
    user_email: str,
    original_filename: str,
    db: Session,
) -> UploadIntentResponse:
    """
    Shared implementation for POST /jobs and POST /uploads/intent.

    WHAT:
        Both routes create a job and presigned URL using the same logic.
        Extracting to a private function avoids duplication and keeps
        both route handlers readable.

    WHY PLACEHOLDER FOR original_s3_key:
        We need the job.id to construct the S3 key, but create_job() generates
        the UUID in Python — the UUID is available immediately after create_job()
        returns, before the DB INSERT is committed. So we pass a placeholder
        first, then update the field using the returned job.id.

        This works because:
        1. create_job() generates uuid4() in Python (not DB server_default).
        2. db.flush() writes the INSERT within the current transaction.
        3. job.original_s3_key is updated in Python memory.
        4. get_db() commits at request end, persisting both values together.
    """
    # Create the Job record with a placeholder S3 key (updated below).
    job = create_job(
        db=db,
        user_id=user_id,
        user_email=user_email,
        original_filename=original_filename,
        original_s3_key="__placeholder__",
    )

    # Build the deterministic S3 key using the job_id just generated.
    # Format: uploads/{user_id}/{job_id}/original.csv
    # Including user_id enables per-user IAM scoping (AD-20).
    # Including job_id makes the key globally unique across all jobs.
    s3_key = f"uploads/{user_id}/{job.id}/original.csv"
    job.original_s3_key = s3_key

    bucket = os.getenv("S3_BUCKET_NAME", "csvforge-files-dev")
    upload_url = generate_presigned_upload_url(bucket=bucket, key=s3_key)

    return UploadIntentResponse(
        job_id=job.id,
        upload_url=upload_url,
        s3_key=s3_key,
    )


# ── POST /uploads/{job_id}/complete ───────────────────────────────────────────
@app.post("/uploads/{job_id}/complete", response_model=UploadCompleteResponse)
def upload_complete(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> UploadCompleteResponse:
    """
    Verify S3 upload and transition job to queued.

    WHAT:
        Step 2 of the two-step upload flow. Called by the browser after it has
        successfully PUT the CSV to S3. Verifies the upload occurred, enforces
        the size limit, queues the job in the DB, and publishes an SQS message.

    HOW (execution order):
        1. Fetch the job to get its S3 key (uses admin fetch to get the key,
           ownership is enforced in transition_to_queued WHERE clause).
        2. Verify the S3 object exists (browser must have uploaded the file).
        3. Verify file size ≤ 50 MiB via S3 HeadObject (authoritative, AD-27).
        4. transition_to_queued() — conditional UPDATE enforcing ownership.
        5. send_job_message() — publish to SQS.
        6. Return {job_id, status: "queued"}.

    Returns 200 with UploadCompleteResponse on success.
    Returns 404 if the job is not found or S3 object is missing.
    Returns 422 if the uploaded file exceeds 50 MiB.
    Returns 409 if the job is not in pending_upload status or wrong owner.
    """
    # Fetch the job to get its S3 key. We use get_job_by_id (admin-style,
    # no ownership check at this step) because we need the key before we
    # can verify ownership. Ownership is enforced below in transition_to_queued().
    job = get_job_by_id(db=db, job_id=job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    bucket = os.getenv("S3_BUCKET_NAME", "csvforge-files-dev")
    s3_key = job.original_s3_key

    # Verify the S3 object exists (proves the browser uploaded the file).
    if not object_exists(bucket=bucket, key=s3_key):
        raise HTTPException(status_code=404, detail="Uploaded file not found in S3")

    # Verify file size ≤ 50 MiB.
    # ContentLength from HeadObject is the authoritative size (AD-27).
    # Client-declared file_size_bytes from the intent request is untrusted.
    MAX_UPLOAD_BYTES = 52_428_800  # 50 MiB
    actual_size = get_object_size(bucket=bucket, key=s3_key)
    if actual_size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"File size {actual_size} bytes exceeds maximum allowed 50 MiB",
        )

    # Conditional UPDATE: pending_upload → queued, WHERE user_id = current_user.user_id.
    # Enforces ownership atomically: returns False if the job belongs to a
    # different user OR is already past pending_upload.
    transitioned = transition_to_queued(
        db=db, job_id=job_id, user_id=current_user.user_id
    )
    if not transitioned:
        # 409 Conflict: job is not in pending_upload state, or wrong owner.
        # We don't distinguish the two cases — doing so would leak information.
        raise HTTPException(
            status_code=409,
            detail="Job is not in pending_upload status or does not belong to this user",
        )

    # Publish to SQS (AD-29: DB update before SQS publish — known limitation).
    queue_url = os.getenv(
        "SQS_QUEUE_URL",
        "https://sqs.ap-south-1.amazonaws.com/ACCOUNT_ID_PLACEHOLDER/csvforge-jobs-dev",
    )
    send_job_message(queue_url=queue_url, job_id=job_id, s3_key=s3_key)

    return UploadCompleteResponse(job_id=job_id, status="queued")


# ── GET /jobs ──────────────────────────────────────────────────────────────────
@app.get("/jobs", response_model=JobListResponse)
def list_jobs(
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> JobListResponse:
    """
    List all jobs for the authenticated user, ordered newest-first.

    WHAT:
        Returns all jobs owned by the caller. Excludes pending_upload jobs
        (AD-13: these are hidden until the upload is confirmed).

    AUTHORIZATION:
        Ownership is enforced at the query level:
        `WHERE user_id = current_user.user_id` — never returns other users' jobs.
        A client cannot bypass this by passing a different user_id — the user_id
        comes from the validated JWT (via get_current_user), not from the request.

    RESPONSE:
        {
          "jobs": [{job_id, status, original_filename, ...}, ...],
          "total": 5
        }
        The `total` count helps the frontend show "You have N jobs."

    Returns 200 with JobListResponse.
    Returns 401 if not authenticated (production only).
    """
    jobs = list_jobs_for_user(db=db, user_id=current_user.user_id)
    return JobListResponse(
        jobs=[_job_to_response(j) for j in jobs],
        total=len(jobs),
    )


# ── GET /jobs/{job_id} ────────────────────────────────────────────────────────
@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> JobResponse:
    """
    Get the current state of a specific job.

    WHAT:
        Returns the full status and results of one job. The caller must own
        the job (AD-13: 404 returned for both "not found" and "wrong owner").

    AUTHORIZATION:
        Uses get_job_by_id_and_user() which filters WHERE id=job_id AND
        user_id=current_user.user_id. A job that exists but belongs to
        another user returns 404 (not 403) to prevent enumeration.

    WHY 404 FOR WRONG OWNER:
        If we returned 403, an attacker who guesses a job UUID would learn
        "this job exists, it belongs to someone else." With 404, they learn
        nothing. A UUID is not a security mechanism, but 404-for-wrong-owner
        is defence in depth.

    STATUS VALUES:
        queued     → job is in the queue, worker hasn't started yet
        processing → worker is actively processing the CSV
        completed  → processing done; row_count, column_names etc. are populated
        failed     → permanent failure; failure_reason explains why

    Returns 200 with JobResponse.
    Returns 404 if the job is not found or does not belong to the caller.
    Returns 401 if not authenticated (production only).
    """
    job = get_job_by_id_and_user(
        db=db, job_id=job_id, user_id=current_user.user_id
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_response(job)


# ── GET /jobs/{job_id}/download ───────────────────────────────────────────────
@app.get("/jobs/{job_id}/download", response_model=JobDownloadResponse)
def download_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> JobDownloadResponse:
    """
    Get a presigned S3 download URL for the processed CSV output.

    WHAT:
        Generates a time-limited presigned GET URL for the processed output
        file. The browser can use this URL to download the cleaned CSV directly
        from S3 — no FastAPI proxy needed.

    PRECONDITIONS:
        The job must exist, belong to the caller, and be in 'completed' status.
        If the job is still processing, a 409 is returned with guidance.

    WHY A PRESIGNED URL (not streaming via FastAPI):
        The processed file can be up to 50 MiB. Streaming through FastAPI/Lambda:
        - Consumes Lambda execution time and memory.
        - Runs into API Gateway's 10 MiB response payload limit.
        A presigned URL offloads delivery to S3's HTTP infrastructure — free,
        scalable, and no Lambda involvement. The URL is valid for 15 minutes (AD-02).

    SECURITY:
        - Ownership is enforced: the caller must own the job.
        - The processed_s3_key comes from the database (set by the worker using
          the DB job record's user_id), not from client input.
        - The presigned URL is a bearer token: anyone with the URL can download
          the file for 15 minutes. The frontend should initiate the download
          immediately after receiving it.

    SUGGESTED FILENAME:
        The filename field in the response is derived from the original filename
        with a "_processed" suffix:
            "my_data.csv" → "my_data_processed.csv"
        The browser uses this for the Save As dialog.

    Returns 200 with JobDownloadResponse.
    Returns 404 if the job is not found or does not belong to the caller.
    Returns 409 if the job is not in 'completed' status.
    Returns 401 if not authenticated (production only).
    """
    from backend.models.job import JobStatus

    job = get_job_by_id_and_user(
        db=db, job_id=job_id, user_id=current_user.user_id
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != JobStatus.COMPLETED.value:
        raise HTTPException(
            status_code=409,
            detail=f"Job is not completed (current status: {job.status}). "
                   "Wait for processing to finish before downloading.",
        )

    if not job.processed_s3_key:
        # This should be unreachable (completed jobs always have processed_s3_key),
        # but guard defensively against data inconsistency.
        raise HTTPException(
            status_code=500,
            detail="Job is completed but processed file key is missing. "
                   "Please contact support.",
        )

    bucket = os.getenv("S3_BUCKET_NAME", "csvforge-files-dev")
    download_url = generate_presigned_download_url(
        bucket=bucket,
        key=job.processed_s3_key,
        expiry_seconds=900,
    )

    # Derive a user-friendly download filename.
    # "my_data.csv" → "my_data_processed.csv"
    # "report"      → "report_processed"  (no extension — unlikely but safe)
    original_name = job.original_filename
    if "." in original_name:
        base, ext = original_name.rsplit(".", 1)
        filename = f"{base}_processed.{ext}"
    else:
        filename = f"{original_name}_processed"

    return JobDownloadResponse(
        job_id=job_id,
        download_url=download_url,
        filename=filename,
        expires_in_seconds=900,
    )


# ── GET /admin/jobs ────────────────────────────────────────────────────────────
@app.get("/admin/jobs")
def admin_list_jobs(
    response: Response,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
    _start: int = Query(default=0, alias="_start", ge=0),
    _end: int = Query(default=25, alias="_end", ge=1),
    _sort: str = Query(default="created_at", alias="_sort"),
    _order: str = Query(default="DESC", alias="_order"),
    status: Optional[str] = Query(default=None),
) -> list:
    """
    List all jobs for all users — admin view.

    WHAT:
        Returns a paginated list of ALL jobs in the system. Designed for the
        React Admin frontend which uses the ra-data-simple-rest convention.

    AUTHORIZATION:
        Requires the caller to be in the 'csvforge-admins' Cognito group
        (enforced by the require_admin dependency). Returns 403 if not admin.

    PAGINATION (React Admin ra-data-simple-rest convention):
        Query params: _start (offset), _end (exclusive end).
        Limit = _end - _start.
        Response header: Content-Range: jobs {_start}-{_end-1}/{total}
        React Admin reads Content-Range to render page controls.

        Example: _start=0, _end=10 → rows 0-9, Content-Range: jobs 0-9/42

    STATUS FILTER:
        Optional `status` query parameter filters to one job state:
        GET /admin/jobs?status=failed → only failed jobs.

    RETURNS:
        A JSON array of AdminJobResponse objects (includes internal S3 keys).

    Returns 200 with array of AdminJobResponse.
    Returns 403 if not in the admin group.
    Returns 401 if not authenticated.
    """
    skip = _start
    limit = max(1, _end - _start)  # ensure at least 1 row requested

    jobs = list_all_jobs(db=db, skip=skip, limit=limit, status_filter=status)
    total = count_all_jobs(db=db, status_filter=status)

    # Set Content-Range header for React Admin pagination.
    # Format: "jobs {first_index}-{last_index}/{total}"
    # first_index = _start, last_index = _start + len(jobs) - 1
    last_index = skip + len(jobs) - 1 if jobs else skip
    response.headers["Content-Range"] = f"jobs {skip}-{last_index}/{total}"

    # Also set Access-Control-Expose-Headers so the browser can read Content-Range
    # from a cross-origin request. (CORSMiddleware expose_headers handles this
    # globally; this line is a belt-and-suspenders reminder for documentation.)
    return [_job_to_admin_response(j) for j in jobs]
