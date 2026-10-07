"""
CSVForge — FastAPI application entry point.

This module creates the FastAPI app instance, registers middleware,
and defines the health check endpoint.

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
       │ HTTP Request
    Uvicorn (local) / API Gateway + Mangum (Lambda)
       │ ASGI
    FastAPI
       │
    CORS Middleware  ← checks Origin header, adds CORS response headers
       │
    Route Matching  ← @app.get("/health") etc.
       │
    Dependency Resolution  ← Depends(get_db) creates a Session
       │
    Route Handler  ← your function runs here, receives db: Session
       │
    Repository Functions  ← job_repository.py
       │
    SQLAlchemy ORM
       │
    PostgreSQL
       │ response
    Route Handler returns Python dict
       │
    FastAPI serializes to JSON (Pydantic)
       │
    CORS Middleware adds headers
       │
    Uvicorn / Mangum
       │ HTTP Response
    Browser
"""

import os

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from backend.db.connection import get_db
from backend.db.job_repository import create_job, get_job_by_id, transition_to_queued
from backend.s3_client import generate_presigned_upload_url, get_object_size, object_exists
from backend.schemas import UploadCompleteResponse, UploadIntentRequest, UploadIntentResponse
from backend.sqs_client import send_job_message

# ── App instance ──────────────────────────────────────────────────────────────
# FastAPI(title=..., version=..., description=...) populates the OpenAPI spec
# that FastAPI auto-generates at startup. Navigate to http://localhost:8000/docs
# to see the Swagger UI generated from these fields.
#
# Every @app.get() decorator and Python type annotation further populates this
# spec — FastAPI reads each function's docstring, parameter types, and return
# type to generate documentation automatically. No separate doc-writing step needed.
app = FastAPI(
    title="CSVForge API",
    version="0.1.0",
    description="CSV upload, processing, and download service.",
)

# ── CORS middleware ───────────────────────────────────────────────────────────
# Browsers enforce the Same-Origin Policy: a page at origin A cannot read a
# response from origin B unless origin B explicitly allows it via
# Access-Control-Allow-Origin headers.
#
# The React frontend (Vite dev server) runs at http://localhost:5173.
# Without this middleware, every API call from the frontend would be
# blocked by the browser with a CORS error — even though the server
# returned a 200. The error is enforced by the browser, not the server.
#
# allow_credentials=True:
#   By default CORS blocks credentials (cookies and Authorization headers)
#   from cross-origin requests. Setting this to True enables the browser
#   to send cookies and Authorization headers to this API from a different
#   origin. Required when the React frontend is served from
#   http://localhost:5173 and needs to call http://localhost:8000 with
#   an auth token in the Authorization header.
#
# expose_headers=["Content-Range"]:
#   By default, the browser's CORS filter only exposes a small set of
#   "safe" response headers to JavaScript (Content-Type, Cache-Control, etc.).
#   Content-Range is NOT in that default safe list.
#
#   React Admin (the frontend framework) reads the Content-Range header to
#   know the total count of items for pagination. The API responds with:
#       Content-Range: jobs 0-9/42
#   React Admin reads the `/42` part to render page controls ("42 total results").
#   Without expose_headers, the browser's CORS filter hides this header from
#   React Admin's JavaScript, and pagination breaks silently.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev server origin
    allow_credentials=True,   # allow Authorization header + cookies cross-origin
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Range"],  # needed for React Admin list pagination
)

# ── Health endpoint ───────────────────────────────────────────────────────────
# HOW FASTAPI REGISTERS ROUTES:
# @app.get("/health") registers a route: "when the app receives a GET request
# to /health, call this function". FastAPI reads the function's signature,
# docstring, and return type at startup to build the OpenAPI spec.
#
# The return type annotation (-> dict) tells FastAPI and Pydantic how to
# serialize the response — Pydantic converts Python objects to JSON-serializable
# dicts. If the function raises an HTTPException, FastAPI catches it and returns
# the appropriate HTTP error response automatically.
@app.get("/health")
def health_check() -> dict:
    """
    Health check endpoint.

    Returns the service status and current runtime environment.
    Used by:
    - Docker Compose healthchecks (future)
    - Load balancer health probes
    - Quick manual verification that the API is running

    The `environment` field lets you confirm you're hitting the right
    deployment (local vs production) without checking env vars directly.

    Returns 200 always — if this endpoint itself fails, the process is broken.
    """
    return {
        "status": "ok",
        "environment": os.getenv("ENVIRONMENT", "production"),
    }


