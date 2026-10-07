"""
Unit tests for the Lambda handler (worker/handler.py).

These tests mock all external dependencies (S3, SQS, PostgreSQL) to exercise
the handler's orchestration logic in isolation.

What's tested:
- Happy path: valid CSV → complete_job called with correct metadata
- Invalid CSV: fail_job called, handler does NOT raise
- Transient failure: S3 download raises → fail_job called, exception re-raised
- Job already in terminal state: claim returns False → returns early, no S3/csv calls
- Security: user_id sourced from DB job record, NOT from SQS message body

Mocking strategy:
- S3 is mocked via moto @mock_aws — we use a real (mocked) S3 bucket so
  download_file() and upload_file() work without changing their implementation.
- PostgreSQL is mocked via unittest.mock.patch — we patch the repository
  functions (claim_job_for_processing, get_job_by_id, complete_job, fail_job)
  and SessionLocal to avoid needing a real database.
- parse_job_message is the real implementation (just does json.loads — no I/O).
- Env vars (S3_BUCKET_NAME, S3_OUTPUT_BUCKET) are overridden per-test via
  patch.dict(os.environ, ...) so tests are isolated from changes made by other
  test files in the same pytest session.

SQS event format used in all tests:
    {
        "Records": [{
            "body": json.dumps({
                "job_id": "test-job-id",
                "user_id": "sqs-user-id",      ← untrusted, should be ignored
                "s3_key": "uploads/user/test-job-id/file.csv",
                "original_filename": "test.csv",
                "file_size_bytes": 100,
            })
        }]
    }

Note: 'user_id' in the SQS body is present but must be ignored by the handler.
The handler reads user_id from the DB job record only (security invariant).
"""

import json
import os
import unittest.mock
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

# ── Environment setup — must happen before importing handler ──────────────────
# ENVIRONMENT=local prevents connection.py from calling Secrets Manager.
# This must be set before any backend.* import resolves.
os.environ.setdefault("ENVIRONMENT", "local")

from worker.handler import lambda_handler  # noqa: E402

# ── Test constants ────────────────────────────────────────────────────────────
_JOB_ID = "test-job-id"
_SQS_USER_ID = "sqs-user-id"       # present in the SQS message body (untrusted)
_DB_USER_ID = "db-user-id"          # from the DB record (authoritative)
_S3_KEY = f"uploads/{_SQS_USER_ID}/{_JOB_ID}/file.csv"
_INPUT_BUCKET = "test-input-bucket"
_OUTPUT_BUCKET = "test-output-bucket"

# Env var overrides applied per-test via patch.dict to isolate from other test files.
# test_upload_endpoints.py sets S3_BUCKET_NAME='csvforge-files-dev' inside its tests.
# Using patch.dict ensures each handler test sees the correct bucket names regardless
# of what earlier tests in the session set.
_TEST_ENV = {
    "S3_BUCKET_NAME": _INPUT_BUCKET,
    "S3_OUTPUT_BUCKET": _OUTPUT_BUCKET,
}

# A minimal valid CSV with one duplicate row for deduplication testing.
# Rows 2 and 3 are identical → duplicate_rows_removed should be 1.
_VALID_CSV = "name,age\nAlice,30\nAlice,30\n"

# An empty CSV body — triggers InvalidCSVError("File is empty") in parse_and_analyse.
_EMPTY_CSV = ""


def _make_sqs_event(job_id=_JOB_ID, s3_key=_S3_KEY, user_id=_SQS_USER_ID):
    """
    Build a synthetic SQS Lambda event dict.

    The 'user_id' field in the body is intentionally present to verify that
    the handler ignores it in favour of the DB job record's user_id.
    """
    return {
        "Records": [
            {
                "body": json.dumps({
                    "job_id": job_id,
                    "user_id": user_id,
                    "s3_key": s3_key,
                    "original_filename": "test.csv",
                    "file_size_bytes": len(_VALID_CSV),
                })
            }
        ]
    }


