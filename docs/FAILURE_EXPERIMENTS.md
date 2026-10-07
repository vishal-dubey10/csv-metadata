# CSVForge Failure Experiments

This document catalogs four concrete failure scenarios in the CSVForge distributed system.
For each scenario, it answers: what failed, who detected it, who retried, what state was preserved,
where did the message go, and what prevents duplicate work.

These are not theoretical — each scenario can be triggered using the AWS CLI commands provided.

---

## Failure 1 — Lambda Fails Mid-Processing (Transient)

### What fails?

The Lambda worker raises an unhandled exception during CSV processing. This could be:
- A transient network error connecting to PostgreSQL
- S3 throttling on GetObject
- An out-of-memory error mid-way through processing a large CSV
- A cold start timeout
- An uncaught bug in csv_processor.py

### What happens to the SQS message?

When Lambda was invoked, SQS made the message **invisible** to all other consumers by applying
the visibility timeout (1800 seconds). While Lambda processes the message, no other worker can
see it.

When the Lambda function exits with an exception (non-zero exit / uncaught exception):
1. Lambda does **not** call `sqs:DeleteMessage` — the exception propagates out of `lambda_handler`
2. The message remains in the queue in the invisible state
3. After the visibility timeout expires (up to 30 minutes), the message becomes visible again
4. Lambda polls the queue, picks it up, and retries

The message is **never lost** — it either gets processed successfully or moves to the DLQ after 5 failures.

### What triggers the retry?

```
Lambda raises exception
  → Lambda runtime catches it, records failure in CloudWatch Logs
  → Lambda does NOT delete the SQS message
  → SQS visibility timeout expires (1800 seconds)
  → SQS makes message visible again
  → Lambda event source mapping polls SQS
  → Lambda invoked again with same message
```

The SQS `ReceiveCount` attribute on the message is incremented each time it becomes visible.

### What prevents infinite retry?

The `RedrivePolicy` on the queue sets `MaxReceiveCount = 5`. After the 5th failed delivery,
SQS automatically moves the message to `csvforge-jobs-dlq-dev`.

```
Attempt 1: ReceiveCount=1 → failure → message invisible for 1800s
Attempt 2: ReceiveCount=2 → failure → message invisible for 1800s
Attempt 3: ReceiveCount=3 → failure → message invisible for 1800s
Attempt 4: ReceiveCount=4 → failure → message invisible for 1800s
Attempt 5: ReceiveCount=5 → failure → SQS moves message to DLQ
```

Worst-case time to DLQ: 5 × 1800s = 9000 seconds (~2.5 hours).

### What state is the job in?

It depends on **when** the crash happened:

| Crash timing | PostgreSQL job status | What happens on retry |
|---|---|---|
| Before `claim_job_for_processing()` | `queued` | Retry calls `claim_job_for_processing()` normally — works correctly |
| After `claim_job_for_processing()`, before `complete_job()` | `processing` | Retry re-claims the job (idempotency guard allows it — see below) |
| After S3 write, before `complete_job()` | `processing` | Retry re-uploads S3 (idempotent), then calls `complete_job()` — works correctly |

### How does the worker's idempotency guard handle retry?

`claim_job_for_processing()` executes a conditional SQL `UPDATE`:

```sql
UPDATE jobs
SET status = 'processing', updated_at = NOW()
WHERE job_id = :job_id
  AND status IN ('queued', 'processing')
RETURNING job_id
```

The `WHERE status IN ('queued', 'processing')` clause is the idempotency guard:

- **If the job is still `processing`** (crashed worker never completed it): the UPDATE matches,
  the new invocation reclaims ownership. This is **correct** — the original invocation is dead.
- **If the job is `completed`** (first invocation somehow completed before the retry ran):
  the UPDATE does not match, `claim_job_for_processing()` returns `False`, and the handler
  returns immediately without re-processing.
- **If the job is `failed`**: same as `completed` — returns `False` immediately.

### AWS CLI command to observe/demonstrate

```bash
# Invoke Lambda directly with a bad payload (triggers exception in the worker)
aws lambda invoke \
  --function-name csvforge-worker-dev \
  --region ap-south-1 \
  --payload '{"Records":[{"body":"{\"job_id\":\"test-123\",\"s3_key\":\"uploads/user/test-123/bad.csv\"}"}]}' \
  /dev/stdout

# Check CloudWatch Logs for the error
aws logs filter-log-events \
  --log-group-name /aws/lambda/csvforge-worker-dev \
  --filter-pattern "ERROR" \
  --region ap-south-1

# Check DLQ after multiple failures
aws sqs get-queue-attributes \
  --queue-url https://sqs.ap-south-1.amazonaws.com/<ACCOUNT_ID>/csvforge-jobs-dlq-dev \
  --attribute-names ApproximateNumberOfMessages \
  --region ap-south-1
```

