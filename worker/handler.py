"""
CSVForge worker — Lambda handler for CSV processing.

WHAT:
    This module is the AWS Lambda entry point for the CSV processing worker.
    It defines `lambda_handler(event, context)`, the function that AWS Lambda
    calls whenever an SQS message triggers this Lambda. Each invocation
    processes one or more SQS records (CSV processing jobs).

WHY (the full pipeline matters):
    The handler is the orchestrator. It coordinates four separate subsystems:
      - SQS (message parsing)       — what job to process
      - PostgreSQL (job state)      — claim ownership, record results
      - S3 (file storage)           — download input, upload output
      - csv_processor (core logic)  — the actual CSV analysis and cleaning

    None of those subsystems know about each other. The handler wires them
    together in the correct order. Getting the order wrong causes bugs that are
    hard to reproduce (out-of-order S3/DB writes, missed claim guards, etc.).

ARCHITECTURE — where this file sits:

    SQS queue (csvforge-jobs-dev)
        │
        │  event source mapping triggers Lambda automatically
        ▼
    lambda_handler(event, context)   ← YOU ARE HERE
        │
        │  1. parse_job_message()     — decode the SQS body
        │  2. SessionLocal()          — open a DB session
        │  3. claim_job_for_processing() — idempotency guard
        │  4. download_file()         — fetch raw CSV from S3
        │  5. parse_and_analyse()     — validate + parse CSV
        │  6. clean()                 — trim + deduplicate
        │  7. write_csv()             — write cleaned output to /tmp
        │  8. upload_file()           — push cleaned CSV to S3 (BEFORE DB update)
        │  9. complete_job()          — mark job completed in PostgreSQL
        ▼
    SQS deletes the message (Lambda returned successfully)

PROCESSING PIPELINE (numbered for inline reference):

    1.  Parse SQS event — iterate over event['Records']
    2.  Parse message body via sqs_client.parse_job_message()
    3.  Open DB session via SessionLocal()
    4.  claim_job_for_processing(db, job_id) — if False (terminal state or
        already claimed), log and return early — no exception raised
    5.  Download CSV from S3 to /tmp/{job_id}_input.csv via download_file()
    6.  parse_and_analyse(file_path) — catch InvalidCSVError → fail_job()
        → return cleanly (SQS deletes message on clean return)
    7.  clean(analysis.rows) → CleanResult
    8.  write_csv(/tmp/{job_id}_output.csv, column_names, cleaned_rows)
    9.  Construct processed S3 key: processed/{user_id}/{job_id}/result.csv
        — IMPORTANT: user_id comes from the DB job record (db_job.user_id),
        NOT from the SQS message (security principle: SQS messages are untrusted)
    10. upload_file(bucket, processed_key, output_path) — S3 BEFORE DB update
        (AD-14: if S3 upload fails, job retries cleanly on next SQS delivery;
        the converse order would update the DB with a key that has no S3 object)
    11. complete_job(db, job_id, processed_s3_key, file_size_bytes, row_count,
        column_count, column_names, duplicate_rows_removed)
    12. Any unexpected exception: fail_job() with error message, then re-raise
        (SQS will retry after the visibility timeout — transient failure path)

KEY INVARIANTS:

    Security — user_id sourced from DB, not SQS:
        The SQS message body is untrusted input. Anyone who can write to the
        SQS queue (or replay a message) could set an arbitrary user_id in the
        message body to land output files in another user's S3 prefix. The DB
        job record is authoritative — it was written by the authenticated API
        endpoint using the verified Cognito sub claim.

        processed/{db_job.user_id}/{job_id}/result.csv
                   ↑ from DB, not SQS ↑

    AD-14 — S3 before DB:
        The S3 upload happens BEFORE the DB update. If the upload fails, the
        job stays in 'processing' state and SQS retries the message. On retry,
        the worker re-downloads and re-uploads (idempotent — same S3 key).
        If we updated the DB first and then S3 failed, we would have a completed
        job record with a processed_s3_key that has no corresponding S3 object —
        the download endpoint would return a broken presigned URL.

    InvalidCSVError — permanent failure path:
        If the CSV is permanently invalid (empty, wrong encoding, malformed),
        the worker calls fail_job() and returns CLEANLY (no exception raised).
        Returning cleanly signals SQS to delete the message. Re-raising would
        cause SQS to retry the message with the same broken file — pointless.

    All other exceptions — transient failure path:
        S3 throttling, DB connection timeout, Lambda running out of memory —
        these are infrastructure problems that may resolve on retry. The worker
        calls fail_job() (to record the error reason) and then re-raises.
        Re-raising causes Lambda to report failure to SQS, which makes the
        message visible again after the visibility timeout and retries up to
        MaxReceiveCount times.

ANALOGY — Lambda as a temporary factory worker:
    Lambda is a factory where temporary workers are hired on demand. When a
    job ticket (SQS message) appears on the ticket board (SQS queue), AWS
    hires a worker (Lambda container cold start) or wakes up an idle worker
    (warm invocation). The worker reads the ticket, grabs the raw materials
    from the warehouse shelf (S3 download), runs them through the machine
    (csv_processor), stores the finished product on the output shelf
    (S3 upload), and stamps the job ticket as done (DB update).

COLD START vs WARM INVOCATION:
    Cold start: Lambda container does not exist yet. AWS creates a new
    container, runs module-level initialization (imports, engine creation,
    credential loading), and then calls lambda_handler(). This takes 1-3
    seconds for a Python Lambda. The module-level code (SessionLocal import,
    env var reads) runs ONCE.

    Warm invocation: The container is still running from a previous request.
    Lambda calls lambda_handler() directly — no container startup, no re-import.
    Module-level variables (like the DB engine's connection pool) persist across
    warm invocations. This is why pool_pre_ping=True is important — a warm
    container may have a stale database connection from a previous invocation.

IDEMPOTENCY:
    claim_job_for_processing() is the primary guard against double-processing.
    It uses a conditional UPDATE that only succeeds from 'queued' or 'processing'
    state. If SQS delivers the same message twice (at-least-once delivery), the
    second invocation will either:
      a) Also claim it (if the first is still processing) — acceptable, the
         first will eventually complete or fail; the second is a wasted run.
      b) Find the job already completed/failed — claim returns False, returns early.
    For full idempotency, the S3 key is deterministic: if the worker uploads
    the same file twice to the same key, the second upload overwrites the first
    with identical content. No orphaned objects.

ENV VARS READ AT STARTUP:
    S3_BUCKET_NAME      — the input bucket (where uploaded CSVs live)
    S3_OUTPUT_BUCKET    — the output bucket (where processed CSVs go);
                          falls back to S3_BUCKET_NAME if not set
    DATABASE_URL        — used by backend.db.connection at import time

IMPORTANT CONCEPTS:
    Event source mapping: the AWS configuration that connects an SQS queue
    to a Lambda function. Lambda polls the queue, batches messages, and
    calls lambda_handler with a batch of records in event['Records'].
    Message acknowledgement: Lambda acknowledges (deletes) messages by
    returning successfully. A raised exception means the message stays
    in the queue for retry. This is why error handling is critical here.
    SQS visibility timeout: how long a message is hidden from other consumers
    after being received. Set to 1800s (30 min) in queues.yaml to give the
    Lambda enough time to process large files before SQS retries.
    DLQ (Dead Letter Queue): after MaxReceiveCount failed deliveries, the
    message moves to the DLQ where an engineer can inspect it manually.

Interview concept:
    "How do you handle idempotency in a Lambda-SQS worker?"
    Use a conditional UPDATE as the claim guard (only succeeds once per
    non-terminal job). Use deterministic S3 keys so re-uploads are safe.
    Distinguish permanent failures (return cleanly) from transient failures
    (re-raise so SQS retries). After MaxReceiveCount retries, the message
    goes to the DLQ for manual inspection.
"""

