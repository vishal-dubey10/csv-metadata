"""
Unit tests for backend/auth.py — the authentication and identity dependency.

WHAT IS TESTED:
  - Local dev bypass (ENVIRONMENT=local): X-Dev-User-Id header → user_id
  - Local dev admin flag: X-Dev-Is-Admin header → is_admin
  - Production path: JWT claims from request scope → user_id + is_admin
  - Production path: missing claims → 401
  - Production path: missing sub claim → 401
  - cognito:groups claim parsing: string (API GW format) and list formats
  - require_admin(): 403 for non-admin, pass-through for admin
  - CurrentUser dataclass: fields and defaults

MOCKING STRATEGY:
  No database, no AWS, no HTTP client.
  All tests call get_current_user() and require_admin() directly as async
  functions, with a mock Request object.

ENVIRONMENT ISOLATION:
  Tests that change ENVIRONMENT must restore it in a finally block to avoid
  polluting other tests in the same pytest session.

See also:
  tests/unit/test_job_endpoints.py — endpoint-level ownership tests
  backend/auth.py                  — the module under test
  docs/DECISIONS.md AD-19          — API Gateway JWT claims path
  docs/DECISIONS.md AD-23          — Cognito access token claims
"""

import asyncio
import os
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request

from backend.auth import CurrentUser, get_current_user, require_admin


# ── Job creation helpers (used by TestCrossUserOwnershipViaAuth) ──────────────

def _create_queued_job_for_auth_test(db, user_id: str, filename: str = "data.csv") -> str:
    """
    Create a job in 'queued' status for a given user. Returns the job_id.
    Defined here (not imported from test_job_endpoints) to keep test_auth.py
    self-contained and avoid circular test module dependencies.
    """
    from backend.db.job_repository import create_job
    from backend.models.job import JobStatus

    job = create_job(db, user_id, f"{user_id}@test.com", filename,
                     f"uploads/{user_id}/placeholder/original.csv")
    db.flush()
    job.original_s3_key = f"uploads/{user_id}/{job.id}/original.csv"
    job.status = JobStatus.QUEUED.value
    db.commit()
    return job.id


def _create_completed_job_for_auth_test(db, user_id: str) -> str:
    """
    Create a job in 'completed' status. Returns the job_id.
    column_names=None to avoid SQLite ARRAY binding issue.
    """
    from backend.db.job_repository import complete_job

    job_id = _create_queued_job_for_auth_test(db, user_id)
    complete_job(db, job_id=job_id,
                 processed_s3_key=f"processed/{user_id}/{job_id}/result.csv",
                 file_size_bytes=512, row_count=5, column_count=2,
                 column_names=None, duplicate_rows_removed=0)
    db.commit()
    return job_id


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_request(aws_event: dict = None) -> MagicMock:
    """
    Build a minimal mock Request object.

    In production, Mangum (0.22.0) places the full API Gateway event at
    request.scope["aws.event"]. Tests that exercise the production path
    must provide this structure. Tests for the local bypass do not need it.
    """
    req = MagicMock(spec=Request)
    req.scope = {"aws.event": aws_event} if aws_event is not None else {}
    return req


def _claims_event(claims: dict) -> dict:
    """Build the nested API Gateway event structure that carries JWT claims."""
    return {
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": claims
                }
            }
        }
    }


def run(coro):
    """
    Run a coroutine synchronously (pytest doesn't run async by default).

    Uses asyncio.run() rather than asyncio.get_event_loop().run_until_complete()
    because Python 3.10+ deprecated implicit event loop creation in non-main
    threads, and Python 3.14 removed the implicit loop entirely.
    asyncio.run() always creates a fresh event loop for the coroutine and
    closes it cleanly — correct for test contexts.
    """
    return asyncio.run(coro)


# ── CurrentUser dataclass ─────────────────────────────────────────────────────

class TestCurrentUser:
    """Tests for the CurrentUser dataclass."""

    def test_required_field_user_id(self):
        """CurrentUser requires user_id."""
        user = CurrentUser(user_id="abc-123")
        assert user.user_id == "abc-123"

    def test_is_admin_defaults_false(self):
        """is_admin defaults to False — most users are not admins."""
        user = CurrentUser(user_id="abc-123")
        assert user.is_admin is False

    def test_email_defaults_none(self):
        """email is optional and defaults to None."""
        user = CurrentUser(user_id="abc-123")
        assert user.email is None

    def test_all_fields_set(self):
        """All fields can be set explicitly."""
        user = CurrentUser(user_id="abc-123", is_admin=True, email="u@x.com")
        assert user.user_id == "abc-123"
        assert user.is_admin is True
        assert user.email == "u@x.com"


