"""
Unit tests for backend/s3_client.py — S3 operations using moto.

WHAT:
    Tests every public function in s3_client.py using moto[s3] to intercept
    boto3 calls and simulate S3 in memory. No real AWS account is needed.

WHY MOTO:
    moto intercepts boto3 at the HTTP layer and returns realistic S3 responses
    using in-memory storage. Tests run in milliseconds, work offline, and
    never incur AWS costs. The @mock_aws decorator (moto 5.x) activates the
    mock for the duration of each test.

    IMPORTANT: moto's mocked S3 is ephemeral. Each test creates its own bucket
    inside the @mock_aws context because moto resets ALL state when the mock
    context exits. Never rely on state from a previous test.

WHAT EACH TEST CLASS COVERS:
    TestPresignedUploadUrl:
        generate_presigned_upload_url() returns a URL that embeds the bucket
        and key in a form S3 would understand. We verify the URL is non-empty
        and contains the expected key fragments.

    TestPresignedDownloadUrl:
        generate_presigned_download_url() returns a URL for GET. Same
        structural verification as upload.

    TestObjectExists:
        object_exists() returns True for existing objects and False for missing
        ones. Tests the happy path and the 404 path.

    TestGetObjectSize:
        get_object_size() returns the correct byte count after a known-size
        object is stored.

    TestUploadDownloadRoundTrip:
        upload_file() followed by download_file() preserves byte-for-byte
        fidelity — the downloaded file matches the uploaded file exactly.

PATTERN NOTES:
    - All tests follow the class-based grouping pattern: class TestXxx: / def test_yyy(self)
    - Each test method creates a fresh bucket inside its own @mock_aws context.
      There are no shared fixtures for the mock state — each test is fully self-contained.
    - The @mock_aws decorator must wrap each test method, NOT the class, because
      moto resets state at the boundary of the decorator, not at the boundary of
      a class. A class-level decorator would share mock state across test methods.
    - Bucket creation uses the same region (ap-south-1) as production to catch
      any region-specific issues with moto's S3 simulation.

See backend/s3_client.py for the full documentation of each function including
analogies, edge cases, and interview concepts.
"""

import os
import tempfile
from pathlib import Path

import pytest
from moto import mock_aws

# Set the environment BEFORE importing backend modules.
# s3_client.py reads AWS_DEFAULT_REGION from os.environ; if not set it defaults
# to ap-south-1, which is correct. Setting these here prevents boto3 from
# attempting to use real credentials or a real endpoint during tests.
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

import boto3  # noqa: E402 — must come after env setup

from backend.s3_client import (  # noqa: E402
    download_file,
    generate_presigned_download_url,
    generate_presigned_upload_url,
    get_object_size,
    object_exists,
    upload_file,
)

# The bucket name used across all tests — matches the development bucket name.
TEST_BUCKET = "csvforge-files-dev"
TEST_REGION = "ap-south-1"


def _create_bucket(s3_client) -> None:
    """
    Helper: create the test bucket in the mocked S3.

    WHY THIS HELPER EXISTS:
    Every test needs a bucket before it can do anything. Extracting this into
    a helper avoids repeating the same boto3 API call in every test. The
    LocationConstraint is required for all regions except us-east-1 — omitting
    it would cause a "BucketAlreadyExists" or "InvalidLocationConstraint" error
    from moto for ap-south-1.
    """
    s3_client.create_bucket(
        Bucket=TEST_BUCKET,
        CreateBucketConfiguration={"LocationConstraint": TEST_REGION},
    )


# ── Presigned Upload URL ───────────────────────────────────────────────────────

