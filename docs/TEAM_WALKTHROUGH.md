# CSVForge Team Walkthrough

A structured 10–15 minute guide for presenting the project to a team, interviewer,
or senior engineer. Each section has a target duration and a talking point.

---

## Opening (1 minute)

**What to say:**

"CSVForge is an async CSV processing pipeline. Users upload a CSV through a React
frontend — the file goes directly to S3 without touching our API server. A Lambda
worker picks it up from SQS, cleans the data, and writes the result back to S3.
The user downloads the processed file via a presigned URL.

The interesting part isn't the CSV cleaning — it's the architecture: how we handle
the upload, how we coordinate state between S3, SQS, and PostgreSQL, and how we
ensure the worker doesn't process the same file twice."

---

## Section 1 — The upload flow (3 minutes)

**What to show:** `backend/main.py` — the two upload endpoints.

**Talking points:**

1. "The upload is a two-step flow. First, `POST /jobs` creates a job record in
   PostgreSQL with status `pending_upload` and returns a presigned S3 PUT URL.
   The browser never gets AWS credentials — just a time-limited signed URL for
   exactly one object."

2. "The browser PUTs the file directly to S3 — our API server never sees the bytes.
   This avoids the 10 MB API Gateway payload limit and means we're not paying Lambda
   execution time for file I/O."

3. "After the upload, the browser calls `POST /uploads/{id}/complete`. We do a
   `HeadObject` call to verify the file actually arrived and check the authoritative
   file size — we never trust the client-reported size. Then we publish to SQS."

**Key concept to mention:** presigned URL, S3 strong consistency (since 2020), HeadObject.

---

## Section 2 — Job state machine (2 minutes)

**What to show:** `backend/models/job.py` — `JobStatus` enum and the docstring diagram.

**Talking points:**

1. "Every upload creates one row in the `jobs` table. The state machine has five states:
   `pending_upload → queued → processing → completed/failed`. Only two are terminal."

2. "The state machine prevents invalid transitions. A job goes `pending_upload → queued`
   only when the upload is confirmed. It goes `queued/processing → processing` only
   through the worker's atomic claim operation."

3. "We use a conditional UPDATE instead of SELECT FOR UPDATE. The check and set happen
   in one SQL statement — atomic at the database level. `rowcount == 1` means we won;
   `rowcount == 0` means the job is already in a terminal state."

**Key concept to mention:** finite state machine, atomic conditional UPDATE, idempotency.

---

## Section 3 — The worker (2 minutes)

**What to show:** `worker/handler.py` — the pipeline comment at the top.

**Talking points:**

1. "The SQS event source mapping triggers the Lambda. The message contains just
   `{job_id, s3_key}` — the claim-check pattern. The actual file stays in S3."

2. "The first thing the worker does is claim the job: `UPDATE jobs SET status='processing'
   WHERE id=? AND status IN ('queued','processing')`. If `rowcount == 0`, the job is
   already done or someone else claimed it — we return without processing."

3. "S3 upload happens BEFORE the DB update. If the DB update fails, the job retries.
   The S3 key is deterministic so re-uploading is safe — idempotent. The reverse order
   would create a completed DB row pointing to a non-existent S3 object."

4. "Permanent failures — like an invalid CSV — return cleanly. That signals SQS to
   delete the message. Transient failures re-raise, so SQS retries up to 5 times,
   then the message moves to the DLQ."

**Key concept to mention:** at-least-once delivery, idempotency, S3-before-DB, DLQ.

---

## Section 4 — Authentication and authorization (2 minutes)

**What to show:** `backend/auth.py` — the production path in `get_current_user()`.

**Talking points:**

1. "API Gateway validates the JWT before FastAPI runs. If the token is expired or
   invalid, the request never reaches our code — API Gateway returns 401. FastAPI
   only reads the already-validated claims from the request context."

2. "The user identity is always the Cognito `sub` claim — an immutable UUID assigned
   at account creation. We never use email for authorization. If a user changes their
   email, the sub stays the same."

3. "Ownership is enforced at the query level. `get_job_by_id_and_user()` adds `AND
   user_id = current_user.user_id` to every ownership-sensitive query. A job that
   exists but belongs to another user returns 404 — not 403 — to prevent enumeration."

**Key concept to mention:** JWT, API Gateway JWT Authoriser, IDOR prevention, 404-not-403.

---

## Section 5 — Infrastructure (2 minutes)

**What to show:** `infrastructure/` directory listing, mention the 5 stacks.

**Talking points:**

1. "Five CloudFormation stacks, deployed in dependency order. Each stack exports
   outputs (VPC IDs, bucket ARNs, queue URLs) that downstream stacks import.
   CloudFormation enforces the creation order through these cross-stack references."

2. "Lambda runs in a private VPC with no internet access. It reaches S3, SQS, and
   Secrets Manager via VPC endpoints — no NAT gateway, which saves ~$33/month.
   RDS is only accessible from within the VPC."

3. "Two things that are NOT yet deployed: RDS (the biggest gap) and frontend hosting.
   All the code is production-ready, but we haven't run the CloudFormation deploy
   against a real AWS account yet."

**Key concept to mention:** CloudFormation, VPC endpoints, private subnets, deployment gap.

---

## Section 6 — Testing (1 minute)

**Talking points:**

1. "197 backend unit tests, 28 frontend tests, all passing. Unit tests use SQLite
   in-memory — they run in 4 seconds without Docker. Integration tests use real
   PostgreSQL (Docker Compose) to test ARRAY columns, TIMESTAMPTZ, and Alembic."

2. "AWS integration tests are marked with `@pytest.mark.aws`. They never run as
   part of the normal unit suite — you have to explicitly pass `-m aws`."

---

## Closing — what I'd do next (1 minute)

"The first thing I'd do is deploy the network and data stacks, then provision RDS.
Once I have a real PostgreSQL endpoint, I can run the Alembic migration, package the
API Lambda, and run the smoke test I've written in the runbook. The frontend is fully
functional locally — I just need the Cognito values from the deployed stack to inject
into the build."

---

## Common questions and answers

**Q: Why not stream through FastAPI instead of presigned URLs?**
A: API Gateway has a 10 MB payload limit. Lambda memory and execution time are wasted
buffering 50 MB files. Presigned URLs let S3 handle delivery — faster, cheaper, no code.

**Q: Why PostgreSQL instead of DynamoDB?**
A: The admin query (`GET /admin/jobs?status=failed`) scans all users. DynamoDB requires
a GSI with eventual consistency for cross-user queries. PostgreSQL handles this with a
simple WHERE clause and an index on `status`.

**Q: How do you prevent two workers from processing the same job?**
A: Atomic conditional UPDATE: `UPDATE jobs SET status='processing' WHERE status IN
('queued','processing')`. Only one UPDATE can win on a row at a time. `rowcount == 1`
means you own it. `rowcount == 0` means someone else got there first.

**Q: What happens if SQS delivers the message twice?**
A: The second worker calls `claim_job_for_processing()`. If the first worker completed
the job, the status is `completed` — not in `('queued','processing')` — so `rowcount == 0`,
and the second worker returns immediately without processing. If the first worker crashed
mid-process, the status is still `processing` — the second worker re-claims it and retries.

**Q: What is missing before going to production?**
A: RDS provisioning, running Alembic migration, ECR image push, frontend S3 bucket and
CloudFront, injecting Cognito values into the frontend build, and CI/CD pipeline.