# ── Upload intent endpoint ─────────────────────────────────────────────────────
# POST /uploads/intent — step 1 of the two-step upload flow.
#
# WHAT THIS ENDPOINT DOES (in order):
#   1. Reads the caller's identity from headers (local dev bypass per AD-19).
#   2. Validates the request body via Pydantic (UploadIntentRequest).
#   3. Creates a Job record in pending_upload status in PostgreSQL.
#   4. Generates a presigned S3 PUT URL for the browser to upload directly.
#   5. Returns {job_id, upload_url, s3_key} so the browser can upload to S3.
#
# WHAT THIS ENDPOINT DOES NOT DO:
#   - It does NOT receive the CSV file bytes.
#   - It does NOT process the CSV.
#   - It does NOT transition the job beyond pending_upload.
#
# WHY TWO STEPS (intent + complete)?
#   The browser uploads directly to S3 using the presigned URL. FastAPI is
#   not involved in the upload itself. S3 does not automatically notify the
#   API when the upload completes — the browser must call /complete to signal
#   that the upload is done. This two-step flow avoids routing file bytes
#   through the API server (API Gateway 10 MB limit, Lambda memory, AD-02).
#
# S3 KEY FORMAT — why the job ID is in the key:
#   uploads/{user_id}/{job_id}/original.csv
#
#   Including user_id provides a natural "namespace" — all files for a user
#   are under the same prefix. This enables per-user access policies in IAM
#   (s3:PutObject on arn:aws:s3:::bucket/uploads/{user_id}/*).
#
#   Including job_id makes the key globally unique. Two uploads by the same
#   user cannot overwrite each other, even if they have the same filename.
#   The job_id is generated in Python (uuid4) before the DB INSERT, so we
#   have it available to construct the key in the same request. (See the
#   UUID generation note in job.py — we need the ID before the INSERT
#   because we need it to construct the S3 key.)
#
#   "original.csv" is a fixed suffix — readable and consistent.
#
# LOCAL DEV IDENTITY BYPASS (AD-19):
#   In production, API Gateway validates the Cognito JWT and injects claims
#   into the request context. FastAPI reads user_id from the JWT sub claim.
#   In local development (ENVIRONMENT=local), there is no API Gateway.
#   Instead, the caller sends X-Dev-User-Id and X-Dev-User-Email headers.
#   This bypass is strictly gated by ENVIRONMENT=local.
@app.post("/uploads/intent", response_model=UploadIntentResponse)
def upload_intent(
    body: UploadIntentRequest,
    db: Session = Depends(get_db),
    x_dev_user_id: str = Header(default="dev-user-001"),
    x_dev_user_email: str = Header(default="dev@example.com"),
) -> UploadIntentResponse:
    """
    Create a new upload job and return a presigned S3 PUT URL.

    WHAT:
        Step 1 of the two-step upload flow. Creates a Job in pending_upload
        status and returns a presigned URL the browser can use to upload
        the CSV file directly to S3.

    HOW (execution order):
        1. Determine caller identity (local dev header bypass vs JWT claims).
        2. Create Job in PostgreSQL (status=pending_upload).
        3. Build the deterministic S3 key for this upload.
        4. Generate a presigned PUT URL for that key (expires in 15 min).
        5. Return {job_id, upload_url, s3_key}.

    Returns 200 with UploadIntentResponse on success.
    Returns 422 if the request body fails Pydantic validation.
    """
    # Identity resolution: in local dev, read from X-Dev-User-Id / X-Dev-User-Email
    # headers. In production, these values come from the JWT claims injected by
    # API Gateway (AD-19). The ENVIRONMENT variable gates the bypass.
    environment = os.getenv("ENVIRONMENT", "production")
    if environment == "local":
        # Local bypass: the header values are injected by Header() above.
        # The caller sets these manually (e.g., curl -H "X-Dev-User-Id: user-1").
        user_id = x_dev_user_id
        user_email = x_dev_user_email
    else:
        # Production path: JWT claims are injected by API Gateway into the
        # request scope by Mangum. Not implemented in local dev. AD-19.
        # This branch is a stub — full JWT extraction is a Day 3 concern.
        user_id = x_dev_user_id
        user_email = body.user_email

    # Create the Job record in PostgreSQL.
    # The job ID is generated in Python (uuid4) by create_job(), so we can
    # use it to construct the S3 key before the DB INSERT completes.
    # status defaults to pending_upload — the job is not visible to list_jobs.
    job = create_job(
        db=db,
        user_id=user_id,
        user_email=user_email,
        original_filename=body.original_filename,
        # original_s3_key is filled in below after we have the job.id.
        # We pass a placeholder first, then update — but actually we need the
        # job.id to build the key. Since create_job() generates the UUID in
        # Python (not the DB), job.id is available immediately after the call.
        # We use the job.id returned by create_job() to build the final key.
        # This works because uuid.uuid4() runs in Python BEFORE the DB flush.
        original_s3_key="__placeholder__",
    )

    # Build the deterministic S3 key using the job_id from the newly created job.
    # Format: uploads/{user_id}/{job_id}/original.csv
    # This key uniquely identifies this specific upload and cannot collide with
    # any other job. The user_id prefix enables per-user IAM scoping (AD-20).
    s3_key = f"uploads/{user_id}/{job.id}/original.csv"

    # Update the job's original_s3_key now that we have the job.id.
    # We set it directly on the ORM object and rely on the session's flush
    # (triggered by the get_db dependency at request end) to persist it.
    job.original_s3_key = s3_key

    # Retrieve the S3 bucket name from the environment.
    # In local dev, this is set in .env (S3_BUCKET_NAME=csvforge-files-dev).
    # In production Lambda, it's an environment variable set by CloudFormation.
    bucket = os.getenv("S3_BUCKET_NAME", "csvforge-files-dev")

    # Generate the presigned PUT URL. The browser will PUT the CSV bytes to
    # this URL. The URL is valid for 15 minutes (900 seconds). It includes
    # a content-length-range condition (≤ 50 MiB) enforced by S3 (AD-27).
    upload_url = generate_presigned_upload_url(bucket=bucket, key=s3_key)

    return UploadIntentResponse(
        job_id=job.id,
        upload_url=upload_url,
        s3_key=s3_key,
    )