# ── Local dev bypass ──────────────────────────────────────────────────────────

class TestLocalDevBypass:
    """
    Tests for the local bypass path (ENVIRONMENT=local).

    The bypass reads identity from X-Dev-User-Id and X-Dev-Is-Admin headers.
    API Gateway and JWT validation are not involved.
    """

    def setup_method(self):
        """Ensure ENVIRONMENT=local for every test in this class."""
        os.environ["ENVIRONMENT"] = "local"

    def teardown_method(self):
        """Restore ENVIRONMENT to avoid polluting other tests."""
        os.environ["ENVIRONMENT"] = "local"

    def test_returns_user_id_from_header(self):
        """get_current_user returns user_id from X-Dev-User-Id header."""
        req = _mock_request()
        user = run(get_current_user(req, x_dev_user_id="user-xyz", x_dev_user_email="", x_dev_is_admin="false"))
        assert user.user_id == "user-xyz"

    def test_empty_user_id_falls_back_to_default(self):
        """
        Empty X-Dev-User-Id header falls back to 'dev-user-001'.
        This matches the default set in the upload endpoints so local
        testing without headers still works end-to-end.
        """
        req = _mock_request()
        user = run(get_current_user(req, x_dev_user_id="", x_dev_user_email="", x_dev_is_admin="false"))
        assert user.user_id == "dev-user-001"

    def test_is_admin_false_by_default(self):
        """X-Dev-Is-Admin defaults to false — regular users are not admins."""
        req = _mock_request()
        user = run(get_current_user(req, x_dev_user_id="user-1", x_dev_user_email="", x_dev_is_admin="false"))
        assert user.is_admin is False

    def test_is_admin_true_from_header(self):
        """X-Dev-Is-Admin: true grants admin access in local bypass."""
        req = _mock_request()
        user = run(get_current_user(req, x_dev_user_id="user-1", x_dev_user_email="", x_dev_is_admin="true"))
        assert user.is_admin is True

    def test_is_admin_accepts_various_truthy_strings(self):
        """
        The is_admin flag accepts 'true', '1', and 'yes' as truthy values.
        These are common boolean-like string representations that might
        appear in environment headers or test scripts.
        """
        req = _mock_request()
        for truthy in ("true", "True", "TRUE", "1", "yes"):
            user = run(get_current_user(req, x_dev_user_id="u", x_dev_user_email="", x_dev_is_admin=truthy))
            assert user.is_admin is True, f"Expected is_admin=True for X-Dev-Is-Admin={truthy!r}"

    def test_is_admin_false_for_falsy_strings(self):
        """Anything other than truthy strings results in is_admin=False."""
        req = _mock_request()
        for falsy in ("false", "False", "0", "no", "", "admin"):
            user = run(get_current_user(req, x_dev_user_id="u", x_dev_user_email="", x_dev_is_admin=falsy))
            assert user.is_admin is False, f"Expected is_admin=False for X-Dev-Is-Admin={falsy!r}"

    def test_email_captured_from_header(self):
        """X-Dev-User-Email is stored on CurrentUser.email (informational only)."""
        req = _mock_request()
        user = run(get_current_user(req, x_dev_user_id="user-1", x_dev_user_email="u@x.com", x_dev_is_admin="false"))
        assert user.email == "u@x.com"

    def test_does_not_read_aws_event_in_local_mode(self):
        """
        In local mode, JWT claims from the request scope are ignored.
        Even if scope["aws.event"] is present with conflicting data,
        the X-Dev-User-Id header takes precedence.
        This ensures local tests can't accidentally test production logic.
        """
        # aws.event has a different user_id than the header
        aws_event = _claims_event({"sub": "cognito-sub-from-event"})
        req = _mock_request(aws_event)
        user = run(get_current_user(req, x_dev_user_id="header-user-id", x_dev_user_email="", x_dev_is_admin="false"))
        assert user.user_id == "header-user-id", (
            "Local bypass must use header user_id, not JWT claims from scope"
        )


# ── Production JWT claims path ────────────────────────────────────────────────