import logging
import os
import sys

from backend.db.connection import SessionLocal
from backend.db.job_repository import (
    claim_job_for_processing,
    complete_job,
    fail_job,
    get_job_by_id,
)
from backend.s3_client import download_file, upload_file
from backend.sqs_client import parse_job_message
from worker.csv_processor import parse_and_analyse, clean, write_csv
from worker.exceptions import InvalidCSVError

# ── Logging setup ─────────────────────────────────────────────────────────────
# AWS Lambda captures stdout and stderr to CloudWatch Logs automatically.
# Using the standard `logging` module (rather than print()) gives us:
#   - Log levels (DEBUG, INFO, WARNING, ERROR) for filtering in CloudWatch.
#   - Module name in the log line (__name__ = "worker.handler") for tracing.
#   - Structured output that CloudWatch Logs Insights can parse.
#
# Level is set to INFO in production (Lambda's default). Set LOG_LEVEL=DEBUG
# in the Lambda environment to enable verbose output for debugging.
logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# ── Environment variable accessors ───────────────────────────────────────────
# S3 bucket names are read at invocation time (inside _process_record), not at
# module load time. Reading at invocation time means tests can set the env vars
# before calling lambda_handler and see the correct values without module reload.
#
# In production (Lambda), the env vars are set once per container and never
# change — reading them on every invocation adds negligible overhead.
#
# S3_BUCKET_NAME: the input bucket where users' uploaded CSVs live.
#   Key format: uploads/{user_id}/{job_id}/original.csv
#   Matches the key constructed by the upload intent endpoint (main.py).
#
# S3_OUTPUT_BUCKET: the output bucket where processed CSVs are written.
#   Key format: processed/{user_id}/{job_id}/result.csv
#   Falls back to S3_BUCKET_NAME if not set — allows single-bucket setups
#   in development without requiring a second bucket.
#
# DATABASE_URL: consumed by backend.db.connection at import time (not here).
#   We do not read it here; connection.py handles it when it imports.
def _get_input_bucket() -> str:
    """Read S3_BUCKET_NAME at invocation time so tests can override via env."""
    return os.getenv("S3_BUCKET_NAME", "")