def _make_db_job(user_id=_DB_USER_ID, job_id=_JOB_ID):
    """
    Build a MagicMock that mimics the Job ORM object returned by get_job_by_id.

    Only the attributes accessed by the handler are set:
      - user_id: used to construct the processed S3 key
      - id: the job identifier
    """
    job = MagicMock()
    job.user_id = user_id
    job.id = job_id
    return job


# ── Shared patch targets ──────────────────────────────────────────────────────
# Patching the functions as imported in worker.handler so the patches apply
# to the names the handler uses at runtime (not the names in their source modules).
_PATCH_CLAIM = "worker.handler.claim_job_for_processing"
_PATCH_GET_JOB = "worker.handler.get_job_by_id"
_PATCH_COMPLETE = "worker.handler.complete_job"
_PATCH_FAIL = "worker.handler.fail_job"
_PATCH_SESSION = "worker.handler.SessionLocal"
_PATCH_DOWNLOAD = "worker.handler.download_file"
_PATCH_UPLOAD = "worker.handler.upload_file"


class TestHappyPath:
    """
    Happy path: a valid CSV in S3 is processed successfully.

    Verifies:
    - complete_job is called (job transitions to completed)
    - complete_job receives the correct metadata fields
    - The processed S3 key uses db_job.user_id (not the SQS body's user_id)
    - duplicate_rows_removed reflects the deduplication count
    """

    @mock_aws
    @patch.dict(os.environ, _TEST_ENV)
    def test_complete_job_called_with_correct_metadata(self, tmp_path):
        """
        Happy path: valid CSV with one duplicate row → complete_job called
        with row_count=1, column_count=2, duplicate_rows_removed=1.

        We put a real CSV into a moto-mocked S3 bucket and let download_file()
        and upload_file() talk to the mocked S3 — no patching of those functions.
        The DB layer (SessionLocal, claim_job_for_processing, get_job_by_id,
        complete_job) is patched via unittest.mock.
        """
        # Set up moto S3 buckets.
        s3 = boto3.client("s3", region_name="ap-south-1")
        s3.create_bucket(
            Bucket=_INPUT_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
        )
        s3.create_bucket(
            Bucket=_OUTPUT_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
        )
        # Put the valid CSV into the input bucket.
        s3.put_object(Bucket=_INPUT_BUCKET, Key=_S3_KEY, Body=_VALID_CSV.encode())

        mock_db = MagicMock()
        db_job = _make_db_job(user_id=_DB_USER_ID)
        mock_complete = MagicMock()
        mock_fail = MagicMock()

        with (
            patch(_PATCH_SESSION, return_value=mock_db),
            patch(_PATCH_CLAIM, return_value=True),
            patch(_PATCH_GET_JOB, return_value=db_job),
            patch(_PATCH_COMPLETE, mock_complete),
            patch(_PATCH_FAIL, mock_fail),
        ):
            lambda_handler(_make_sqs_event(), context=None)

        # complete_job must have been called exactly once.
        assert mock_complete.call_count == 1

        # Inspect the keyword arguments passed to complete_job.
        call_kwargs = mock_complete.call_args[1]

        assert call_kwargs["job_id"] == _JOB_ID
        # _VALID_CSV has 2 data rows (both 'Alice,30'), one duplicate removed → 1 clean row.
        assert call_kwargs["row_count"] == 1
        assert call_kwargs["column_count"] == 2
        assert call_kwargs["column_names"] == ["name", "age"]
        assert call_kwargs["duplicate_rows_removed"] == 1
        # file_size_bytes should be a positive integer (the output file size).
        assert isinstance(call_kwargs["file_size_bytes"], int)
        assert call_kwargs["file_size_bytes"] > 0

        # fail_job must NOT have been called on the happy path.
        mock_fail.assert_not_called()

    @mock_aws
    @patch.dict(os.environ, _TEST_ENV)
    def test_processed_s3_key_uses_db_user_id(self):
        """
        Security test: the processed S3 key must use db_job.user_id, not the
        user_id present in the SQS message body.

        We pass sqs-user-id in the SQS body but configure the DB job mock to
        return db-user-id. The processed_s3_key argument to complete_job must
        contain 'db-user-id', not 'sqs-user-id'.
        """
        s3 = boto3.client("s3", region_name="ap-south-1")
        s3.create_bucket(
            Bucket=_INPUT_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
        )
        s3.create_bucket(
            Bucket=_OUTPUT_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
        )
        s3.put_object(Bucket=_INPUT_BUCKET, Key=_S3_KEY, Body=_VALID_CSV.encode())

        mock_db = MagicMock()
        # DB job has db-user-id — different from sqs-user-id in the SQS body.
        db_job = _make_db_job(user_id=_DB_USER_ID)  # 'db-user-id'
        mock_complete = MagicMock()

        with (
            patch(_PATCH_SESSION, return_value=mock_db),
            patch(_PATCH_CLAIM, return_value=True),
            patch(_PATCH_GET_JOB, return_value=db_job),
            patch(_PATCH_COMPLETE, mock_complete),
            patch(_PATCH_FAIL, MagicMock()),
        ):
            # Event has sqs-user-id in the body.
            lambda_handler(_make_sqs_event(user_id=_SQS_USER_ID), context=None)

        call_kwargs = mock_complete.call_args[1]
        processed_key = call_kwargs["processed_s3_key"]

        # The key MUST use db-user-id, not sqs-user-id.
        assert processed_key.startswith(f"processed/{_DB_USER_ID}/"), (
            f"Expected key starting with 'processed/{_DB_USER_ID}/' but got: {processed_key}"
        )
        assert _SQS_USER_ID not in processed_key, (
            f"SQS user_id '{_SQS_USER_ID}' must not appear in the processed key: {processed_key}"
        )