class TestProductionJWTClaims:
    """
    Tests for the production path (ENVIRONMENT=production).

    FastAPI reads JWT claims from the API Gateway event injected by Mangum
    at scope["aws.event"]["requestContext"]["authorizer"]["jwt"]["claims"].
    API Gateway validates the token; FastAPI only reads the pre-validated claims.
    """

    def setup_method(self):
        os.environ["ENVIRONMENT"] = "production"

    def teardown_method(self):
        os.environ["ENVIRONMENT"] = "local"

    def test_reads_sub_as_user_id(self):
        """sub claim is the authoritative user identity (AD-23)."""
        aws_event = _claims_event({"sub": "cognito-uuid-abc-123"})
        req = _mock_request(aws_event)
        user = run(get_current_user(req, x_dev_user_id="", x_dev_user_email="", x_dev_is_admin=""))
        assert user.user_id == "cognito-uuid-abc-123"

    def test_missing_claims_raises_401(self):
        """
        If JWT claims are absent (no API Gateway, direct Lambda invocation),
        the endpoint returns 401 Unauthorized.

        This guards against misconfigured API Gateway where the JWT authoriser
        is not attached to the route — the Lambda would be invocable without auth.
        """
        req = _mock_request({})  # aws.event present but no requestContext
        with pytest.raises(HTTPException) as exc_info:
            run(get_current_user(req, x_dev_user_id="", x_dev_user_email="", x_dev_is_admin=""))
        assert exc_info.value.status_code == 401

    def test_no_aws_event_in_scope_raises_401(self):
        """
        If scope["aws.event"] is missing entirely (non-Lambda ASGI context),
        the endpoint returns 401.
        """
        req = _mock_request()  # no aws.event at all
        with pytest.raises(HTTPException) as exc_info:
            run(get_current_user(req, x_dev_user_id="", x_dev_user_email="", x_dev_is_admin=""))
        assert exc_info.value.status_code == 401

    def test_missing_sub_claim_raises_401(self):
        """
        Claims dict present but missing the sub claim → 401.
        sub is the only authoritative identity field (AD-23).
        An empty sub would create a user_id="" which must not be allowed.
        """
        aws_event = _claims_event({"token_use": "access"})  # no sub
        req = _mock_request(aws_event)
        with pytest.raises(HTTPException) as exc_info:
            run(get_current_user(req, x_dev_user_id="", x_dev_user_email="", x_dev_is_admin=""))
        assert exc_info.value.status_code == 401

    def test_cognito_groups_string_grants_admin(self):
        """
        API Gateway passes cognito:groups as a space-delimited string.
        A user in 'csvforge-admins' group gets is_admin=True.
        """
        aws_event = _claims_event({
            "sub": "user-1",
            "cognito:groups": "csvforge-admins other-group",
        })
        req = _mock_request(aws_event)
        user = run(get_current_user(req, x_dev_user_id="", x_dev_user_email="", x_dev_is_admin=""))
        assert user.is_admin is True

    def test_cognito_groups_list_grants_admin(self):
        """
        Some Cognito configurations pass cognito:groups as a Python list.
        The code handles both string and list formats defensively.
        """
        aws_event = _claims_event({
            "sub": "user-1",
            "cognito:groups": ["csvforge-admins", "other-group"],
        })
        req = _mock_request(aws_event)
        user = run(get_current_user(req, x_dev_user_id="", x_dev_user_email="", x_dev_is_admin=""))
        assert user.is_admin is True

    def test_no_groups_claim_not_admin(self):
        """
        A user with no cognito:groups claim is not an admin.
        Most users don't belong to any Cognito group.
        """
        aws_event = _claims_event({"sub": "regular-user"})
        req = _mock_request(aws_event)
        user = run(get_current_user(req, x_dev_user_id="", x_dev_user_email="", x_dev_is_admin=""))
        assert user.is_admin is False

    def test_wrong_group_not_admin(self):
        """
        Being in a different Cognito group does not grant admin access.
        Only membership in 'csvforge-admins' grants is_admin=True.
        """
        aws_event = _claims_event({
            "sub": "user-1",
            "cognito:groups": "some-other-group another-group",
        })
        req = _mock_request(aws_event)
        user = run(get_current_user(req, x_dev_user_id="", x_dev_user_email="", x_dev_is_admin=""))
        assert user.is_admin is False

    def test_production_ignores_dev_headers(self):
        """
        In production mode, X-Dev-User-Id is completely ignored.
        The user identity comes only from the JWT claims.
        This prevents the local dev bypass from being exploited in production.
        """
        aws_event = _claims_event({"sub": "jwt-user"})
        req = _mock_request(aws_event)
        user = run(get_current_user(
            req,
            x_dev_user_id="attacker-header-injection",
            x_dev_user_email="",
            x_dev_is_admin="true",  # attempting admin escalation via header
        ))
        assert user.user_id == "jwt-user", (
            "Production path must use JWT sub claim, not X-Dev-User-Id header"
        )
        assert user.is_admin is False, (
            "Production path must not grant admin from X-Dev-Is-Admin header"
        )


# ── require_admin dependency ──────────────────────────────────────────────────

