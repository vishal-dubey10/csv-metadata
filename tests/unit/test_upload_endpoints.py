"""
Unit tests for the upload endpoints — POST /uploads/intent and POST /uploads/{job_id}/complete.

WHAT:
    Tests the full two-step upload flow through FastAPI's TestClient with
    moto's @mock_aws intercepting all boto3 calls. No real AWS is needed.

WHY MOTO + TESTCLIENT:
    The upload endpoints call S3 (presigned URL generation, object_exists,
    get_object_size) and SQS (send_job_message). Without moto, these calls
    would hit real AWS — slow, expensive, and non-deterministic.

    moto intercepts boto3 at the HTTP level and returns realistic AWS responses
    using in-memory state. TestClient runs the FastAPI ASGI app in-process —
    no real HTTP server needed. Combined, they let us test the entire HTTP
    request → FastAPI routing → repository → S3/SQS path deterministically.

IMPORTANT SETUP — WHY STATICPOOL:
    SQLite in-memory databases are per-connection: each new SQLite connection
    opens a fresh, empty database. SQLAlchemy's default pool for SQLite is
    SingletonThreadPool — one connection per thread.

    The FastAPI TestClient runs the ASGI app in a background thread (via anyio).
    Without special handling, that background thread would open a NEW SQLite
    connection (a new, empty in-memory database) — even though the test thread
    already called create_all() on a different connection in its own thread.
    Result: "no such table: jobs" error in the route handler.

    The fix: use sqlalchemy.pool.StaticPool to force ALL connections (from any
    thread) to reuse a single underlying connection object. StaticPool makes ALL
    sessions use exactly one in-memory database — the same one where create_all()
    initialized the schema.

    This is standard practice for FastAPI + SQLite in-memory unit tests.

DATABASE ISOLATION:
    The endpoint_db fixture (defined in this file) creates a fresh in-memory
    database per test: create_all() before the test, drop_all() after. Each test
    starts with a clean, empty jobs table. It does NOT use the conftest sqlite_db
    fixture because the conftest uses SingletonThreadPool (no StaticPool).

IDENTITY HEADERS:
    POST /uploads/intent and /uploads/{job_id}/complete read user identity from
    X-Dev-User-Id and X-Dev-User-Email headers (local dev bypass, AD-19).
    Tests pass these headers explicitly to control the user_id used for each test.

PATTERN NOTES:
    - Class-based grouping: class TestXxx: / def test_yyy(self, ...)
    - @mock_aws wraps each test method (not the class) — moto resets state at
      the decorator boundary, and class-level mocks would share state between tests.
    - The endpoint_db fixture is function-scoped (one fresh DB per test).
    - We override the get_db() dependency inside _make_client to wire the TestClient
      to use the same single in-memory SQLite connection as the test.

See backend/main.py for the full inline documentation of each endpoint.
See docs/DECISIONS.md for AD-02, AD-13, AD-19, AD-27, AD-29.
"""

import os

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db.job_repository import get_job_by_id
from backend.models.job import JobStatus


# ── AWS / env configuration constants ─────────────────────────────────────────
# Use the same values as .env so moto mock resources match what the code expects.
BUCKET_NAME = "csvforge-files-dev"
QUEUE_NAME = "csvforge-jobs-dev"
REGION = "ap-south-1"

# Default test user identity (matches the header defaults in the route).
TEST_USER_ID = "test-user-001"
TEST_USER_EMAIL = "test@example.com"


# ── Per-test SQLite session with StaticPool ────────────────────────────────────

