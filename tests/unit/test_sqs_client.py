"""
Unit tests for backend/sqs_client.py — SQS operations using moto.

WHAT:
    Tests every public function in sqs_client.py using moto[sqs] to intercept
    boto3 calls and simulate SQS in memory. No real AWS account is needed.

WHY MOTO:
    moto simulates the SQS message lifecycle (send, receive, delete) entirely
    in memory. Sending a message and receiving it back in the same test verifies
    the full round-trip without a real queue.

    IMPORTANT: like moto[s3], all SQS state is ephemeral within a @mock_aws
    context. Each test creates its own queue to ensure isolation.

WHAT EACH TEST CLASS COVERS:
    TestSendJobMessage:
        send_job_message() returns a MessageId string (non-empty UUID-like string).
        It does not raise on valid inputs and the return type is correct.

    TestParseJobMessage:
        parse_job_message() correctly decodes a JSON string and returns a dict
        with "job_id" and "s3_key" keys. Also verifies that malformed JSON
        raises an appropriate error (json.JSONDecodeError), which is the
        signal for the worker to let the message go to the DLQ.

    TestRoundTrip:
        send_job_message() followed by SQS ReceiveMessage + parse_job_message()
        preserves the original job_id and s3_key exactly. This is the most
        important test: it proves the encode/decode pair is consistent.

PATTERN NOTES:
    - @mock_aws must wrap each test method individually (see test_s3_client.py
      module docstring for the explanation of why class-level wrapping is wrong).
    - Queue creation uses the same region (ap-south-1) and queue name
      (csvforge-jobs-dev) as the production environment to catch any discrepancy.
    - MessageBody is always a JSON string — parse_job_message() decodes it to dict.

See backend/sqs_client.py for the full documentation including the claim-check
pattern, at-least-once delivery, and interview concepts.
"""

import json
import os

import pytest
from moto import mock_aws

# Set fake credentials so moto does not try to use real AWS.
# These must be set BEFORE importing backend modules.
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

import boto3  # noqa: E402

from backend.sqs_client import parse_job_message, send_job_message  # noqa: E402

TEST_REGION = "ap-south-1"
TEST_QUEUE_NAME = "csvforge-jobs-dev"


def _create_queue(sqs_client) -> str:
    """
    Helper: create a test SQS queue and return its URL.

    WHY THIS HELPER EXISTS:
    Every test that sends or receives messages needs a queue. Extracting
    this into a helper avoids repeating the create_queue API call and the
    URL extraction in every test. The returned URL is passed to
    send_job_message() as the queue_url parameter — matching the real
    production pattern where SQS_QUEUE_URL env var holds the queue URL.
    """
    response = sqs_client.create_queue(
        QueueName=TEST_QUEUE_NAME,
        Attributes={"VisibilityTimeout": "1800"},
    )
    return response["QueueUrl"]


# ── send_job_message ───────────────────────────────────────────────────────────

class TestSendJobMessage:
    """
    Tests for send_job_message().

    Verifies that the function enqueues a message without error and returns
    a valid message ID string.

    What these tests would catch if they failed:
    - send_job_message() raises an exception on valid inputs.
    - The return type is not a string (e.g., returns None or the full response dict).
    - The MessageId key name in the SQS response has changed.
    """

    @mock_aws
    def test_returns_message_id_string(self):
        sqs = boto3.client("sqs", region_name=TEST_REGION)
        queue_url = _create_queue(sqs)
        message_id = send_job_message(
            queue_url,
            job_id="job-uuid-001",
            s3_key="uploads/user-1/job-uuid-001/original.csv",
        )
        assert isinstance(message_id, str)

    @mock_aws
    def test_message_id_is_nonempty(self):
        sqs = boto3.client("sqs", region_name=TEST_REGION)
        queue_url = _create_queue(sqs)
        message_id = send_job_message(
            queue_url,
            job_id="job-uuid-002",
            s3_key="uploads/user-1/job-uuid-002/original.csv",
        )
        assert len(message_id) > 0

    @mock_aws
    def test_does_not_raise_on_valid_inputs(self):
        sqs = boto3.client("sqs", region_name=TEST_REGION)
        queue_url = _create_queue(sqs)
        # If this call raises, the test fails automatically.
        send_job_message(
            queue_url,
            job_id="job-uuid-003",
            s3_key="uploads/user-1/job-uuid-003/original.csv",
        )

    @mock_aws
    def test_two_messages_have_different_ids(self):
        """
        Two separate sends must return different MessageIds.

        SQS assigns a unique ID to each message. This test guards against an
        implementation bug where the same ID is returned regardless of which
        message was sent.
        """
        sqs = boto3.client("sqs", region_name=TEST_REGION)
        queue_url = _create_queue(sqs)
        id_1 = send_job_message(queue_url, job_id="job-a", s3_key="uploads/u/job-a/original.csv")
        id_2 = send_job_message(queue_url, job_id="job-b", s3_key="uploads/u/job-b/original.csv")
        assert id_1 != id_2


