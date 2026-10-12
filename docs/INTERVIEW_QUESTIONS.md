# CSVForge Interview Questions

Every question here is rooted in actual code, decisions, or architecture in this repository.
The cited file and function are the places to look when reviewing.

---

## Level 1 — Fundamentals (can you explain the concepts?)

**1. What is a presigned URL and why does CSVForge use one for file uploads?**

Relevant: `backend/s3_client.py` — `generate_presigned_upload_url()`, `docs/DECISIONS.md` AD-02.

A presigned URL is a time-limited, pre-authenticated HTTP URL that grants the bearer
permission to perform one specific S3 operation on one specific key. The signature is
computed server-side using IAM credentials and embedded in the URL query string. The
caller (browser) needs no AWS credentials.

CSVForge uses it because: API Gateway has a 10 MB payload limit, the file can be 50 MB,
and routing through Lambda wastes memory and execution time. The presigned URL lets the
browser upload directly to S3 — the API server never touches the bytes.

---

**2. What is an ORM and what are its trade-offs?**

Relevant: `backend/models/job.py`, `backend/db/job_repository.py`.

An ORM (Object-Relational Mapper) translates between Python objects and SQL tables.
`job = Job(id=..., status=...)` and `db.add(job)` becomes an `INSERT INTO jobs` statement.
Benefits: less SQL boilerplate, type safety, easier testing, automatic schema reflection.
Drawbacks: ORM-generated SQL can be inefficient (N+1 queries, over-fetching), and the
abstraction layer can hide performance problems.

---

**3. What is connection pooling and why does it matter for Lambda?**

Relevant: `backend/db/connection.py` — engine creation, `pool_size=1`.

A connection pool pre-creates and reuses database connections rather than opening a new
TCP connection on every request. Opening a PostgreSQL connection involves a TCP handshake,
TLS negotiation, and authentication — 10–50 ms of overhead. Pooling reduces this to near-zero.

For Lambda: each container handles one request at a time, so `pool_size=1` is optimal.
At 500 concurrent invocations, that's 500 open connections — predictable and within RDS limits.
If `pool_size` were 5, 500 concurrent invocations would open 2500 connections and likely hit
the `max_connections` limit on a db.t3.micro.

---

**4. What is the difference between authentication and authorization?**

Relevant: `backend/auth.py`.

Authentication (authn): verifying identity — "who are you?" API Gateway validates the JWT
signature, expiry, and issuer. A request with an invalid token never reaches FastAPI.

Authorization (authz): verifying permission — "can you do this?" FastAPI reads the validated
claims from the request context. `get_job_by_id_and_user()` adds `AND user_id = current_user.user_id`
to every query. Authentication happens at the API Gateway layer; authorization in the repository.

---

**5. What is a database transaction and why does `get_db()` use one?**

Relevant: `backend/db/connection.py` — `get_db()`.

A transaction groups related SQL operations into an atomic unit: either all succeed (commit)
or all are discarded (rollback). Without transactions, if a route creates a job row and then
crashes mid-request, the partial write stays in the database permanently.

`get_db()` opens a session (which starts a transaction), yields it to the route, commits on
success, rolls back on exception, and always closes the session in the `finally` block.

---

**6. What is the difference between `pending_upload` and `queued` in the job state machine?**

Relevant: `backend/models/job.py` — `JobStatus`, `backend/db/job_repository.py` — `list_jobs_for_user()`.

`pending_upload`: the job record exists in PostgreSQL but the S3 upload has not been confirmed.
The browser has a presigned URL but hasn't used it yet. This state is hidden from user-facing
list endpoints (AD-13) because the job doesn't meaningfully "exist" until the upload is confirmed.

`queued`: the S3 upload was confirmed (`HeadObject` verified), the job transitioned in PostgreSQL,
and an SQS message was published. The job is visible to the user and waiting for the Lambda worker.

---

**7. What is a finite state machine? How is it implemented in CSVForge?**