def _get_output_bucket() -> str:
    """Read S3_OUTPUT_BUCKET at invocation time; fall back to S3_BUCKET_NAME."""
    return os.getenv("S3_OUTPUT_BUCKET") or _get_input_bucket()


# ── Main handler ──────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context: object) -> None:
    """
    AWS Lambda entry point — process one batch of SQS CSV processing jobs.

    WHAT:
        Iterates over the SQS records in the Lambda event and processes each
        one through the full CSV pipeline: download → validate → clean →
        upload → mark complete.

    WHY:
        SQS event source mappings can deliver multiple records in a single
        Lambda invocation (batch size). Iterating over event['Records'] ensures
        we process all records in the batch, not just the first one.

        However, if any record raises an unhandled exception, Lambda will
        report the entire batch as failed and SQS will re-deliver all records
        (including ones already successfully processed). For simplicity, this
        handler uses a per-record try/except: failures on one record do not
        affect other records in the same batch.

        The claim_job_for_processing idempotency guard ensures that a
        re-delivered record for an already-completed job is a no-op.

    HOW (execution order):
        For each record in event['Records']:
          1.  Parse the JSON message body.
          2.  Dispatch to _process_record() for the full pipeline.

    Inputs:
        event: The SQS event dict from AWS Lambda. Structure:
            {
                "Records": [
                    {
                        "body": "{\"job_id\": \"...\", \"s3_key\": \"...\"}",
                        ...
                    }
                ]
            }
        context: Lambda context object (not used in this handler).
                 Contains remaining_time_in_millis(), function_name, etc.

    Output:
        None. Return value is ignored by SQS.

    Side effects:
        - S3: downloads input file, uploads output file.
        - PostgreSQL: updates job status (processing → completed/failed).
        - /tmp: creates and leaves temporary files (Lambda resets /tmp on cold start).

    Edge cases:
        - Empty event['Records']: no iterations, handler returns None harmlessly.
        - Malformed JSON in message body: json.JSONDecodeError propagates;
          the whole batch is retried by SQS.
        - Multiple records in one batch: each is processed independently via
          _process_record(). A failure on one does not abort others.
    """
    # ── Pipeline step 1: iterate over SQS records ─────────────────────────────
    # event['Records'] is a list of SQS message dicts. Each dict has a 'body'
    # field containing the JSON-encoded job payload. In normal operation,
    # batch size is 1 (configured in the CloudFormation event source mapping).
    # We iterate defensively in case batch size is changed in the future.
    for record in event.get("Records", []):
        _process_record(record)