# ── parse_job_message ──────────────────────────────────────────────────────────

class TestParseJobMessage:
    """
    Tests for parse_job_message().

    Verifies JSON decoding behaviour: correct fields returned for valid input,
    and JSONDecodeError raised for malformed input.

    What these tests would catch if they failed:
    - parse_job_message() swallows malformed JSON instead of raising.
    - The returned dict has the wrong keys or values.
    - Extra fields in the JSON are stripped (they should be preserved).
    """

    def test_returns_correct_job_id(self):
        """parse_job_message must preserve the exact job_id string."""
        body = json.dumps({"job_id": "job-uuid-100", "s3_key": "uploads/u/job-uuid-100/original.csv"})
        result = parse_job_message(body)
        assert result["job_id"] == "job-uuid-100"

    def test_returns_correct_s3_key(self):
        """parse_job_message must preserve the exact s3_key string including slashes."""
        key = "uploads/user-42/job-uuid-100/original.csv"
        body = json.dumps({"job_id": "job-uuid-100", "s3_key": key})
        result = parse_job_message(body)
        assert result["s3_key"] == key

    def test_returns_dict(self):
        body = json.dumps({"job_id": "job-xyz", "s3_key": "uploads/u/job-xyz/original.csv"})
        result = parse_job_message(body)
        assert isinstance(result, dict)

    def test_preserves_extra_fields(self):
        """
        parse_job_message must not strip unknown fields.

        If the message schema is extended in the future (e.g., adding a
        "priority" field), the existing parse_job_message() should pass the
        new field through to the worker. The worker decides which fields to use.
        """
        body = json.dumps({
            "job_id": "job-xyz",
            "s3_key": "uploads/u/job-xyz/original.csv",
            "priority": "high",
        })
        result = parse_job_message(body)
        assert result["priority"] == "high"

    def test_raises_on_malformed_json(self):
        """
        parse_job_message must raise json.JSONDecodeError for invalid JSON.

        This is intentional — malformed messages should propagate the error
        so SQS retries and eventually routes to the DLQ for investigation.
        Silently swallowing the error would lose the message with no trace.
        """
        import json as _json
        with pytest.raises(_json.JSONDecodeError):
            parse_job_message("this is not json")

    def test_raises_on_empty_string(self):
        """An empty string is not valid JSON and must raise JSONDecodeError."""
        import json as _json
        with pytest.raises(_json.JSONDecodeError):
            parse_job_message("")


# ── Round-trip: send → receive → parse ────────────────────────────────────────

class TestRoundTrip:
    """
    Full round-trip tests: send a message and receive + parse it back.

    These tests verify that the encode side (send_job_message) and the decode
    side (parse_job_message) are consistent. If send_job_message used msgpack
    but parse_job_message expected JSON, these tests would catch it immediately.

    What these tests would catch if they failed:
    - The encoding format used by send is incompatible with parse.
    - The job_id or s3_key value is modified in transit (e.g., truncated).
    - The MessageBody stored by SQS is different from what parse_job_message expects.
    """

    @mock_aws
    def test_roundtrip_preserves_job_id(self):
        """
        Send a message with a known job_id, receive it via SQS, parse it.
        The parsed job_id must exactly match what was sent.
        """
        sqs = boto3.client("sqs", region_name=TEST_REGION)
        queue_url = _create_queue(sqs)

        original_job_id = "550e8400-e29b-41d4-a716-446655440000"
        original_s3_key = "uploads/user-1/550e8400-e29b-41d4-a716-446655440000/original.csv"

        send_job_message(queue_url, job_id=original_job_id, s3_key=original_s3_key)

        # Receive the message directly via boto3 (simulates what Lambda does).
        response = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
        received_body = response["Messages"][0]["Body"]

        parsed = parse_job_message(received_body)
        assert parsed["job_id"] == original_job_id

    @mock_aws
    def test_roundtrip_preserves_s3_key(self):
        """The s3_key must survive the send → SQS → receive → parse round-trip intact."""
        sqs = boto3.client("sqs", region_name=TEST_REGION)
        queue_url = _create_queue(sqs)

        original_job_id = "job-roundtrip-001"
        original_s3_key = "uploads/user-99/job-roundtrip-001/original.csv"

        send_job_message(queue_url, job_id=original_job_id, s3_key=original_s3_key)

        response = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
        received_body = response["Messages"][0]["Body"]

        parsed = parse_job_message(received_body)
        assert parsed["s3_key"] == original_s3_key

    @mock_aws
    def test_roundtrip_returns_both_fields(self):
        """After a round-trip, the parsed dict contains both job_id and s3_key."""
        sqs = boto3.client("sqs", region_name=TEST_REGION)
        queue_url = _create_queue(sqs)

        send_job_message(
            queue_url,
            job_id="job-both-fields",
            s3_key="uploads/user-1/job-both-fields/original.csv",
        )

        response = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
        received_body = response["Messages"][0]["Body"]
        parsed = parse_job_message(received_body)

        assert "job_id" in parsed
        assert "s3_key" in parsed
