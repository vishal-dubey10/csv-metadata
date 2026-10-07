"""
CSVForge — SQS client wrapper.

WHAT:
    This module is the single place responsible for sending and parsing
    SQS messages in the CSVForge system. It provides three functions:
      1. get_sqs_client()       — construct a boto3 SQS client
      2. send_job_message()     — enqueue a job for the Lambda worker
      3. parse_job_message()    — decode a message the Lambda worker received

WHY:
    Centralising SQS calls here means:
    - The message format (JSON schema) is defined in one place. If the schema
      changes, only this file needs updating — not every call site.
    - Unit tests can mock this module rather than mocking boto3 internals.
    - The rest of the application (routes) does not need to know SQS API details.
    - parse_job_message() in the worker mirrors send_job_message() in the API —
      encoding and decoding are co-located, reducing the chance of a mismatch.

ARCHITECTURE:
    FastAPI (upload complete endpoint)
        │
        │ calls send_job_message(queue_url, job_id, s3_key)
        ▼
      SQS queue (csvforge-jobs-dev)
        │
        │ event source mapping triggers Lambda
        ▼
    Lambda worker
        │
        │ receives SQS event, calls parse_job_message(event_record["body"])
        ▼
    dict {"job_id": "...", "s3_key": "..."}

MESSAGE FORMAT:
    The SQS message body is a JSON string containing exactly two fields:
        {
          "job_id": "550e8400-e29b-41d4-a716-446655440000",
          "s3_key": "uploads/user-1/550e8400-e29b-41d4-a716-446655440000/original.csv"
        }

    WHY ONLY job_id AND s3_key:
        SQS messages have a maximum body size of 256 KB. More importantly,
        messages should contain IDENTIFIERS, not DATA. The worker uses the
        job_id to claim the job in PostgreSQL and to determine the output key.
        The worker uses s3_key to download the raw CSV from S3. File content
        never travels through SQS — it stays in S3.

        This is the "claim check" pattern: instead of putting the full payload
        in the message, you put a ticket (identifier) and retrieve the payload
        from a storage system (S3). Benefits:
        - No 256 KB size constraint for the actual data.
        - S3 stores large files cheaply; SQS queuing stays fast.
        - The message is lightweight → faster throughput, lower cost.

ANALOGY — SQS as a job ticket board:
    send_job_message() is the reception desk stamping a job ticket and
    pinning it to the board: "Job #550e8400 is ready for processing,
    file is in shelf uploads/user-1/550e8400/original.csv."

    parse_job_message() is the factory worker reading the ticket to find
    out which job to claim and where to find the raw file.

    The ticket contains only the reference (shelf location), not the
    actual file contents — the worker picks up the file separately.

IMPORTANT CONCEPTS:
    At-least-once delivery:
        SQS Standard queues deliver each message AT LEAST once. In rare
        cases, the same message may be delivered more than once (e.g., if
        Lambda is killed mid-processing after receiving but before deleting).
        The worker must be idempotent: processing the same job_id twice
        must be safe. claim_job_for_processing() handles this via a
        conditional UPDATE that only succeeds from "queued" state (AD-29).

    Message ID vs message body:
        send_job_message() returns the MessageId assigned by SQS (a UUID).
        This ID is useful for distributed tracing but is NOT the job_id —
        they are separate identifiers. The MessageId is SQS-internal;
        the job_id is the CSVForge application identifier stored in PostgreSQL.

    Claim-check pattern:
        A messaging pattern where large payloads are stored in a separate
        data store (here: S3) and only a reference (the key) is sent in the
        message. The consumer fetches the full payload using the reference.
        This keeps message queues lightweight and decoupled from storage.

Interview concept:
    "What is the claim-check pattern and when would you use it?"
    When your message payload exceeds the broker's size limit (256 KB for SQS),
    or when you want to decouple storage from messaging, store the large payload
    in an external store (S3, database) and send only a reference in the message.
    The consumer retrieves the payload separately. This reduces message costs,
    keeps the broker fast, and allows large payloads without changing the
    messaging protocol.
"""

import json
import os

import boto3


def get_sqs_client():
    """
    Create and return a boto3 SQS client.

    WHAT:
        Constructs a boto3 SQS client configured for the correct region.
        Supports an optional endpoint override for LocalStack.

    WHY:
        Credentials are read from the standard boto3 credential chain (env vars
        → ~/.aws → Lambda execution role). The caller never passes credentials
        explicitly — this is the correct pattern for portable AWS code.

    HOW (execution order):
        1. Read AWS_DEFAULT_REGION from the environment (default: ap-south-1).
        2. Read AWS_ENDPOINT_URL from the environment (optional LocalStack support).
        3. Call boto3.client("sqs", ...) and return the client.

    Output:
        A configured boto3 SQS service client.

    Side effects:
        Reads environment variables; no network calls at construction time.

    Analogy:
        Same "phone handset" analogy as s3_client.get_s3_client() — the client
        is the pre-configured handset, API calls are the actual phone calls.
        See s3_client.py for the full analogy explanation.
    """
    region = os.getenv("AWS_DEFAULT_REGION", "ap-south-1")

    # AWS_ENDPOINT_URL allows redirecting boto3 calls to LocalStack or other
    # SQS-compatible services. When unset, boto3 targets real AWS SQS.
    endpoint_url = os.getenv("AWS_ENDPOINT_URL")

    return boto3.client("sqs", region_name=region, endpoint_url=endpoint_url)