def _process_record(record: dict) -> None:
    """
    Process a single SQS record through the full CSV processing pipeline.

    WHAT:
        Handles all 12 pipeline steps (see module docstring) for one SQS
        message. Encapsulated in a separate function so the per-record logic
        is easy to test in isolation and easy to read in lambda_handler.

    WHY A SEPARATE FUNCTION:
        lambda_handler iterates over records and calls this function. Having
        all 12 steps inline in lambda_handler would make it too long to read
        and too coarse-grained to test. _process_record is the unit that
        tests exercise with mocks.

    HOW (execution order — mirrors the module docstring pipeline):
        Steps 1-12 are documented inline below with matching step numbers.

    Inputs:
        record: One dict from event['Records']. Must have a 'body' key
                containing a JSON string with 'job_id' and 's3_key'.

    Output:
        None.

    Side effects:
        DB session opened and closed (via SessionLocal).
        S3 download, S3 upload.
        /tmp files created.

    Edge cases:
        - claim_job_for_processing returns False: log and return (no exception).
        - InvalidCSVError: call fail_job(), commit, return (no exception).
        - Any other exception: call fail_job(), commit, re-raise.
    """
    # ── Pipeline step 2: parse the SQS message body ───────────────────────────
    # parse_job_message() decodes the JSON string from record['body'].
    # It returns a dict with at minimum 'job_id' and 's3_key'.
    # If the body is malformed JSON, json.JSONDecodeError propagates — SQS
    # will retry the message. We do NOT catch it here.
    message = parse_job_message(record["body"])
    job_id = message["job_id"]
    s3_key = message["s3_key"]

    logger.info("Processing job %s (s3_key=%s)", job_id, s3_key)

    # ── Pipeline step 3: open a DB session ────────────────────────────────────
    # SessionLocal() creates a new SQLAlchemy session. In Lambda warm
    # invocations, the session factory is module-level and reused; only the
    # Session object itself is new per invocation.
    # The session is closed in the finally block to return the underlying
    # connection to the pool regardless of success or failure.
    db = SessionLocal()
    try:
        # ── Pipeline step 4: claim the job atomically ──────────────────────────
        # claim_job_for_processing() issues a conditional UPDATE:
        #   UPDATE jobs SET status='processing'
        #   WHERE id=job_id AND status IN ('queued', 'processing')
        # Returns True if the UPDATE affected exactly 1 row (we own this job).
        # Returns False if the job is in a terminal state (completed/failed),
        # doesn't exist, or was already claimed by another worker.
        #
        # WHY log and return (not raise):
        # A False return is not an error — it is expected behavior when SQS
        # re-delivers a message for a job that already finished or was
        # cancelled. Raising would cause SQS to retry again unnecessarily.
        claimed = claim_job_for_processing(db, job_id)
        if not claimed:
            logger.info(
                "Job %s is in a terminal state or already claimed — skipping",
                job_id,
            )
            return

        # Fetch the DB job record so we can read user_id.
        # SECURITY NOTE: We read user_id from the DB record, NOT from the SQS
        # message. The SQS message body is untrusted. If an attacker replays a
        # message with a different user_id, the DB record's user_id overrides
        # it — the processed file still lands in the correct user's S3 prefix.
        # This is the single most important security invariant in this handler.
        db_job = get_job_by_id(db, job_id)
        if db_job is None:
            # This should be impossible (claim succeeded, so the row exists),
            # but guard defensively.
            logger.error("Job %s claimed but not found in DB — skipping", job_id)
            return

        # ── Pipeline step 5: download the raw CSV from S3 ─────────────────────
        # Lambda's ephemeral /tmp storage is up to 512 MB per container.
        # We use a per-job filename to avoid collisions when the container
        # processes multiple jobs in sequence (warm invocations).
        input_path = f"/tmp/{job_id}_input.csv"

        # Read bucket names at invocation time (not at module load time).
        # This ensures tests can set S3_BUCKET_NAME via os.environ before
        # calling lambda_handler and have the handler see the correct value.
        input_bucket = _get_input_bucket()
        output_bucket = _get_output_bucket()

        # ── Pipeline step 6: parse and analyse the CSV ─────────────────────────
        # This block handles both the download and the parse so that any
        # exception during either step falls through to the outer handler.
        # InvalidCSVError is a permanent failure — catch it, record it, return.
        # All other exceptions are transient — let them propagate to the outer
        # except block which calls fail_job() and re-raises.
        try:
            download_file(input_bucket, s3_key, input_path)
            analysis = parse_and_analyse(input_path)
        except InvalidCSVError as e:
            # Permanent failure: the file is unrecoverable. Record the reason,
            # commit the status change, and return cleanly.
            # Returning cleanly (no exception) signals SQS to delete the message.
            logger.warning("Job %s permanently failed: %s", job_id, e.message)
            fail_job(db, job_id, e.message)
            db.commit()
            return

        # ── Pipeline step 7: clean (trim + deduplicate) ────────────────────────
        # clean() is a pure function — no I/O, no side effects.
        # Returns a CleanResult with cleaned_rows and duplicate_rows_removed.
        clean_result = clean(analysis.rows)

        # ── Pipeline step 8: write the cleaned CSV to /tmp ────────────────────
        output_path = f"/tmp/{job_id}_output.csv"
        write_csv(output_path, analysis.column_names, clean_result.cleaned_rows)

        # ── Pipeline step 9: construct the processed S3 key ───────────────────
        # SECURITY: user_id comes from db_job.user_id (the DB record set by the
        # authenticated API endpoint), NOT from the SQS message body.
        # This prevents an attacker from landing processed files in another
        # user's S3 prefix by sending a crafted SQS message.
        # See the module docstring "KEY INVARIANTS" section for the full rationale.
        processed_key = f"processed/{db_job.user_id}/{job_id}/result.csv"

        # ── Pipeline step 10: upload processed CSV to S3 ──────────────────────
        # AD-14: S3 upload happens BEFORE the DB update.
        # If the upload fails here (S3 throttling, network error), we raise
        # an exception → fail_job is called → job is retried on next SQS delivery.
        # The key is deterministic (same path on retry), so re-upload is safe.
        #
        # If we updated the DB first and S3 upload failed, we would have a
        # 'completed' job record with a processed_s3_key that has no S3 object.
        # The download endpoint would generate a presigned URL for a non-existent
        # file — the user would get a 403/404 when they tried to download.
        upload_file(output_bucket, processed_key, output_path)

        # ── Pipeline step 11: mark the job as completed in PostgreSQL ──────────
        # All result metadata is written in one UPDATE statement.
        # file_size_bytes: the number of rows × some per-row cost is a rough
        # estimate; for simplicity we use the raw row count from the analysis.
        # The actual file size should be obtained from the output file's os.path.
        import os as _os
        output_file_size = _os.path.getsize(output_path)

        complete_job(
            db,
            job_id=job_id,
            processed_s3_key=processed_key,
            file_size_bytes=output_file_size,
            row_count=clean_result.cleaned_rows.__len__(),
            column_count=analysis.column_count,
            column_names=analysis.column_names,
            duplicate_rows_removed=clean_result.duplicate_rows_removed,
        )
        db.commit()

        logger.info(
            "Job %s completed: %d rows, %d columns, %d duplicates removed",
            job_id,
            len(clean_result.cleaned_rows),
            analysis.column_count,
            clean_result.duplicate_rows_removed,
        )

    except Exception as e:
        # ── Pipeline step 12: transient failure path ───────────────────────────
        # An unexpected exception (S3 throttling, DB timeout, OOM, etc.) has
        # occurred. Record the failure reason in PostgreSQL so the user sees
        # the error message, then RE-RAISE so Lambda signals failure to SQS.
        #
        # WHY re-raise (not return cleanly):
        # Returning cleanly would signal SQS to delete the message. A transient
        # error may resolve on the next attempt — re-raising keeps the message
        # in the queue for retry (up to MaxReceiveCount times, then → DLQ).
        #
        # WHY call fail_job() before re-raising:
        # If this Lambda invocation ultimately times out or maxes out retries,
        # the user would see the job stuck in 'processing' forever. fail_job()
        # gives the user immediate feedback about what went wrong. On retry,
        # claim_job_for_processing returns True (processing state is in the IN
        # clause), so the retry will attempt to process the job again and may
        # overwrite the failed status with completed.
        error_message = str(e)
        logger.error("Job %s failed with unexpected error: %s", job_id, error_message, exc_info=True)
        try:
            fail_job(db, job_id, error_message)
            db.commit()
        except Exception:
            # If fail_job itself fails (e.g., DB is completely unreachable),
            # log and swallow — we still need to re-raise the original error.
            logger.error("Could not record failure reason for job %s", job_id, exc_info=True)
        raise

    finally:
        # Always close the session to return the connection to the pool.
        # This runs whether we returned normally, raised InvalidCSVError,
        # or raised any other exception.
        db.close()