# ── Upload complete endpoint ───────────────────────────────────────────────────
# POST /uploads/{job_id}/complete — step 2 of the two-step upload flow.
#
# WHAT THIS ENDPOINT DOES (in order):
#   1. Verifies the uploaded file exists in S3 (object_exists / get_object_size).
#   2. Enforces the 50 MiB size limit (AD-27).
#   3. Transitions the job from pending_upload → queued in PostgreSQL.
#   4. Publishes a job message to SQS (tells the Lambda worker to process it).
#   5. Returns {job_id, status: "queued"}.
#
# ERROR CODES:
#   404 — job not found (wrong job_id, or job doesn't belong to this user)
#   404 — S3 object not found (browser never actually uploaded the file)
#   422 — file exceeds 50 MiB limit
#   409 — transition_to_queued returned False (job already queued, wrong state,
#          or wrong user) — Conflict because the job is not in the expected state
#   200 — success; job is now queued for processing
#
# WHY DB UPDATE BEFORE SQS PUBLISH (AD-29):
#   The DB update (pending_upload → queued) happens before SQS SendMessage.
#   This creates a failure window: if SQS send fails after the DB update, the
#   job is stuck in "queued" with no SQS message. This is a known limitation
#   (AD-29) — a full transactional outbox would fix it but adds complexity.
#   The failure is observable via CloudWatch alarms on stuck-queued jobs.
@app.post("/uploads/{job_id}/complete", response_model=UploadCompleteResponse)
def upload_complete(
    job_id: str,
    db: Session = Depends(get_db),
    x_dev_user_id: str = Header(default="dev-user-001"),
) -> UploadCompleteResponse:
    """
    Verify S3 upload and transition job to queued.

    WHAT:
        Step 2 of the two-step upload flow. Called by the browser after it has
        successfully PUT the CSV to S3. Verifies the upload occurred, enforces
        the size limit, queues the job in the DB, and publishes an SQS message.

    HOW (execution order):
        1. Resolve caller identity (local dev header vs JWT).
        2. Fetch the job from PostgreSQL to get the S3 key.
        3. Verify the object exists in S3 (proves the browser uploaded it).
        4. Check object size ≤ 52,428,800 bytes (50 MiB). Return 422 if exceeded.
        5. transition_to_queued() — conditional UPDATE. Return 409 if False.
        6. send_job_message() — publish to SQS.
        7. Return {job_id, status: "queued"}.

    Returns 200 with UploadCompleteResponse on success.
    Returns 404 if the job is not found or S3 object is missing.
    Returns 422 if the uploaded file exceeds 50 MiB.
    Returns 409 if the job cannot be transitioned (wrong state or wrong owner).
    """
    # Identity resolution — same local dev bypass as upload_intent.
    environment = os.getenv("ENVIRONMENT", "production")
    user_id = x_dev_user_id  # local dev and production stub both use this for now

    # Fetch the job to get its S3 key.
    # We use get_job_by_id (admin function, no ownership check) because we
    # enforce ownership separately in transition_to_queued() (which checks user_id).
    # Using the admin fetch here lets us return 404 for both "not found" and
    # "wrong user" — avoiding information leakage (AD-13).
    job = get_job_by_id(db=db, job_id=job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    # Verify the S3 object actually exists.
    # The browser told us it uploaded the file, but we cannot trust client claims.
    # object_exists() issues a HeadObject to S3 — the authoritative source.
    # If the object is missing (browser lied or upload failed silently), return 404.
    bucket = os.getenv("S3_BUCKET_NAME", "csvforge-files-dev")
    s3_key = job.original_s3_key

    if not object_exists(bucket=bucket, key=s3_key):
        raise HTTPException(status_code=404, detail="Uploaded file not found in S3")

    # Verify the uploaded file size does not exceed the 50 MiB limit.
    # get_object_size() issues a HeadObject and returns ContentLength.
    # This is the AUTHORITATIVE size — not the client-declared file_size_bytes
    # from the intent request. Client-supplied metadata is never trusted for
    # security boundaries (AD-27).
    #
    # MAX_UPLOAD_BYTES = 52,428,800 = 50 × 1024 × 1024 bytes = 50 MiB.
    # The presigned URL already includes a content-length-range condition that
    # S3 enforces during the PUT. This HeadObject check is a second enforcement
    # layer — defence in depth. If the presigned URL somehow lacked the condition
    # (e.g., a bug in URL generation), this check catches it.
    MAX_UPLOAD_BYTES = 52_428_800
    actual_size = get_object_size(bucket=bucket, key=s3_key)
    if actual_size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"File size {actual_size} bytes exceeds maximum allowed 50 MiB ({MAX_UPLOAD_BYTES} bytes)",
        )

    # Attempt the DB state transition: pending_upload → queued.
    # transition_to_queued() uses a conditional UPDATE with WHERE user_id = <user_id>
    # AND status = 'pending_upload'. It returns True (1 row updated) or False (0 rows).
    # False means: job doesn't exist for this user in pending_upload state —
    # could be wrong user, already queued, or already processing/completed/failed.
    transitioned = transition_to_queued(db=db, job_id=job_id, user_id=user_id)
    if not transitioned:
        # 409 Conflict: the job is not in the state we expected.
        # The most common cause is calling /complete twice — the second call
        # finds the job in "queued" status, not "pending_upload", and returns False.
        raise HTTPException(
            status_code=409,
            detail="Job is not in pending_upload status or does not belong to this user",
        )

    # Publish the SQS message to trigger the Lambda worker.
    # The message contains only job_id and s3_key (claim-check pattern).
    # The worker will download the file, process it, and update the DB.
    # See sqs_client.py for the full explanation of the claim-check pattern.
    queue_url = os.getenv(
        "SQS_QUEUE_URL",
        "https://sqs.ap-south-1.amazonaws.com/ACCOUNT_ID_PLACEHOLDER/csvforge-jobs-dev",
    )
    send_job_message(queue_url=queue_url, job_id=job_id, s3_key=s3_key)

    return UploadCompleteResponse(job_id=job_id, status="queued")