class TestRequireAdmin:
    """
    Tests for the require_admin() FastAPI dependency.

    require_admin wraps get_current_user and adds an admin-group check.
    Returns CurrentUser on success, raises 403 on failure.
    """

    def test_passes_through_admin_user(self):
        """An admin user passes through require_admin without error."""
        admin = CurrentUser(user_id="admin-1", is_admin=True)
        result = run(require_admin(admin))
        assert result.user_id == "admin-1"

    def test_raises_403_for_non_admin(self):
        """
        A non-admin raises HTTPException 403 Forbidden.

        Why 403 (not 401)?
        401 Unauthorized = not authenticated (no token or invalid token).
        403 Forbidden = authenticated but lacks permission.
        The user IS authenticated; they simply lack admin group membership.
        """
        non_admin = CurrentUser(user_id="user-1", is_admin=False)
        with pytest.raises(HTTPException) as exc_info:
            run(require_admin(non_admin))
        assert exc_info.value.status_code == 403

    def test_403_message_mentions_admin(self):
        """The 403 response detail message mentions 'admin' for clarity."""
        non_admin = CurrentUser(user_id="user-1", is_admin=False)
        with pytest.raises(HTTPException) as exc_info:
            run(require_admin(non_admin))
        assert "admin" in exc_info.value.detail.lower()

    def test_admin_flag_preserved_in_return(self):
        """require_admin returns the full CurrentUser, not just user_id."""
        admin = CurrentUser(user_id="admin-1", is_admin=True, email="a@x.com")
        result = run(require_admin(admin))
        assert result.is_admin is True
        assert result.email == "a@x.com"


# ── Security: cross-user ownership via endpoint ───────────────────────────────

class TestCrossUserOwnershipViaAuth:
    """
    Integration-style security tests verifying that two different authenticated
    users cannot access each other's resources through the API.

    These tests exercise the full path:
    auth dependency → get_job_by_id_and_user → 404 for wrong owner

    This is the most important security property in the system (AD-13, section 10):
    "Prevent IDOR vulnerabilities."
    """

    def setup_method(self):
        """Set up a fresh in-memory database per test."""
        os.environ["ENVIRONMENT"] = "local"

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from backend.db.connection import Base, get_db
        from backend.models.job import Job  # noqa: F401
        from backend.main import app

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
        self.db = TestSession()
        self.engine = engine

        def override_get_db():
            yield self.db

        app.dependency_overrides[get_db] = override_get_db

        from fastapi.testclient import TestClient
        self.client = TestClient(app)

    def teardown_method(self):
        """Clean up database and dependency overrides."""
        from backend.main import app
        self.db.close()
        from backend.db.connection import Base
        Base.metadata.drop_all(self.engine)
        app.dependency_overrides.clear()

    def test_user_b_cannot_get_user_a_job(self):
        """
        IDOR test: user-b requests a job owned by user-a.
        Must receive 404, not the job details.
        """
        job_id = _create_queued_job_for_auth_test(self.db, "user-a")

        response = self.client.get(
            f"/jobs/{job_id}",
            headers={"X-Dev-User-Id": "user-b"},
        )
        assert response.status_code == 404, (
            "IDOR: user-b must not be able to read user-a's job"
        )

    def test_user_b_cannot_download_user_a_result(self):
        """
        IDOR test: user-b cannot download user-a's processed file.
        """
        job_id = _create_completed_job_for_auth_test(self.db, "user-a")

        response = self.client.get(
            f"/jobs/{job_id}/download",
            headers={"X-Dev-User-Id": "user-b"},
        )
        assert response.status_code == 404, (
            "IDOR: user-b must not be able to download user-a's processed file"
        )

    def test_list_endpoint_never_returns_other_users_jobs(self):
        """
        GET /jobs for user-b must never include user-a's jobs.
        """
        for i in range(3):
            _create_queued_job_for_auth_test(self.db, "user-a", f"file-{i}.csv")

        response = self.client.get(
            "/jobs",
            headers={"X-Dev-User-Id": "user-b"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0, (
            "user-b's job list must be empty — user-a's jobs must not appear"
        )
        assert data["jobs"] == []

    def test_guessing_job_id_does_not_reveal_existence(self):
        """
        If user-b knows user-a's job UUID, they receive 404 — not 403.
        Cannot distinguish "does not exist" from "exists but not yours."
        """
        job_id = _create_queued_job_for_auth_test(self.db, "user-a")

        response = self.client.get(
            f"/jobs/{job_id}",
            headers={"X-Dev-User-Id": "user-b"},
        )
        assert response.status_code == 404
        detail = response.json().get("detail", "")
        assert "not found" in detail.lower(), (
            "Error message must say 'not found', not 'forbidden' or 'unauthorized'"
        )