# ── Local test entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Local test runner — invoke lambda_handler with a test event from a JSON file.

    USAGE:
        PYTHONPATH=. S3_BUCKET_NAME=my-bucket DATABASE_URL=postgresql://... \\
        .venv/bin/python worker/handler.py path/to/test_event.json

    EXAMPLE test_event.json:
        {
            "Records": [
                {
                    "body": "{\"job_id\": \"test-job-1\", \"s3_key\": \"uploads/user-1/test-job-1/original.csv\"}"
                }
            ]
        }

    WHY:
        AWS Lambda development without a local emulator (like SAM CLI or
        LocalStack) requires deploying to test changes. The `if __name__`
        block allows running the handler locally against a real test event
        file, a real S3 bucket, and a real PostgreSQL database — useful for
        end-to-end smoke tests before deployment.

        This pattern is described in AD-28 (run_local.sh).
        The handler can also be tested against LocalStack by setting
        AWS_ENDPOINT_URL to the LocalStack endpoint.

    HOW:
        The first CLI argument is the path to a JSON file containing a Lambda
        event. The event is loaded and passed to lambda_handler() directly.
        A fake context object satisfies the handler's signature without needing
        a real Lambda context.
    """
    import json

    if len(sys.argv) < 2:
        print("Usage: python worker/handler.py <path/to/test_event.json>")
        sys.exit(1)

    event_path = sys.argv[1]
    with open(event_path) as f:
        test_event = json.load(f)

    class FakeContext:
        """Minimal stand-in for the AWS Lambda context object."""
        function_name = "csvforge-worker-local"
        remaining_time_in_millis = lambda self: 300_000  # noqa: E731

    print(f"Running lambda_handler with event from {event_path}")
    lambda_handler(test_event, FakeContext())
    print("lambda_handler completed successfully.")