class TestPresignedUploadUrl:
    """
    Tests for generate_presigned_upload_url().

    These tests verify the URL is generated without error and contains the
    expected bucket and key. moto generates a URL with the same structural
    format as real S3 presigned URLs.

    What these tests would catch if they failed:
    - boto3 is not installed or the wrong version.
    - The function crashes or returns None instead of a string.
    - The key is omitted from the URL (wrong Params mapping).
    """

    @mock_aws
    def test_returns_string(self):
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)
        url = generate_presigned_upload_url(TEST_BUCKET, "uploads/user-1/job-abc/original.csv")
        assert isinstance(url, str)

    @mock_aws
    def test_url_is_nonempty(self):
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)
        url = generate_presigned_upload_url(TEST_BUCKET, "uploads/user-1/job-abc/original.csv")
        assert len(url) > 0

    @mock_aws
    def test_url_contains_bucket_name(self):
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)
        url = generate_presigned_upload_url(TEST_BUCKET, "uploads/user-1/job-abc/original.csv")
        assert TEST_BUCKET in url

    @mock_aws
    def test_url_contains_key(self):
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)
        key = "uploads/user-1/job-abc/original.csv"
        url = generate_presigned_upload_url(TEST_BUCKET, key)
        # Slashes in the key are percent-encoded in URLs: "/" becomes "%2F" or
        # remains as "/" depending on the URL segment. Either way, the key
        # fragments must appear in the URL. We check for the filename part.
        assert "original.csv" in url

    @mock_aws
    def test_custom_expiry_is_accepted(self):
        """
        generate_presigned_upload_url() must not raise on a non-default expiry.
        We verify the function accepts expiry_seconds without crashing — the
        actual URL expiry is embedded in the signature and not directly readable.
        """
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)
        url = generate_presigned_upload_url(
            TEST_BUCKET, "uploads/user-1/job-abc/original.csv", expiry_seconds=300
        )
        assert isinstance(url, str)
        assert len(url) > 0


# ── Presigned Download URL ─────────────────────────────────────────────────────

class TestPresignedDownloadUrl:
    """
    Tests for generate_presigned_download_url().

    Same structural checks as TestPresignedUploadUrl. The download URL is used
    to let users retrieve their processed CSV without needing AWS credentials.
    """

    @mock_aws
    def test_returns_string(self):
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)
        url = generate_presigned_download_url(TEST_BUCKET, "processed/user-1/job-abc/result.csv")
        assert isinstance(url, str)

    @mock_aws
    def test_url_is_nonempty(self):
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)
        url = generate_presigned_download_url(TEST_BUCKET, "processed/user-1/job-abc/result.csv")
        assert len(url) > 0

    @mock_aws
    def test_url_contains_key_fragment(self):
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)
        url = generate_presigned_download_url(TEST_BUCKET, "processed/user-1/job-abc/result.csv")
        assert "result.csv" in url


# ── object_exists ─────────────────────────────────────────────────────────────

class TestObjectExists:
    """
    Tests for object_exists().

    These tests verify the happy path (object present → True) and the
    "not yet uploaded" path (object absent → False). The 404 case is the
    most important because it is the guard in /uploads/{job_id}/complete
    that prevents queuing a job before the file arrives.

    What these tests would catch if they failed:
    - The function raises instead of returning False for 404 responses.
    - The ClientError code parsing is wrong (e.g., checking "NoSuchKey"
      instead of "404" — HeadObject returns 404 not "NoSuchKey").
    - object_exists() returns True for a non-existent object (catastrophic bug).
    """

    @mock_aws
    def test_returns_true_when_object_exists(self):
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)
        key = "uploads/user-1/job-abc/original.csv"
        # Put a small object into the mocked bucket so object_exists() has something to find.
        s3.put_object(Bucket=TEST_BUCKET, Key=key, Body=b"col1,col2\nval1,val2")
        assert object_exists(TEST_BUCKET, key) is True

    @mock_aws
    def test_returns_false_when_object_missing(self):
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)
        assert object_exists(TEST_BUCKET, "uploads/user-1/job-abc/original.csv") is False

    @mock_aws
    def test_returns_false_for_different_key(self):
        """Confirm object_exists() is key-specific — wrong key returns False."""
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)
        s3.put_object(Bucket=TEST_BUCKET, Key="uploads/user-1/job-abc/original.csv", Body=b"data")
        # A different key (same prefix) must return False.
        assert object_exists(TEST_BUCKET, "uploads/user-1/job-abc/other.csv") is False


# ── get_object_size ────────────────────────────────────────────────────────────