---

## Failure 2 — Duplicate Delivery (SQS At-Least-Once)

### What fails?

SQS is an **at-least-once** delivery system. In normal operation:

1. Lambda receives a message (ReceiveCount=1)
2. Lambda processes it successfully
3. Lambda calls `sqs:DeleteMessage`

However, due to network partitions or SQS internal replication, the same message can
be delivered to Lambda **twice**, even if the first invocation succeeded. This is by design
in SQS Standard queues — it guarantees delivery but not exactly-once delivery.

### The scenario (step by step)

```
SQS delivers message (job_id="abc-123") to Lambda invocation #1
  → Invocation #1: claim_job_for_processing("abc-123") → returns True (UPDATE row: queued→processing)
  → Invocation #1: downloads CSV from S3
  → Invocation #1: processes CSV, uploads result to S3
  → Invocation #1: complete_job("abc-123") → UPDATE row: processing→completed
  → Invocation #1: returns successfully → Lambda calls sqs:DeleteMessage

BUT: before the DeleteMessage reaches SQS, SQS's visibility timeout expired
(or SQS re-delivered due to internal replication):

SQS delivers message (job_id="abc-123") to Lambda invocation #2
  → Invocation #2: claim_job_for_processing("abc-123") runs this SQL:
      UPDATE jobs SET status='processing' WHERE job_id='abc-123'
        AND status IN ('queued','processing')
  → The job is already 'completed' — the WHERE clause does NOT match
  → claim_job_for_processing returns False
  → handler returns immediately (no CSV processing, no S3 write, no DB update)
  → Lambda calls sqs:DeleteMessage (cleans up the duplicate message)
```

### The job completes exactly once

Even though SQS delivered the message twice, the job was processed only once. The PostgreSQL
state transition is the single source of truth:

- `completed` means the work is done — no second worker will touch this job
- `failed` means the work was rejected — no retry will happen
- `processing` means exactly one worker currently owns it

This is the **idempotency guarantee** that makes at-least-once delivery safe.

### Connection to `job_repository.py`

`claim_job_for_processing()` in `backend/db/job_repository.py` implements the conditional UPDATE:

```python
result = session.execute(
    update(Job)
    .where(Job.job_id == job_id)
    .where(Job.status.in_(["queued", "processing"]))
    .values(status="processing", updated_at=func.now())
    .returning(Job.job_id)
)
return result.rowcount == 1  # True = claimed, False = already done
```

The `RETURNING` clause and `rowcount == 1` check are atomic — PostgreSQL guarantees that
exactly one concurrent UPDATE will succeed if two invocations race. The loser gets rowcount=0.

### AWS CLI command to demonstrate

```bash
# Send the same message twice to SQS
aws sqs send-message \
  --queue-url https://sqs.ap-south-1.amazonaws.com/<ACCOUNT_ID>/csvforge-jobs-dev \
  --message-body '{"job_id":"duplicate-test-456","s3_key":"uploads/user/duplicate-test-456/file.csv"}' \
  --region ap-south-1

# Send again immediately (same message body — same job_id)
aws sqs send-message \
  --queue-url https://sqs.ap-south-1.amazonaws.com/<ACCOUNT_ID>/csvforge-jobs-dev \
  --message-body '{"job_id":"duplicate-test-456","s3_key":"uploads/user/duplicate-test-456/file.csv"}' \
  --region ap-south-1

# Watch CloudWatch Logs — second invocation should log "Job already processed, skipping"
aws logs tail /aws/lambda/csvforge-worker-dev --follow --region ap-south-1
```

Expected log output for the second invocation:
```
INFO: Received SQS message for job_id=duplicate-test-456
INFO: claim_job_for_processing returned False — job already processed or claimed
INFO: Handler returning without processing
```

---

## Failure 3 — Missing or Corrupted S3 Object

### What fails?

The Lambda worker attempts to download the CSV from S3, but the object does not exist.

This can happen because:
- The browser never completed the upload (upload was interrupted after the job record was created)
- The S3 object was manually deleted
- The `s3_key` in the SQS message points to a wrong path (bug)
- S3 returned a transient 503/throttle that looks like a 404