Relevant: `backend/models/job.py` — `JobStatus`.

A finite state machine has a fixed set of states and explicitly defined, controlled transitions
between them. In CSVForge, the five states are `pending_upload`, `queued`, `processing`,
`completed`, `failed`. Transitions are enforced by conditional UPDATEs in `job_repository.py`
that check the current state before changing it. Invalid transitions simply fail (`rowcount == 0`).

---

**8. What is a Pydantic model and why does CSVForge use them?**

Relevant: `backend/schemas.py`.

Pydantic models define the shape of request/response bodies with Python type annotations.
FastAPI automatically validates incoming JSON against them before the route handler runs.
Benefits: 422 with a clear error message on invalid input (before any business logic executes),
automatic JSON serialization, OpenAPI documentation generation, and type safety in route handlers.

---

## Level 2 — Implementation (can you explain the actual code?)

**9. Why does `claim_job_for_processing()` use a conditional UPDATE instead of SELECT + UPDATE?**

Relevant: `backend/db/job_repository.py` — `claim_job_for_processing()`.

A SELECT + UPDATE has a race condition: two workers can both SELECT `status='queued'`, both decide
to proceed, and both UPDATE to `processing`. Both process the same job.

A conditional UPDATE is atomic at the database level:
```sql
UPDATE jobs SET status='processing' WHERE id=? AND status IN ('queued','processing')
```
The check and the set happen in one statement. PostgreSQL's row-level locking ensures only one
UPDATE can write to a row at a time. `rowcount == 1` proves you won the race.

---

**10. Why does the worker upload to S3 BEFORE updating the database (AD-14)?**

Relevant: `worker/handler.py` — pipeline step 10 comment.

If the database update fails after the S3 upload: the job stays in `processing`, SQS retries,
the worker re-downloads and re-uploads the same deterministic S3 key (idempotent), then updates
the DB. Safe.

If S3 upload fails first: the database never says `completed`, SQS retries, the worker retries
the S3 upload. Safe.

The dangerous reverse order: update DB to `completed` first, then S3 upload fails. Now there's
a `completed` job record with a `processed_s3_key` that has no S3 object. The download endpoint
generates a presigned URL for a non-existent file. The user gets a 403 or 404 from S3 when they
try to download. No retry can fix this.

---

**11. Why does `get_job_by_id_and_user()` return 404 for both "not found" and "wrong owner"?**

Relevant: `backend/db/job_repository.py`, `backend/main.py` AD-13.

Returning 403 for wrong owner would reveal that the job exists. An attacker who guesses a UUID
would learn "this job exists but belongs to someone else" — useful for enumeration. Returning 404
for both cases gives the attacker no information. A UUID is not a security mechanism, but 404-for-wrong-owner
is defense in depth. The trade-off: legitimate "not found" errors look identical to "wrong owner" errors.

---

**12. What does `expire_on_commit=False` do and why is it set on `SessionLocal`?**

Relevant: `backend/db/connection.py` — `SessionLocal` configuration.

SQLAlchemy's default: after `db.commit()`, every attribute of every loaded ORM object is marked
"expired" (stale). The next attribute access triggers a `SELECT` to reload from the DB.

In FastAPI/Lambda: the sequence is `commit()` → FastAPI serializes `job.status` → `db.close()`.
After commit with the default setting, accessing `job.status` tries to issue a `SELECT`, but the
session may already be closing. This raises `DetachedInstanceError`.

With `expire_on_commit=False`: ORM objects retain their in-memory values after commit. FastAPI
can serialize `job.status` without another DB round-trip. Safe because each request gets a fresh
session — there's no "next request" that could see stale data from this session.

---

**13. How does the API Lambda get the database password without storing it in Lambda environment variables?**

Relevant: `backend/db/connection.py` — `_build_database_url()`, `docs/DECISIONS.md` AD-16.