class TestInvalidCSV:
    """
    Invalid CSV path: parse_and_analyse raises InvalidCSVError.

    Verifies:
    - fail_job is called with the error message
    - lambda_handler returns cleanly (no exception raised)
    - complete_job is NOT called
    """

    @mock_aws
    @patch.dict(os.environ, _TEST_ENV)
    def test_fail_job_called_and_no_exception_raised(self):
        """
        Empty file → InvalidCSVError('File is empty') → fail_job called,
        handler returns without raising.

        Returning cleanly is critical: it signals SQS to DELETE the message.
        An InvalidCSVError is a permanent failure — retrying with the same
        file would loop forever.
        """
        s3 = boto3.client("s3", region_name="ap-south-1")
        s3.create_bucket(
            Bucket=_INPUT_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
        )
        # Put an EMPTY object — triggers InvalidCSVError in parse_and_analyse.
        s3.put_object(Bucket=_INPUT_BUCKET, Key=_S3_KEY, Body=b"")

        mock_db = MagicMock()
        db_job = _make_db_job()
        mock_fail = MagicMock()
        mock_complete = MagicMock()

        with (
            patch(_PATCH_SESSION, return_value=mock_db),
            patch(_PATCH_CLAIM, return_value=True),
            patch(_PATCH_GET_JOB, return_value=db_job),
            patch(_PATCH_COMPLETE, mock_complete),
            patch(_PATCH_FAIL, mock_fail),
        ):
            # Must NOT raise — clean return signals SQS to delete the message.
            lambda_handler(_make_sqs_event(), context=None)

        # fail_job must be called to record the permanent failure.
        assert mock_fail.call_count == 1
        call_args = mock_fail.call_args[0]  # positional args
        assert call_args[1] == _JOB_ID
        # The failure reason should mention the problem (non-empty string).
        assert isinstance(call_args[2], str)
        assert len(call_args[2]) > 0

        # complete_job must NOT be called on the invalid CSV path.
        mock_complete.assert_not_called()

    @mock_aws
    @patch.dict(os.environ, _TEST_ENV)
    def test_handler_does_not_raise_on_invalid_csv(self):
        """
        Explicitly verify no exception escapes lambda_handler for an invalid CSV.

        A raised exception would cause SQS to retry the message — pointless for
        a permanently invalid file.
        """
        s3 = boto3.client("s3", region_name="ap-south-1")
        s3.create_bucket(
            Bucket=_INPUT_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
        )
        s3.put_object(Bucket=_INPUT_BUCKET, Key=_S3_KEY, Body=b"")

        mock_db = MagicMock()

        with (
            patch(_PATCH_SESSION, return_value=mock_db),
            patch(_PATCH_CLAIM, return_value=True),
            patch(_PATCH_GET_JOB, return_value=_make_db_job()),
            patch(_PATCH_COMPLETE, MagicMock()),
            patch(_PATCH_FAIL, MagicMock()),
        ):
            try:
                lambda_handler(_make_sqs_event(), context=None)
            except Exception as e:
                pytest.fail(f"lambda_handler raised an exception for invalid CSV: {e}")