### How the worker handles it

`download_file()` in `backend/s3_client.py` calls `s3.get_object(Bucket=bucket, Key=key)`.

If the object does not exist, boto3 raises:

```python
botocore.exceptions.ClientError: An error occurred (NoSuchKey) when calling the GetObject operation
```

This is **not** an `InvalidCSVError`. The handler code only catches `InvalidCSVError` for
clean failures. All other exceptions (including `ClientError`) propagate up:

```python
# In worker/handler.py:
try:
    # ... download, process, upload
except InvalidCSVError as e:
    # Permanent failure: bad CSV content — mark job failed, delete SQS message
    fail_job(job_id, str(e))
    return  # clean return → SQS deletes message
# Any other exception propagates → SQS retries
```

Because `ClientError` is not caught, it propagates out of `lambda_handler`, which signals
to the Lambda runtime that the invocation failed. SQS will retry after the visibility timeout.

### Permanent vs transient distinction

| Root cause | Error type | Behavior |
|---|---|---|
| Object genuinely missing (wrong key, deleted) | `ClientError(NoSuchKey)` | All 5 retries fail → DLQ. Permanent problem. |
| S3 temporarily throttling | `ClientError(SlowDown/503)` | Early retries fail, later succeed. Transient. |
| Object exists but 0 bytes | `download_file()` succeeds, `parse_and_analyse()` raises `InvalidCSVError("File is empty")` | Clean failure → job marked `failed`, message deleted |
| Object exists but corrupted binary content | `download_file()` succeeds, `parse_and_analyse()` raises `InvalidCSVError(...)` | Clean failure → job marked `failed`, message deleted |

The re-raise path handles **both** missing file (permanent) and throttling (transient) —
the DLQ catches permanent failures after exhausting retries. CloudWatch alarms on DLQ depth
surface permanent failures to the operations team.

### The key distinction

```
File missing:       ClientError → re-raise → 5 retries → DLQ  (needs human intervention)
S3 throttle:        ClientError → re-raise → retries succeed → job completes
Empty file:         InvalidCSVError → clean return → job=failed → message deleted (self-healing)
Malformed content:  InvalidCSVError → clean return → job=failed → message deleted (self-healing)
```

### AWS CLI command to observe

```bash
# Check if object exists before Lambda processes it
aws s3 ls s3://csvforge-files-dev/uploads/user/test-job-id/ --region ap-south-1

# Simulate a missing-file failure: send a job with a nonexistent key
aws sqs send-message \
  --queue-url https://sqs.ap-south-1.amazonaws.com/<ACCOUNT_ID>/csvforge-jobs-dev \
  --message-body '{"job_id":"missing-file-test","s3_key":"uploads/user/missing-file-test/does-not-exist.csv"}' \
  --region ap-south-1

# Watch Lambda logs for the ClientError
aws logs tail /aws/lambda/csvforge-worker-dev --follow --region ap-south-1

# After 5 failures, check the DLQ
aws sqs receive-message \
  --queue-url https://sqs.ap-south-1.amazonaws.com/<ACCOUNT_ID>/csvforge-jobs-dlq-dev \
  --max-number-of-messages 10 \
  --region ap-south-1
```

---

## Failure 4 — Repeated Failure → Dead Letter Queue

### What happens to a message that keeps failing?

The `RedrivePolicy` on `csvforge-jobs-dev` specifies `MaxReceiveCount = 5`.

After the 5th failed delivery attempt:
1. SQS increments `ReceiveCount` to 5
2. SQS automatically moves the message body to `csvforge-jobs-dlq-dev`
3. The message is **removed** from the main queue
4. Lambda stops retrying the message

The DLQ message has the same body as the original, plus a `MessageAttributes` record
with the original queue ARN. The `MessageRetentionPeriod` on the DLQ is 14 days (1209600 seconds).

### How to inspect the DLQ

```bash
# Check how many messages are in the DLQ
aws sqs get-queue-attributes \
  --queue-url https://sqs.ap-south-1.amazonaws.com/<ACCOUNT_ID>/csvforge-jobs-dlq-dev \
  --attribute-names ApproximateNumberOfMessages \
  --region ap-south-1

# Read up to 10 dead-letter messages (non-destructive by default)
aws sqs receive-message \
  --queue-url https://sqs.ap-south-1.amazonaws.com/<ACCOUNT_ID>/csvforge-jobs-dlq-dev \
  --max-number-of-messages 10 \
  --attribute-names All \
  --region ap-south-1
```