When `ENVIRONMENT=production`, `_build_database_url()` reads `DB_SECRET_ARN` (the ARN of a
Secrets Manager secret) from the Lambda environment. It then calls `boto3.client("secretsmanager")
.get_secret_value(SecretId=arn)` which returns a JSON with `{"username": "...", "password": "..."}`.
The password is used to construct the URL and is never stored in environment variables or logs.

This call happens at module import time (once per Lambda cold start). Warm invocations reuse the
already-constructed `DATABASE_URL` string from module memory.

---

**14. Why does the worker read `user_id` from the database rather than from the SQS message?**

Relevant: `worker/handler.py` — pipeline step 9.

SQS messages are untrusted input. If an attacker can write messages to the SQS queue (e.g., through
a misconfigured IAM policy), they could craft a message with a different `user_id`. If the worker
used the SQS message's `user_id` to construct the output S3 key (`processed/{user_id}/{job_id}/result.csv`),
the attacker could land processed files in another user's S3 prefix.

The database record's `user_id` was set by the authenticated API endpoint using the verified Cognito
sub claim. It is the authoritative identity. The SQS message contains only `job_id` and `s3_key`.

---

**15. What is the CORS configuration in CSVForge and why is it necessary?**

Relevant: `backend/main.py` — `CORSMiddleware`, `infrastructure/data.yaml` — S3 CORS.

There are two CORS configurations:

1. **FastAPI CORS middleware**: the React frontend (origin: `http://localhost:5173`) sends API
   requests to FastAPI (origin: `http://localhost:8000`). Without CORS headers, the browser
   blocks the response. The middleware adds `Access-Control-Allow-Origin` and exposes the
   `Content-Range` header (needed by React Admin for pagination).