@pytest.fixture()
def endpoint_db():
    """
    SQLite in-memory session for upload endpoint tests, using StaticPool.

    WHY STATICPOOL (not conftest's sqlite_db which uses SingletonThreadPool):
        FastAPI's TestClient runs the ASGI app in a background thread via anyio.
        SQLAlchemy's default pool for in-memory SQLite is SingletonThreadPool,
        which gives each THREAD its own connection:

          - Main test thread → connection C1 → in-memory database DB1.
          - TestClient background thread → connection C2 → in-memory database DB2.

        create_all() runs on C1 (DB1). When the route handler runs in C2's
        session, DB2 is empty → "no such table: jobs".

        StaticPool forces ALL calls to engine.connect() (from any thread) to
        return the SAME underlying connection object. This means all sessions
        share one in-memory database — the one where create_all() created the
        tables. The route handler and the test assertions see the same rows.

    Each test gets a fresh database (create_all before yield, drop_all after).
    The endpoint_db fixture is independent of conftest's sqlite_db.
    """
    from backend.db.connection import Base
    from backend.models.job import Job  # noqa: F401 — registers Job in Base.metadata

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        # StaticPool: every call to engine.connect() returns the SAME connection.
        # No new connections are ever created — all threads share this one.
        poolclass=StaticPool,
    )
    TestSession = sessionmaker(bind=test_engine, expire_on_commit=False)
    Base.metadata.create_all(bind=test_engine)
    db = TestSession()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=test_engine)


def _make_client(db: Session) -> TestClient:
    """
    Create a FastAPI TestClient wired to the provided SQLite session.

    WHY OVERRIDE get_db():
        FastAPI's get_db() dependency creates a new SQLAlchemy session from
        the real database engine. In tests, we want route handlers to use the
        same SQLite session as the test so we can assert on the same data.

        FastAPI's dependency override mechanism (app.dependency_overrides) is
        the correct way to swap out a dependency for tests.

    WHY YIELD THE SAME db OBJECT (not create a new session):
        The test and the route handler must share one session (and therefore
        one SQLite connection). Even with StaticPool, if we created a new
        Session inside override_get_db, it would be a different Session object.
        Sharing the same Session object ensures the route handler's writes are
        immediately visible to the test's post-request assertions.
    """
    from backend.db.connection import get_db
    from backend.main import app

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _setup_s3(bucket: str = BUCKET_NAME, region: str = REGION) -> None:
    """
    Create an S3 bucket inside the active @mock_aws context.

    IMPORTANT: must be called INSIDE an active @mock_aws context.
    moto resets all state when the @mock_aws decorator boundary is crossed.
    """
    s3 = boto3.client("s3", region_name=region)
    s3.create_bucket(
        Bucket=bucket,
        CreateBucketConfiguration={"LocationConstraint": region},
    )


def _setup_sqs(queue_name: str = QUEUE_NAME, region: str = REGION) -> str:
    """
    Create an SQS queue inside the active @mock_aws context.

    Returns the queue URL (needed to set SQS_QUEUE_URL env var so the
    route can find the queue).
    """
    sqs = boto3.client("sqs", region_name=region)
    response = sqs.create_queue(QueueName=queue_name)
    return response["QueueUrl"]


def _put_object_to_s3(bucket: str, key: str, content: bytes = b"name,age\nAlice,30\n") -> None:
    """
    Upload an object directly to mocked S3.

    Used in /complete tests to simulate the browser having uploaded the file
    before calling the complete endpoint.
    """
    s3 = boto3.client("s3", region_name=REGION)
    s3.put_object(Bucket=bucket, Key=key, Body=content)


# ── POST /uploads/intent ───────────────────────────────────────────────────────