class TestTransientFailure:
    """
    Transient failure path: an unexpected exception during download.

    Verifies:
    - fail_job is called (user gets immediate error feedback)
    - The exception is re-raised (so SQS retries the message)
    - complete_job is NOT called
    """

    def test_s3_download_failure_calls_fail_job_and_reraises(self):
        """
        S3 download raises a generic RuntimeError (simulating a transient
        infrastructure failure). The handler must:
          1. Call fail_job() to record the error.
          2. Re-raise the exception (SQS retries after visibility timeout).

        Patching download_file directly to avoid needing a real S3 bucket.
        """
        mock_db = MagicMock()
        db_job = _make_db_job()
        mock_fail = MagicMock()
        mock_complete = MagicMock()

        download_error = RuntimeError("Simulated S3 connection timeout")

        with (
            patch(_PATCH_SESSION, return_value=mock_db),
            patch(_PATCH_CLAIM, return_value=True),
            patch(_PATCH_GET_JOB, return_value=db_job),
            patch(_PATCH_DOWNLOAD, side_effect=download_error),
            patch(_PATCH_UPLOAD, MagicMock()),
            patch(_PATCH_COMPLETE, mock_complete),
            patch(_PATCH_FAIL, mock_fail),
        ):
            with pytest.raises(RuntimeError, match="Simulated S3 connection timeout"):
                lambda_handler(_make_sqs_event(), context=None)

        # fail_job must be called even on transient failures.
        assert mock_fail.call_count == 1

        # complete_job must NOT be called.
        mock_complete.assert_not_called()

    def test_transient_failure_records_error_message(self):
        """
        The failure reason passed to fail_job must contain the exception message.
        This gives the user (and operators) visibility into what went wrong.
        """
        mock_db = MagicMock()
        mock_fail = MagicMock()
        download_error = RuntimeError("DB connection refused")

        with (
            patch(_PATCH_SESSION, return_value=mock_db),
            patch(_PATCH_CLAIM, return_value=True),
            patch(_PATCH_GET_JOB, return_value=_make_db_job()),
            patch(_PATCH_DOWNLOAD, side_effect=download_error),
            patch(_PATCH_UPLOAD, MagicMock()),
            patch(_PATCH_COMPLETE, MagicMock()),
            patch(_PATCH_FAIL, mock_fail),
        ):
            with pytest.raises(RuntimeError):
                lambda_handler(_make_sqs_event(), context=None)

        call_args = mock_fail.call_args[0]
        failure_reason = call_args[2]
        assert "DB connection refused" in failure_reason