2. **S3 bucket CORS**: the browser PUTs the CSV file directly to S3 (origin: S3's domain).
   S3 needs to allow cross-origin PUT requests from `http://localhost:5173`. Without this,
   the presigned URL upload silently fails with a CORS error.

---

## Level 3 — Production scenarios

**16. A Lambda invocation fails with "connection refused" on the first DB call after being warm for 10 minutes. What is the root cause and fix?**

**Situation:** Lambda container was warm but idle. The PostgreSQL TCP connection in the pool went stale (network appliance timed out the idle TCP socket after ~350 seconds without either side sending data).

**Investigation:** Check CloudWatch logs for the Lambda function. Look for `psycopg.OperationalError: connection refused` or `broken pipe`. Check if the error only happens on warm invocations after idle periods.

**Root cause:** Stale connection in the pool. The pool believes the connection is alive; the first SQL statement fails because the TCP socket was silently closed by an intermediate NAT gateway or PostgreSQL's own idle timeout.

**Fix:** `pool_pre_ping=True` is already set in `connection.py`. Verify this is in place. The pre-ping sends a lightweight `SELECT 1` before handing the connection to the request — if the probe fails, SQLAlchemy discards the stale connection and opens a fresh one. A warm invocation that fails indicates `pool_pre_ping` was not set.

**Verification:** Deploy with `pool_pre_ping=True`. Invoke the Lambda, wait 10+ minutes, invoke again. No error.

**Prevention:** Always use `pool_pre_ping=True` for Lambda + RDS. Document the expected idle timeout so future engineers know why it's there.

---

**17. SQS has messages (ApproximateNumberOfMessagesVisible > 0) but no jobs are completing. What do you check?**

**Investigation steps:**

1. `aws cloudwatch get-metric-statistics` — check Lambda invocation count. Are invocations happening?
2. If invocations are 0: check the event source mapping — is it enabled?
   `aws lambda list-event-source-mappings --function-name csvforge-worker-dev`
3. If invocations are happening but jobs don't complete: check Lambda CloudWatch logs for errors.
   `aws logs tail /aws/lambda/csvforge-worker-dev --since 30m`
4. Check if the Lambda has VPC connectivity to RDS:
   - Look for `connection refused` or `timeout` on the PostgreSQL call.
   - Check the security group rules: does `RDSSecurityGroup` allow port 5432 from `LambdaSecurityGroup`?
5. Check if `claim_job_for_processing` returns False for every message:
   - Look for log line "Job X is in a terminal state or already claimed — skipping".
   - Check the job status in PostgreSQL — are jobs already `completed`?
6. Check the DLQ: `aws sqs get-queue-attributes --queue-url <dlq-url> --attribute-names ApproximateNumberOfMessages`.

---

**18. A user reports they can see another user's job. Where could authorization have failed?**

**Situation:** User A sees job data that belongs to User B.

**Investigation:**

1. Check `list_jobs_for_user()` in `job_repository.py` — does the WHERE clause include `Job.user_id == user_id`?
2. Check `get_current_user()` in `auth.py` — is `user_id` being extracted from `sub` (not `username` or `email`)?
3. Check `GET /jobs` route in `main.py` — is it calling `list_jobs_for_user(user_id=current_user.user_id)` and not `list_all_jobs()`?
4. Check if admin endpoint (`/admin/jobs`) is accidentally reachable without admin group membership.
5. In production: check if API Gateway JWT Authoriser is correctly attached to the route. If `AuthorizationType: NONE` was accidentally set, any user could pass any `sub` claim.

**Root cause (most likely):** A route calling `list_all_jobs()` instead of `list_jobs_for_user()`, or the auth dependency returning a hardcoded user_id for testing that was accidentally left in.

---

**19. A worker processes the same message twice and creates two processed files. What prevents this from corrupting the output?**

**Analysis:**

1. **Double-processing is expected with SQS Standard queues** — at-least-once delivery means rare duplicates.
2. **`claim_job_for_processing()` is the primary guard**: if both workers claim the same `queued` job,
   one gets `rowcount=1` and the other also gets `rowcount=1` (because `processing` is in the IN clause).
   Both workers proceed to process the same job. This is the case the docstring describes.
3. **S3 key is deterministic**: both workers write to `processed/{user_id}/{job_id}/result.csv`.
   The second write overwrites the first with identical content. No data corruption — just redundant work.
4. **`complete_job()` overwrites**: the second `complete_job()` call updates the DB row again with the
   same data. Idempotent.
5. **Full mutual exclusion**: not guaranteed by the current implementation alone. For stronger guarantees,
   add a check after claiming: re-read the job and verify `processed_s3_key` is still null before processing.

---

**20. The CloudFormation change set for `csvforge-api-dev` proposes replacing the API Lambda function. What do you check before approving?**

**Before approving a Replace:**

1. Read the change set detail: `aws cloudformation describe-change-set --change-set-name ...`
2. Understand WHY it's replacing (not updating). Common causes:
   - `FunctionName` changed (renaming the function)
   - `Runtime` changed in an incompatible way
   - A property that requires replacement was changed
3. Check if there is in-flight traffic or active Lambda invocations before replacing.
4. Verify the ZIP artifact at the new S3 key is correct and complete before applying.
5. Have a rollback plan: keep the previous ZIP artifact. Rolling back requires redeploying with the old key.
6. If this is production: deploy to dev first, run smoke tests, then apply to production.

**For RDS specifically**: a change set that proposes replacing an `AWS::RDS::DBInstance` means data loss. Always reject this and reconfigure the change to use `Modify` semantics (usually by not changing the DBInstanceIdentifier or engine version in incompatible ways).

---

## Spaced repetition schedule

| Session | Review these questions |
|---------|----------------------|
| After Day 1 labs | 1, 2, 3, 6, 8 |
| After Day 2 labs | 4, 7, 13, 14 |
| After Day 3 labs | 9, 10, 11, 15 |
| After Day 4 labs | 5, 12, 16, 17 |
| After Day 5 labs | All Level 1 |
| Before AWS labs | 13, 14, 17, 19, 20 |
| Final review | All Level 3 |