class TestUploadIntent:
    """
    Tests for POST /uploads/intent — the first step of the upload flow.

    The endpoint must:
    - Return 200 with job_id, upload_url, and s3_key.
    - Create a Job in the DB with status=pending_upload.
    - Generate a presigned URL that starts with https://.
    - Build an s3_key that contains the user_id and job_id.
    """

    @mock_aws
    def test_returns_200(self, endpoint_db):
        """POST /uploads/intent returns HTTP 200 on valid request."""
        _setup_s3()
        os.environ["S3_BUCKET_NAME"] = BUCKET_NAME
        client = _make_client(endpoint_db)
        response = client.post(
            "/uploads/intent",
            json={"user_email": TEST_USER_EMAIL, "original_filename": "data.csv", "file_size_bytes": 1024},
            headers={"X-Dev-User-Id": TEST_USER_ID, "X-Dev-User-Email": TEST_USER_EMAIL},
        )
        assert response.status_code == 200

    @mock_aws
    def test_response_has_job_id(self, endpoint_db):
        """Response body must include a non-empty job_id string."""
        _setup_s3()
        os.environ["S3_BUCKET_NAME"] = BUCKET_NAME
        client = _make_client(endpoint_db)
        response = client.post(
            "/uploads/intent",
            json={"user_email": TEST_USER_EMAIL, "original_filename": "data.csv", "file_size_bytes": 1024},
            headers={"X-Dev-User-Id": TEST_USER_ID, "X-Dev-User-Email": TEST_USER_EMAIL},
        )
        body = response.json()
        assert "job_id" in body
        assert len(body["job_id"]) > 0

    @mock_aws
    def test_response_has_upload_url(self, endpoint_db):
        """Response body must include a presigned upload_url starting with https://."""
        _setup_s3()
        os.environ["S3_BUCKET_NAME"] = BUCKET_NAME
        client = _make_client(endpoint_db)
        response = client.post(
            "/uploads/intent",
            json={"user_email": TEST_USER_EMAIL, "original_filename": "data.csv", "file_size_bytes": 1024},
            headers={"X-Dev-User-Id": TEST_USER_ID, "X-Dev-User-Email": TEST_USER_EMAIL},
        )
        body = response.json()
        assert "upload_url" in body
        assert body["upload_url"].startswith("https://")

    @mock_aws
    def test_response_has_s3_key(self, endpoint_db):
        """Response body must include an s3_key under the uploads/ prefix."""
        _setup_s3()
        os.environ["S3_BUCKET_NAME"] = BUCKET_NAME
        client = _make_client(endpoint_db)
        response = client.post(
            "/uploads/intent",
            json={"user_email": TEST_USER_EMAIL, "original_filename": "data.csv", "file_size_bytes": 1024},
            headers={"X-Dev-User-Id": TEST_USER_ID, "X-Dev-User-Email": TEST_USER_EMAIL},
        )
        body = response.json()
        assert "s3_key" in body
        assert body["s3_key"].startswith("uploads/")

    @mock_aws
    def test_s3_key_contains_user_id(self, endpoint_db):
        """The s3_key must embed the user_id from X-Dev-User-Id."""
        _setup_s3()
        os.environ["S3_BUCKET_NAME"] = BUCKET_NAME
        client = _make_client(endpoint_db)
        response = client.post(
            "/uploads/intent",
            json={"user_email": TEST_USER_EMAIL, "original_filename": "data.csv", "file_size_bytes": 1024},
            headers={"X-Dev-User-Id": TEST_USER_ID, "X-Dev-User-Email": TEST_USER_EMAIL},
        )
        body = response.json()
        assert TEST_USER_ID in body["s3_key"]

    @mock_aws
    def test_s3_key_contains_job_id(self, endpoint_db):
        """The s3_key must embed the job_id returned in the same response."""
        _setup_s3()
        os.environ["S3_BUCKET_NAME"] = BUCKET_NAME
        client = _make_client(endpoint_db)
        response = client.post(
            "/uploads/intent",
            json={"user_email": TEST_USER_EMAIL, "original_filename": "data.csv", "file_size_bytes": 1024},
            headers={"X-Dev-User-Id": TEST_USER_ID, "X-Dev-User-Email": TEST_USER_EMAIL},
        )
        body = response.json()
        assert body["job_id"] in body["s3_key"]

    @mock_aws
    def test_creates_job_in_pending_upload_status(self, endpoint_db):
        """
        After POST /uploads/intent, the job must exist in the DB with
        status=pending_upload. The job is invisible to list_jobs_for_user
        (AD-13) but discoverable via get_job_by_id (admin/worker access).
        """
        _setup_s3()
        os.environ["S3_BUCKET_NAME"] = BUCKET_NAME
        client = _make_client(endpoint_db)
        response = client.post(
            "/uploads/intent",
            json={"user_email": TEST_USER_EMAIL, "original_filename": "data.csv", "file_size_bytes": 1024},
            headers={"X-Dev-User-Id": TEST_USER_ID, "X-Dev-User-Email": TEST_USER_EMAIL},
        )
        job_id = response.json()["job_id"]
        job = get_job_by_id(endpoint_db, job_id)
        assert job is not None
        assert job.status == JobStatus.PENDING_UPLOAD.value

    @mock_aws
    def test_missing_required_field_returns_422(self, endpoint_db):
        """
        Pydantic validates the request body — missing original_filename returns 422.
        The schema validation fires before the route handler executes.
        """
        _setup_s3()
        os.environ["S3_BUCKET_NAME"] = BUCKET_NAME
        client = _make_client(endpoint_db)
        response = client.post(
            "/uploads/intent",
            json={"user_email": TEST_USER_EMAIL, "file_size_bytes": 1024},
            # original_filename is missing
            headers={"X-Dev-User-Id": TEST_USER_ID, "X-Dev-User-Email": TEST_USER_EMAIL},
        )
        assert response.status_code == 422