class TestTerminalStateJob:
    """
    Job already in terminal state: claim_job_for_processing returns False.

    Verifies:
    - Handler returns immediately (early exit)
    - No S3 calls are made
    - No csv_processor calls are made
    - fail_job is NOT called
    - complete_job is NOT called

    This covers the idempotency guard: SQS at-least-once delivery may redeliver
    a message for a job that already completed. The handler must be a no-op.
    """

    def test_returns_immediately_when_claim_returns_false(self):
        """
        claim_job_for_processing returns False → handler returns with no side effects.
        """
        mock_db = MagicMock()
        mock_fail = MagicMock()
        mock_complete = MagicMock()
        mock_download = MagicMock()
        mock_upload = MagicMock()

        with (
            patch(_PATCH_SESSION, return_value=mock_db),
            patch(_PATCH_CLAIM, return_value=False),  # job is in terminal state
            patch(_PATCH_GET_JOB, MagicMock()),
            patch(_PATCH_DOWNLOAD, mock_download),
            patch(_PATCH_UPLOAD, mock_upload),
            patch(_PATCH_COMPLETE, mock_complete),
            patch(_PATCH_FAIL, mock_fail),
        ):
            lambda_handler(_make_sqs_event(), context=None)

        # No S3 operations.
        mock_download.assert_not_called()
        mock_upload.assert_not_called()

        # No DB result writes.
        mock_complete.assert_not_called()
        mock_fail.assert_not_called()

    def test_no_csv_processing_when_claim_returns_false(self):
        """
        csv_processor functions must not be called when claim returns False.
        Patching them to verify they are never invoked.
        """
        mock_db = MagicMock()

        with (
            patch(_PATCH_SESSION, return_value=mock_db),
            patch(_PATCH_CLAIM, return_value=False),
            patch(_PATCH_GET_JOB, MagicMock()),
            patch(_PATCH_DOWNLOAD, MagicMock()),
            patch(_PATCH_UPLOAD, MagicMock()),
            patch(_PATCH_COMPLETE, MagicMock()),
            patch(_PATCH_FAIL, MagicMock()),
            patch("worker.handler.parse_and_analyse") as mock_parse,
            patch("worker.handler.clean") as mock_clean,
            patch("worker.handler.write_csv") as mock_write,
        ):
            lambda_handler(_make_sqs_event(), context=None)

        mock_parse.assert_not_called()
        mock_clean.assert_not_called()
        mock_write.assert_not_called()


class TestUserIdSource:
    """
    Security tests: user_id must come from the DB job record, not the SQS message.

    This is the most important security invariant in the handler. An attacker who
    can write to the SQS queue could craft a message with any user_id to land
    processed files in another user's S3 prefix — unless the handler ignores the
    SQS body's user_id and uses the DB record's user_id instead.
    """

    @mock_aws
    @patch.dict(os.environ, _TEST_ENV)
    def test_processed_key_uses_db_user_id_not_sqs_user_id(self):
        """
        DB job record has user_id='db-user-id'.
        SQS message body has user_id='sqs-user-id'.
        The processed S3 key must contain 'db-user-id', never 'sqs-user-id'.
        """
        s3 = boto3.client("s3", region_name="ap-south-1")
        s3.create_bucket(
            Bucket=_INPUT_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
        )
        s3.create_bucket(
            Bucket=_OUTPUT_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
        )
        s3.put_object(Bucket=_INPUT_BUCKET, Key=_S3_KEY, Body=_VALID_CSV.encode())

        mock_db = MagicMock()
        # DB record has 'db-user-id'.
        db_job = _make_db_job(user_id="db-user-id")
        mock_complete = MagicMock()

        with (
            patch(_PATCH_SESSION, return_value=mock_db),
            patch(_PATCH_CLAIM, return_value=True),
            patch(_PATCH_GET_JOB, return_value=db_job),
            patch(_PATCH_COMPLETE, mock_complete),
            patch(_PATCH_FAIL, MagicMock()),
        ):
            # SQS body contains 'sqs-user-id' — must be ignored.
            lambda_handler(_make_sqs_event(user_id="sqs-user-id"), context=None)

        assert mock_complete.call_count == 1
        call_kwargs = mock_complete.call_args[1]
        processed_key = call_kwargs["processed_s3_key"]

        # MUST use the DB user_id.
        assert "db-user-id" in processed_key
        # MUST NOT use the SQS body user_id.
        assert "sqs-user-id" not in processed_key
        # Key format: processed/{user_id}/{job_id}/result.csv
        assert processed_key == f"processed/db-user-id/{_JOB_ID}/result.csv"