class TestGetObjectSize:
    """
    Tests for get_object_size().

    Verifies that ContentLength is correctly read from the HeadObject response.
    This is the authoritative byte count used in the 50 MiB size check at
    /uploads/{job_id}/complete.

    What these tests would catch if they failed:
    - The function returns None or 0 instead of the real size.
    - The ContentLength key name is wrong (e.g., "content_length" vs "ContentLength").
    - The function calls GetObject (downloads bytes) instead of HeadObject.
    """

    @mock_aws
    def test_returns_correct_size_for_small_object(self):
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)
        content = b"col1,col2\nval1,val2\n"
        expected_size = len(content)
        s3.put_object(Bucket=TEST_BUCKET, Key="uploads/u1/j1/original.csv", Body=content)
        assert get_object_size(TEST_BUCKET, "uploads/u1/j1/original.csv") == expected_size

    @mock_aws
    def test_returns_correct_size_for_larger_object(self):
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)
        # Create a 10,000-byte payload (simulating a mid-sized CSV).
        content = b"x" * 10_000
        s3.put_object(Bucket=TEST_BUCKET, Key="uploads/u1/j2/original.csv", Body=content)
        assert get_object_size(TEST_BUCKET, "uploads/u1/j2/original.csv") == 10_000

    @mock_aws
    def test_returns_zero_for_empty_object(self):
        """An empty file (0 bytes) is a valid upload — ContentLength should be 0."""
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)
        s3.put_object(Bucket=TEST_BUCKET, Key="uploads/u1/j3/empty.csv", Body=b"")
        assert get_object_size(TEST_BUCKET, "uploads/u1/j3/empty.csv") == 0


# ── upload_file + download_file (round-trip) ──────────────────────────────────

class TestUploadDownloadRoundTrip:
    """
    Tests for upload_file() and download_file().

    These are integration-style unit tests (both functions tested together)
    because the meaningful invariant is: what was uploaded can be downloaded
    and the bytes are identical. Testing upload alone proves nothing about
    whether the bytes are stored correctly.

    What these tests would catch if they failed:
    - upload_file() uploads to the wrong bucket or key.
    - download_file() reads from the wrong key.
    - Encoding/decoding errors corrupt the bytes during the round-trip.
    - The local file is not created by download_file().
    """

    @mock_aws
    def test_roundtrip_preserves_bytes(self):
        """
        Upload a file, download it to a different path, verify byte identity.

        This is the core correctness invariant for the worker pipeline:
        a CSV downloaded from S3 must be identical to the CSV that was uploaded.
        """
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)

        original_content = b"name,age,city\nAlice,30,Mumbai\nBob,25,Delhi\n"
        key = "uploads/user-1/job-test/original.csv"

        with tempfile.TemporaryDirectory() as tmpdir:
            upload_path = Path(tmpdir) / "upload.csv"
            download_path = Path(tmpdir) / "download.csv"

            # Write the original content to a local temp file.
            upload_path.write_bytes(original_content)

            # Upload to mocked S3.
            upload_file(TEST_BUCKET, key, str(upload_path))

            # Download from mocked S3 to a different local path.
            download_file(TEST_BUCKET, key, str(download_path))

            # Verify byte-for-byte identity.
            downloaded_content = download_path.read_bytes()
            assert downloaded_content == original_content

    @mock_aws
    def test_upload_creates_object_in_s3(self):
        """
        After upload_file(), object_exists() must return True.

        Verifies that upload_file() actually writes to S3 (not a dry-run or
        silent no-op) and that the object is discoverable via HeadObject.
        """
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)

        key = "processed/user-1/job-test/result.csv"

        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "result.csv"
            source.write_bytes(b"clean,output\n1,2\n")
            upload_file(TEST_BUCKET, key, str(source))

        assert object_exists(TEST_BUCKET, key) is True

    @mock_aws
    def test_download_creates_local_file(self):
        """
        After download_file(), a local file must exist at dest_path.

        Verifies the file is actually written to disk, not just read into memory.
        """
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)

        key = "uploads/user-1/job-dl/original.csv"
        s3.put_object(Bucket=TEST_BUCKET, Key=key, Body=b"a,b\n1,2\n")

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "downloaded.csv"
            assert not dest.exists()
            download_file(TEST_BUCKET, key, str(dest))
            assert dest.exists()

    @mock_aws
    def test_roundtrip_large_content(self):
        """
        Round-trip preserves bytes for a larger payload (100 KB).

        Verifies that neither upload_file() nor download_file() truncates or
        pads the content, even when the payload spans multiple read/write chunks.
        """
        s3 = boto3.client("s3", region_name=TEST_REGION)
        _create_bucket(s3)

        # Build a realistic-looking 100 KB CSV (5000 rows × 20 bytes each).
        header = b"id,value\n"
        rows = b"".join(f"{i},{i * 2}\n".encode() for i in range(5000))
        original_content = header + rows
        key = "uploads/user-1/job-large/original.csv"

        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "large.csv"
            dst = Path(tmpdir) / "large_dl.csv"
            src.write_bytes(original_content)
            upload_file(TEST_BUCKET, key, str(src))
            download_file(TEST_BUCKET, key, str(dst))
            assert dst.read_bytes() == original_content