# ── POST /uploads/{job_id}/complete ───────────────────────────────────────────

class TestUploadComplete:
    """
    Tests for POST /uploads/{job_id}/complete — the second step of the upload flow.

    The endpoint must:
    - Return 200 with status='queued' when the file is present and within size.
    - Transition the job from pending_upload → queued in the DB.
    - Return 404 if the S3 object is not found (upload never happened).
    - Return 404 if the job_id does not exist.
    - Return 422 if the uploaded file exceeds 50 MiB.
    - Return 409 if called twice (job is already in queued status).
    """

    def _create_intent(self, client, user_id: str = TEST_USER_ID) -> dict:
        """Helper: call /uploads/intent and return the response body dict."""
        response = client.post(
            "/uploads/intent",
            json={"user_email": TEST_USER_EMAIL, "original_filename": "data.csv", "file_size_bytes": 1024},
            headers={"X-Dev-User-Id": user_id, "X-Dev-User-Email": TEST_USER_EMAIL},
        )
        assert response.status_code == 200, f"Intent setup failed: {response.json()}"
        return response.json()

    @mock_aws
    def test_returns_200_with_file_present(self, endpoint_db):
        """
        When the file exists in S3 and the job is in pending_upload status,
        /complete returns 200.
        """
        queue_url = _setup_sqs()
        _setup_s3()
        os.environ["S3_BUCKET_NAME"] = BUCKET_NAME
        os.environ["SQS_QUEUE_URL"] = queue_url
        client = _make_client(endpoint_db)

        intent = self._create_intent(client)
        _put_object_to_s3(BUCKET_NAME, intent["s3_key"])

        response = client.post(
            f"/uploads/{intent['job_id']}/complete",
            headers={"X-Dev-User-Id": TEST_USER_ID},
        )
        assert response.status_code == 200

    @mock_aws
    def test_response_status_is_queued(self, endpoint_db):
        """Response body status field must be 'queued' on success."""
        queue_url = _setup_sqs()
        _setup_s3()
        os.environ["S3_BUCKET_NAME"] = BUCKET_NAME
        os.environ["SQS_QUEUE_URL"] = queue_url
        client = _make_client(endpoint_db)

        intent = self._create_intent(client)
        _put_object_to_s3(BUCKET_NAME, intent["s3_key"])

        response = client.post(
            f"/uploads/{intent['job_id']}/complete",
            headers={"X-Dev-User-Id": TEST_USER_ID},
        )
        assert response.json()["status"] == "queued"

    @mock_aws
    def test_job_transitions_to_queued_in_db(self, endpoint_db):
        """
        After a successful /complete call, the job status in the database
        must be 'queued'. This verifies the actual DB state, not just the
        HTTP response body.
        """
        queue_url = _setup_sqs()
        _setup_s3()
        os.environ["S3_BUCKET_NAME"] = BUCKET_NAME
        os.environ["SQS_QUEUE_URL"] = queue_url
        client = _make_client(endpoint_db)

        intent = self._create_intent(client)
        _put_object_to_s3(BUCKET_NAME, intent["s3_key"])

        client.post(
            f"/uploads/{intent['job_id']}/complete",
            headers={"X-Dev-User-Id": TEST_USER_ID},
        )

        job = get_job_by_id(endpoint_db, intent["job_id"])
        assert job.status == JobStatus.QUEUED.value

    @mock_aws
    def test_s3_object_missing_returns_404(self, endpoint_db):
        """
        If the browser never uploaded the file to S3, /complete returns 404.
        The endpoint verifies S3 presence before transitioning the job.
        """
        queue_url = _setup_sqs()
        _setup_s3()
        os.environ["S3_BUCKET_NAME"] = BUCKET_NAME
        os.environ["SQS_QUEUE_URL"] = queue_url
        client = _make_client(endpoint_db)

        intent = self._create_intent(client)
        # Deliberately skip the S3 put — file is NOT present in S3.

        response = client.post(
            f"/uploads/{intent['job_id']}/complete",
            headers={"X-Dev-User-Id": TEST_USER_ID},
        )
        assert response.status_code == 404

    @mock_aws
    def test_nonexistent_job_id_returns_404(self, endpoint_db):
        """
        A job_id that does not exist in the database must return 404.
        Catches attempts to complete a job that was never created.
        """
        queue_url = _setup_sqs()
        _setup_s3()
        os.environ["S3_BUCKET_NAME"] = BUCKET_NAME
        os.environ["SQS_QUEUE_URL"] = queue_url
        client = _make_client(endpoint_db)

        response = client.post(
            "/uploads/non-existent-job-id/complete",
            headers={"X-Dev-User-Id": TEST_USER_ID},
        )
        assert response.status_code == 404

    @mock_aws
    def test_file_too_large_returns_422(self, endpoint_db):
        """
        If the uploaded file exceeds 50 MiB (52,428,800 bytes), /complete
        returns 422. The size check uses S3 HeadObject (authoritative), not
        the client-declared file_size_bytes from the intent (AD-27).
        """
        queue_url = _setup_sqs()
        _setup_s3()
        os.environ["S3_BUCKET_NAME"] = BUCKET_NAME
        os.environ["SQS_QUEUE_URL"] = queue_url
        client = _make_client(endpoint_db)

        intent = self._create_intent(client)
        # Upload a file exactly 1 byte over the 50 MiB limit.
        oversized_content = b"x" * (52_428_800 + 1)
        _put_object_to_s3(BUCKET_NAME, intent["s3_key"], oversized_content)

        response = client.post(
            f"/uploads/{intent['job_id']}/complete",
            headers={"X-Dev-User-Id": TEST_USER_ID},
        )
        assert response.status_code == 422

    @mock_aws
    def test_second_complete_call_returns_409(self, endpoint_db):
        """
        Calling /complete twice for the same job returns 409 on the second call.

        After the first /complete, the job transitions to 'queued'.
        The second call finds the job in 'queued' (not 'pending_upload'),
        so transition_to_queued() returns False → 409 Conflict.

        This is the idempotency guard for the complete endpoint (AD-29).
        409 Conflict = "the request is valid but the server state has changed."
        """
        queue_url = _setup_sqs()
        _setup_s3()
        os.environ["S3_BUCKET_NAME"] = BUCKET_NAME
        os.environ["SQS_QUEUE_URL"] = queue_url
        client = _make_client(endpoint_db)

        intent = self._create_intent(client)
        _put_object_to_s3(BUCKET_NAME, intent["s3_key"])

        # First call — succeeds, transitions job to queued.
        first = client.post(
            f"/uploads/{intent['job_id']}/complete",
            headers={"X-Dev-User-Id": TEST_USER_ID},
        )
        assert first.status_code == 200

        # Second call — job is now 'queued', not 'pending_upload'.
        # transition_to_queued WHERE clause won't match → returns False → 409.
        second = client.post(
            f"/uploads/{intent['job_id']}/complete",
            headers={"X-Dev-User-Id": TEST_USER_ID},
        )
        assert second.status_code == 409