def send_job_message(queue_url: str, job_id: str, s3_key: str) -> str:
    """
    Publish a job processing message to the SQS queue.

    WHAT:
        Encodes job_id and s3_key as a JSON string and sends it as a message
        to the SQS queue identified by queue_url. Returns the SQS-assigned
        message ID.

    WHY:
        After a file upload is confirmed (/uploads/{job_id}/complete), the job
        state transitions to "queued" in PostgreSQL. The corresponding SQS
        message tells the Lambda worker WHICH job to process and WHERE to find
        the raw file in S3. Decoupling these two systems (PostgreSQL + SQS) via
        the job_id means the worker and API never call each other directly —
        they communicate only through the queue and the database.

        WHY JSON (not plain text):
            JSON is self-describing and easily extensible. If we later add a
            "priority" field to the message, we add one key to the JSON; the
            existing parse_job_message() code would ignore the new field until
            explicitly handled. A plain-text or binary format would require a
            versioning scheme from the start.

    HOW (execution order):
        1. Construct the message body as a Python dict.
        2. JSON-encode the dict to a string (json.dumps).
        3. Call SQS SendMessage with queue_url and the JSON body.
        4. Return the MessageId from the SQS response.

    Inputs:
        queue_url: Full SQS queue URL, e.g.:
            "https://sqs.ap-south-1.amazonaws.com/123456789012/csvforge-jobs-dev"
            Read from the SQS_QUEUE_URL environment variable by the route handler.
        job_id: The CSVForge job UUID (primary key in PostgreSQL jobs table).
        s3_key: The S3 object key for the uploaded CSV, e.g.:
            "uploads/user-1/550e8400-e29b-41d4-a716-446655440000/original.csv"

    Output:
        The SQS MessageId string (a UUID assigned by SQS). Not the job_id.
        Useful for distributed tracing or deduplication in FIFO queues.

    Side effects:
        One HTTPS call to SQS; the message is enqueued and will be delivered
        to the Lambda worker within milliseconds (typically <10 ms).

    Edge cases:
        - If queue_url is wrong or the queue does not exist: ClientError
          "AWS.SimpleQueueService.NonExistentQueue".
        - If the IAM role lacks sqs:SendMessage on the queue ARN: ClientError
          with "AccessDenied". Check the IAM policy on the API Lambda execution role.
        - Message size > 256 KB: MessageTooLong error. This cannot happen here
          because job_id and s3_key are both short strings (total < 1 KB).

    Analogy:
        This function is the reception desk writing a job ticket and pinning it
        to the board. The ticket says: "Job #<job_id>, raw file at <s3_key>."
        The factory worker (Lambda) will pick up this ticket, cover it with a
        sticky note (visibility timeout), and start processing.

    Interview concept:
        "What makes a system loosely coupled?"
        FastAPI and the Lambda worker never call each other directly. They are
        connected only by the SQS queue (for new work) and PostgreSQL (for state).
        If the Lambda worker is down, FastAPI keeps accepting uploads and enqueueing
        messages — it does not fail. When Lambda comes back up, it processes the
        backlog. This temporal decoupling is the core benefit of message queues.
    """
    client = get_sqs_client()

    # The message body must contain only what the worker needs to do its job:
    # the job identifier (to claim + update in PostgreSQL) and the S3 key
    # (to download the raw CSV). Nothing else — no user data, no file content.
    body = json.dumps({"job_id": job_id, "s3_key": s3_key})

    response = client.send_message(QueueUrl=queue_url, MessageBody=body)

    # SQS assigns a unique MessageId to every message it accepts. This ID is
    # different from the CSVForge job_id — it is SQS-internal. We return it
    # to the caller for logging / tracing purposes.
    return response["MessageId"]


def parse_job_message(body: str) -> dict:
    """
    Parse a raw SQS message body string into a structured dict.

    WHAT:
        Decodes the JSON string from an SQS event record's "body" field and
        returns a Python dict with "job_id" and "s3_key" keys.

    WHY:
        The Lambda worker receives SQS events in the form:
            {
                "Records": [
                    {
                        "body": "{\"job_id\": \"...\", \"s3_key\": \"...\"}",
                        ...
                    }
                ]
            }
        The "body" is a raw string, not a Python dict. This function handles
        the JSON decode so the worker code reads clean Python objects rather
        than calling json.loads() directly. It also documents and enforces the
        expected message schema in one place.

    HOW (execution order):
        1. json.loads(body) — decode the JSON string to a Python dict.
        2. Return the dict (caller accesses d["job_id"] and d["s3_key"]).

    Inputs:
        body: The raw SQS message body string (must be valid JSON with
              "job_id" and "s3_key" keys).

    Output:
        A dict with at minimum {"job_id": str, "s3_key": str}.
        May contain additional keys if the message schema was extended —
        the caller should access only the keys it knows about.

    Side effects:
        None — pure decoding, no network calls.

    Edge cases:
        - Malformed JSON (e.g., truncated message): json.JSONDecodeError raised.
          The worker should let this propagate so SQS retries the message.
          After MaxReceiveCount retries, the message moves to the DLQ where an
          engineer can inspect the malformed body.
        - Missing "job_id" or "s3_key": the worker will get a KeyError when it
          accesses d["job_id"]. Again, let this propagate to trigger a DLQ move.
          Never silently swallow malformed messages — they are bugs that need
          investigation.

    Interview concept:
        "What is the difference between schema validation at the producer vs consumer?"
        The producer (FastAPI / send_job_message) controls the message format at
        generation time — it always produces valid JSON with the correct keys.
        The consumer (Lambda / parse_job_message) parses defensively. In practice,
        if both sides use the same sqs_client.py module, mismatches are caught at
        development time. In a polyglot system (different languages), a schema
        registry (Avro, Protobuf, JSON Schema) enforces compatibility.
    """
    # json.loads raises json.JSONDecodeError if `body` is not valid JSON.
    # We intentionally do NOT catch it here — let the worker propagate the error
    # to SQS so it retries and eventually routes to the DLQ for investigation.
    return json.loads(body)