Note: `receive-message` does NOT delete messages. Messages become invisible temporarily
(default visibility timeout) and reappear after 30 seconds if not deleted.

### How to replay a message from the DLQ

When you have identified the root cause of the failure and fixed it, replay the message:

```bash
# Step 1: Receive the message from the DLQ (note the ReceiptHandle)
RECEIPT=$(aws sqs receive-message \
  --queue-url https://sqs.ap-south-1.amazonaws.com/<ACCOUNT_ID>/csvforge-jobs-dlq-dev \
  --query 'Messages[0].ReceiptHandle' \
  --output text \
  --region ap-south-1)

BODY=$(aws sqs receive-message \
  --queue-url https://sqs.ap-south-1.amazonaws.com/<ACCOUNT_ID>/csvforge-jobs-dlq-dev \
  --query 'Messages[0].Body' \
  --output text \
  --region ap-south-1)

# Step 2: Re-publish the message body to the main queue
aws sqs send-message \
  --queue-url https://sqs.ap-south-1.amazonaws.com/<ACCOUNT_ID>/csvforge-jobs-dev \
  --message-body "${BODY}" \
  --region ap-south-1

# Step 3: Delete the message from the DLQ (it has been replayed)
aws sqs delete-message \
  --queue-url https://sqs.ap-south-1.amazonaws.com/<ACCOUNT_ID>/csvforge-jobs-dlq-dev \
  --receipt-handle "${RECEIPT}" \
  --region ap-south-1
```

Alternatively, use `change-message-visibility` to make the DLQ message immediately visible
(set visibility timeout to 0) before re-sending:

```bash
# Make the message immediately visible again in the DLQ
aws sqs change-message-visibility \
  --queue-url https://sqs.ap-south-1.amazonaws.com/<ACCOUNT_ID>/csvforge-jobs-dlq-dev \
  --receipt-handle "${RECEIPT}" \
  --visibility-timeout 0 \
  --region ap-south-1
```

### Why the DLQ matters for operations

**Without a DLQ:** After `MaxReceiveCount` failures, SQS simply deletes the message. The work
disappears with no record. There is no way to know which jobs failed, why, or how many times.

**With a DLQ:** Dead letters are **auditable proof** that work was attempted and failed.
You can:
- Query the DLQ to find failed job IDs
- Cross-reference with CloudWatch Logs to find the error (by `job_id`)
- Fix the root cause
- Replay the messages to re-process the jobs
- Measure the failure rate over time

### DLQ CloudWatch alarm

Set a CloudWatch metric alarm to alert when any messages appear in the DLQ:

```bash
# Create a CloudWatch alarm: alert when DLQ has > 0 messages
aws cloudwatch put-metric-alarm \
  --alarm-name "CSVForge-DLQ-NonEmpty" \
  --alarm-description "Dead letter queue has messages — worker failures detected" \
  --metric-name ApproximateNumberOfMessagesVisible \
  --namespace AWS/SQS \
  --statistic Average \
  --period 60 \
  --threshold 0 \
  --comparison-operator GreaterThanThreshold \
  --dimensions Name=QueueName,Value=csvforge-jobs-dlq-dev \
  --evaluation-periods 1 \
  --alarm-actions arn:aws:sns:ap-south-1:<ACCOUNT_ID>:csvforge-alerts \
  --region ap-south-1
```

`ApproximateNumberOfMessagesVisible > 0` on the DLQ means a job has permanently failed.
This is distinct from the main queue depth (which measures pending load, not failures).

---

## Summary Table

| Scenario | Who detected? | Who retried? | Message fate | Duplicate guard |
|---|---|---|---|---|
| Lambda crash (transient) | SQS visibility timeout expiry | SQS re-delivery → Lambda | Retries → DLQ after 5 | `claim_job_for_processing` conditional UPDATE |
| Duplicate delivery (at-least-once) | Worker (claim returns False) | No retry needed | Duplicate deleted | `WHERE status IN ('queued','processing')` |
| Missing S3 object | Worker (ClientError) | SQS re-delivery → Lambda | Retries → DLQ after 5 | No re-processing (job already completed or still fails) |
| Repeated failures | SQS (MaxReceiveCount=5) | None — escalated to DLQ | Moved to DLQ | DLQ holds message for manual inspection/replay |
