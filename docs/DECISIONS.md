# CSVForge Engineering Decisions

This document is the canonical record of every significant architectural and implementation decision.
Each entry explains what was decided, why, what the trade-off is, and what the alternatives were.
Cross-referenced to design.md sections and requirement IDs where applicable.

---

## AD-01 — Lambda is the CSV worker (not ECS/Fargate)

**Decision:** Use AWS Lambda (Docker container image) for the worker.

**Rationale:**
- The explicit learning goal includes the S3 → SQS → Lambda serverless pattern.
- A 50 MB CSV with trimming and deduplication runs well within Lambda limits: 300s timeout, 1024 MB memory, 512 MB /tmp.
- SQS is the decoupling point — if the worker needs to move to ECS later, the API and database do not change.

**Trade-off:** 15-minute execution ceiling; 50 MB file size is an explicit MVP constraint. Cold starts ~500ms–2s (acceptable for async processing).

**Alternatives:** ECS Fargate (no timeout, no size limit) — deferred to post-MVP scaling path.

---

## AD-02 — Direct browser → S3 upload via presigned URL

**Decision:** The browser uploads directly to S3 using a presigned PUT URL. The CSV never passes through Lambda or API Gateway.

**Rationale:**
- API Gateway HTTP API has a 10 MB payload limit.
- Routing 50 MB through Lambda wastes Lambda memory and execution time.
- Presigned URLs scope the write to a specific key and expire in 15 minutes.
- A `content-length-range` condition is embedded in the presigned URL to enforce the server-side file size limit (AD-27).

**Trade-off:** Two-step upload flow (intent + complete). The completion call is required because S3 does not notify the API when the upload finishes.

---

## AD-03 — React Admin for admin surface only

**Decision:** React Admin is used only for the admin view (list files, list jobs, filter by status). The user-facing flow (upload, status, download) uses plain React + MUI + React Router.

**Rationale:** The user flow is a wizard sequence — React Admin's CRUD list abstractions fight against that shape. React Admin is appropriate for the admin's data-grid / filter / detail pattern.

**Trade-off:** Two front-end patterns in the same codebase.

---

## AD-04 — PostgreSQL (RDS) for job state

**Decision:** Amazon RDS PostgreSQL (db.t3.micro) stores all job state.

**Rationale:** SQL queries, ACID transactions, admin status-filter queries work naturally with PostgreSQL. DynamoDB requires a GSI with eventual consistency for admin cross-user queries.

**Trade-off:** VPC setup required; higher minimum cost than DynamoDB.

---

## AD-05 — Worker Lambda deployed as Docker container via ECR

**Decision:** The worker Lambda uses `PackageType: Image` with an image pushed to ECR.

**Rationale:** Demonstrates Lambda container deployment pattern. Ensures consistent Python version and system libraries across dev, CI, and Lambda. psycopg binary is bundled in the image.

**Trade-off:** Larger cold starts; ECR setup required in CloudFormation.

---

## AD-06 — API Lambda deployed as zip package

**Decision:** The API Lambda uses `PackageType: Zip` with a Python 3.14 managed runtime. (Updated from 3.13 per AD-22.)

**Rationale:** No binary dependencies that justify container overhead. Zip is simpler to build and deploy.

---

## AD-07 — SQS batch size = 1

**Decision:** `BatchSize: 1` on the event source mapping.

**Rationale:** With batch size > 1, a Lambda exception causes SQS to retry the entire batch, including already-succeeded messages. With batch size 1, each message is independent. A failure retries exactly one job.

**Trade-off:** More Lambda invocations at scale (negligible at MVP volume).

**On ReportBatchItemFailures:** Removed. This feature is for batch size > 1 to report partial failures without retrying successful messages. With batch size = 1 it provides no value and adds unnecessary complexity.

---

## AD-08 — Worker idempotency via status check + conditional SQL UPDATE

**Decision:** Worker implements a two-step idempotency check:
1. Status check: if status is `completed` or `failed`, return immediately (no processing).
2. Conditional UPDATE: `WHERE status IN ('queued', 'processing')` — only 1 row updated means this worker owns the job.

**Rationale:** SQS guarantees at-least-once delivery, not exactly-once. Duplicate messages are expected. The conditional UPDATE is PostgreSQL's atomic way to assign ownership without a separate lock.

---

## AD-09 — Raw CloudFormation (not CDK)

**Decision:** Infrastructure defined in raw CloudFormation YAML.

**Rationale:** CDK abstracts which resources are created. For a learning project, raw CloudFormation provides full visibility into every resource, property, and dependency. Templates are independently deployable and understandable.

**Trade-off:** Verbose YAML; no type safety.

---

## AD-10 — In-memory token storage

**Decision:** Access and ID tokens are held in React component state (in-memory). They are lost on page refresh.

**Rationale:** localStorage and sessionStorage are accessible to any JavaScript on the page, creating XSS exposure. In-memory storage has no XSS risk.

**Trade-off:** User must log in again after a page refresh.

**MVP contract:** When the access token expires, the user must authenticate again. Silent refresh via Refresh Token stored in an HttpOnly cookie is out of MVP scope. The auth provider (Cognito) handles token expiry enforcement; the application does not implement refresh logic.

---

## AD-11 — No CloudFront in MVP

**Decision:** Frontend deployed as S3 static website (HTTP only). No CloudFront distribution.

**Rationale:** S3 static hosting is sufficient for a private dev/demo environment. CloudFront adds HTTPS, CDN, and DDoS mitigation — all correct for production but not needed for the MVP sprint.

**Future path:** Replace S3 static hosting with CloudFront + OAC. No application code changes required; only infrastructure changes.

---

## AD-12 — LocalStack is optional

**Decision:** Local development supports both LocalStack and real AWS dev account. Application code is identical; only environment variables differ.

**Rationale:** LocalStack can diverge from real AWS behaviour. A real dev account gives accurate behaviour with no emulator edge cases.

---

## AD-13 — pending_upload as an internal-only database state

**Decision:** The `jobs` table has five states. `pending_upload` is never surfaced to users. `GET /jobs` and `GET /jobs/{id}` filter it out (404 for direct access, excluded from lists).

**Rationale:** Cleanly models the window between the intent call and confirmed S3 upload without showing users a job that appears stuck.

**Known consequence:** Stale `pending_upload` rows accumulate if the browser never calls the completion endpoint. No data corruption; operational noise only. A cleanup job is out of MVP scope.

---

## AD-14 — Write S3 before PostgreSQL in worker

**Decision:** Worker writes the processed file to S3 before updating the database to `completed`.

**Rationale:** S3 writes are not transactional. If the DB update fails after an S3 write, the retry replaces the same S3 object (deterministic key) — harmless. If the DB write happened first and then S3 failed, we'd have a `completed` record with no output file — an unrecoverable inconsistency.

---

## AD-15 — VPC endpoint strategy (No NAT constraint)

**Decision:** Lambda functions (API and Worker) run in private subnets. AWS services are reached via VPC endpoints:

| Service | Endpoint type | Why |
|---------|--------------|-----|
| S3 | **Gateway endpoint** | Free; no per-hour cost; sufficient for same-region S3 access from Lambda |
| SQS | **Interface endpoint (PrivateLink)** | SQS does not support Gateway endpoints; Interface endpoint required |
| Secrets Manager | **Interface endpoint (PrivateLink)** | Secrets Manager does not support Gateway endpoints |
| RDS (PostgreSQL) | **No endpoint needed** — RDS is in the same VPC | Lambda connects to RDS via private VPC IP; no endpoint required |

**No NAT Gateway is provisioned.** All AWS service traffic stays within the VPC boundary.

**Gateway endpoints:** Free. Route table entry added to private subnet route tables. No DNS changes.

**Interface endpoints (SQS, Secrets Manager):** ~$0.01/hour each × 2 AZs ≈ $0.02/hour total. Approximately $14/month. Required for no-NAT operation.

**Security groups for interface endpoints:** Allow inbound 443 (HTTPS) from the Lambda security group.

**Subnet placement:** Lambda functions run in two private subnets (one per AZ). No public subnet NAT path exists. Interface endpoints placed in the same private subnets.

**Cognito:** Accessed from the browser (public internet), not from Lambda. No VPC endpoint needed.

---

## AD-16 — Database credentials: Secrets Manager (Option A)

**Decision:** PostgreSQL password is stored in AWS Secrets Manager. Lambda fetches the secret at runtime and constructs the `DATABASE_URL`.

**Rationale:**
- The original design (§11.3) specified Secrets Manager for the DB password.
- This is the standard AWS pattern for credential injection into Lambda.
- The plaintext password is never in Lambda environment variables or CloudFormation parameters.
- `DATABASE_URL` as an environment variable would expose the password in the Lambda console.

**Implementation:**
- CloudFormation creates `AWS::SecretsManager::Secret` with a generated password.
- Lambda environment variable: `DB_SECRET_ARN` (ARN only, not the secret value).
- At runtime: Lambda calls `secretsmanager:GetSecretValue` to retrieve `{"username":"...","password":"..."}`.
- The handler constructs `DATABASE_URL` from the retrieved values plus the `DB_HOST`, `DB_PORT`, `DB_NAME` environment variables (see AD-24).
- The Lambda IAM role gets `secretsmanager:GetSecretValue` on the specific secret ARN only.
- Local development: `.env` file contains `DATABASE_URL` directly (no Secrets Manager call).

**IAM policy (least privilege):**
```json
{
  "Effect": "Allow",
  "Action": "secretsmanager:GetSecretValue",
  "Resource": "<exact secret ARN>"
}
```

**Not used:** IAM database authentication (RDS IAM auth). Reason: requires configuring the RDS instance for IAM auth, generating ephemeral tokens via boto3, and more complex SQLAlchemy integration. Secrets Manager password auth is the standard, well-documented approach and is simpler for a learning sprint.

---

## AD-17 — SQS/Lambda timeout configuration

**Decision:**

| Parameter | Value | Reasoning |
|-----------|-------|-----------|
| Lambda Timeout | 300s (5 min) | Conservative ceiling for processing a 50 MB CSV |
| SQS Visibility Timeout | **1800s (30 min)** | AWS recommends **6× Lambda Timeout** = 6 × 300 = 1800s |
| Max Receive Count | 5 | AWS recommends ≥ 5 |
| Message Retention | 4 days | Sufficient for manual DLQ inspection |

**Why 6×:** The visibility timeout must be sufficiently longer than the expected Lambda processing time so the message does not become visible while the worker is still processing it. AWS recommends at least 6× as a starting value for queue-triggered functions. 6× is not an immutable law — it is chosen here because: Lambda timeout = 300s, 6 × 300 = 1800s gives substantial buffer beyond the worst-case execution, covering network variance and cold-start overhead. If Lambda execution regularly approaches 300s, increase both values together.

**Why maxReceiveCount = 5:** AWS documentation recommends maxReceiveCount ≥ 5 to give Lambda enough retry attempts before DLQ routing. With maxReceiveCount = 3 and Lambda occasionally failing due to cold starts or transient issues, valid jobs could reach the DLQ prematurely.

**Trade-off accepted:** A message can stay invisible for up to 30 minutes per attempt (5 attempts × 30 minutes = 2.5 hours worst case before DLQ). For async background processing this is acceptable.

---

## AD-18 — Docker build context for Worker Lambda

**Decision:** The Docker build context is the **project root**, not `./worker/`.

**Why:**  
Docker's build context determines what files are accessible to COPY instructions in the Dockerfile.  
- If context = `./worker/`, only files inside `worker/` are accessible. The Dockerfile cannot access shared modules from `backend/`.
- If context = `.` (project root), both `worker/` and `backend/` are accessible.

The correct build command is:
```bash
docker build -f worker/Dockerfile -t csvforge-worker .
```

Inside the Dockerfile, the `backend/` modules that the worker depends on are also copied:
```dockerfile
FROM public.ecr.aws/lambda/python:3.14
COPY worker/requirements.txt .
RUN pip install -r requirements.txt --no-cache-dir
COPY backend/ ${LAMBDA_TASK_ROOT}/backend/
COPY worker/ ${LAMBDA_TASK_ROOT}/
CMD ["handler.lambda_handler"]
```

This ensures `from backend.db.job_repository import ...` resolves correctly at runtime inside the container (see AD-28 for the full packaging decision).

---

## AD-19 — API Gateway JWT claims: how FastAPI accesses them

**Decision:** FastAPI reads JWT claims from the payload format 2.0 request context.

**Mechanism:**
1. API Gateway JWT authoriser validates the token signature against Cognito JWKS, verifies `iss` and `aud`, rejects expired tokens with 401. FastAPI never sees invalid tokens.
2. For valid tokens, API Gateway (HTTP API, payload format 2.0) passes the decoded JWT claims inside the Lambda event under `requestContext.authorizer.jwt.claims`.
3. Mangum (0.22.0) maps the format 2.0 event to an ASGI scope. The `requestContext` is placed in `scope["aws.event"]["requestContext"]`.
4. FastAPI accesses it via `request.scope["aws.event"]["requestContext"]["authorizer"]["jwt"]["claims"]`.
5. The `sub` claim = `user_id` (Cognito user's UUID).
6. The `cognito:groups` claim = list of Cognito group memberships. Admin check: `"csvforge-admins" in claims.get("cognito:groups", [])`.
7. `email` is NOT in the access token (see AD-23).

**Local dev bypass:** `ENVIRONMENT=local` → FastAPI reads `X-Dev-User-Id` and `X-Dev-User-Email` headers instead. This bypass is strictly gated by the environment variable.

**Not used:** Custom OIDC headers (`x-amzn-oidc-data`). That header is set by ALB's built-in OIDC integration, not by API Gateway JWT authorisers.

---

## AD-20 — IAM S3 resource scoping (no wildcards)

**Decision:** IAM policies reference the exact S3 bucket ARN from CloudFormation, not wildcards like `csvforge-files-*`.

**API Lambda IAM (s3 presigned URL generation):**
- `s3:PutObject` on `arn:aws:s3:::${BucketName}/uploads/*`
- `s3:GetObject` on `arn:aws:s3:::${BucketName}/uploads/*` (for HeadObject on complete endpoint)
- `s3:GetObject` on `arn:aws:s3:::${BucketName}/processed/*` (for presigned download URLs)

**Worker Lambda IAM:**
- `s3:GetObject` on `arn:aws:s3:::${BucketName}/uploads/*`
- `s3:PutObject` on `arn:aws:s3:::${BucketName}/processed/*`
- The Worker cannot read from `processed/` and cannot write to `uploads/`. Least privilege enforced by prefix.

**Note on HeadObject:** HeadObject requires `s3:GetObject` permission (not `s3:ListBucket`) on the object key.

The `!GetAtt FilesBucket.Arn` CloudFormation intrinsic function provides the exact ARN. No wildcards over bucket names.

---

## AD-21 — CSV validation contract (REVISED: duplicate headers are a permanent failure)

**Defined behaviour for all input types:**

**VALID inputs:**
- UTF-8 encoded file
- UTF-8 with BOM (UTF-8-SIG) — BOM stripped transparently
- CRLF line endings
- LF line endings
- Mixed CRLF/LF line endings
- Quoted fields (including quoted commas and quoted newlines)
- Header row only with zero data rows — valid; produces output with header only, `row_count=0`

**INVALID inputs — permanent failure (do NOT retry):**
- Empty file (0 bytes) → `InvalidCSVError("File is empty")`
- Invalid UTF-8 encoding → `InvalidCSVError("File encoding is not valid UTF-8: ...")`
- **Duplicate column names** → `InvalidCSVError("Duplicate column name: 'name' appears 2 times")` — previously documented as "valid" in this table; REVISED: duplicate headers cause silent data loss in `csv.DictReader` (last value wins, prior values dropped), which is unacceptable for a data cleaning tool
- Empty header name (column with no name) → `InvalidCSVError("Column has an empty name at position N")`
- Malformed CSV quoting (e.g., unclosed quote) → `InvalidCSVError("Malformed CSV: ...")`
- Rows with fewer fields than headers → `InvalidCSVError("Row N has M fields; expected K (headers: [...])")`
- Rows with more fields than headers → `InvalidCSVError("Row N has M fields; expected K (headers: [...])")`
- Entirely binary content → `InvalidCSVError("File is not a valid CSV: ...")`

**Permanent vs transient distinction:**
- **Permanent failure**: invalid file content → worker does NOT raise an exception → SQS deletes message → job marked `failed`.
- **Transient failure**: S3 throttling, RDS connection error, network timeout → worker RAISES an exception → SQS retries after visibility timeout expires.

**Why duplicate headers are now a permanent failure:**
`csv.DictReader` with duplicate column names silently overwrites earlier values with the last occurrence for any duplicate key. A CSV with `name,name,age` / `Alice,Bob,25` would produce `{'name': 'Bob', 'age': '25'}` — Alice is silently dropped. For a data cleaning tool, this constitutes undetectable data loss. The correct behaviour is to reject the file with a clear error so the user can fix their CSV.

---

## AD-22 — Python 3.14 (not 3.13)

**Decision:** Use Python 3.14 for both Lambda runtimes and local development.

**Rationale:**  
Python 3.14 is GA on Lambda (launched Nov 2025; deprecation ~Jun 2029). All key dependencies — psycopg 3.3.3, FastAPI 0.141.1, SQLAlchemy 2.0.54, boto3 1.43.63, Mangum 0.22.0 — explicitly support Python 3.14. No concrete incompatibility exists.

**Lambda base image:** `public.ecr.aws/lambda/python:3.14`  
**AD-06 update:** API Lambda runtime: `python3.14`

---

## AD-23 — Cognito access token is the API credential; email comes from the ID token at login time only

**Decision:** The API uses the Cognito **access token** (not the ID token) for all API requests. Identity is derived solely from JWT claims passed by API Gateway.

**Cognito access token claims:** `sub`, `cognito:groups`, `client_id`, `scope`, `token_use: "access"`, `username`, `iss`, `exp`, `iat`, `jti`. Does NOT contain: `email`, `email_verified`, or other user attributes.

**Identity mapping:**
- `user_id` = `sub` claim — authoritative, tamper-proof (validated by API Gateway before FastAPI runs)
- `cognito:groups` — authoritative, tamper-proof (from JWT)
- `user_email` — informational only; provided by frontend from ID token at upload intent time; stored for admin display; NEVER used for auth or ownership decisions

**Security boundary:**
- Authorization and ownership checks use only `user_id` from `sub`
- `user_email` in the request body is client-supplied; stored as `jobs.user_email` for admin display only
- A malicious client forging `user_email` causes no security consequence — ownership is always `user_id`-based

---

## AD-24 — Database credential bootstrap: split secret and endpoint

**Decision:** The Secrets Manager secret contains **username and password only**. The RDS endpoint (host), port (5432), and database name (`csvforge`) are passed as separate Lambda environment variables.

**Why:**  
If the secret contained `{"username","password","host","port","dbname"}`, there would be a circular dependency: CloudFormation creates the RDS instance and the Secret simultaneously, but the RDS endpoint isn't known until after provisioning. This would require a custom resource.

**Revised implementation:**
- Secret: `{"username":"csvforge","password":"<generated>"}` only
- Lambda env vars: `DB_SECRET_ARN` (ARN), `DB_HOST` (RDS endpoint from CloudFormation Output), `DB_PORT=5432`, `DB_NAME=csvforge`
- Runtime: Lambda calls `GetSecretValue(DB_SECRET_ARN)`, constructs `DATABASE_URL = postgresql+psycopg://{user}:{pass}@{DB_HOST}:{DB_PORT}/{DB_NAME}`
- Local: `.env` file has `DATABASE_URL` directly

---

## AD-25 — RDS subnet group spans two AZs; single-AZ instance runs in one AZ

**Decision (clarification):** The RDS DB subnet group is configured across two private subnets in different AZs. The RDS **instance** is Single-AZ and runs in **one** AZ.

**Why the subnet group needs two AZs:** AWS requires this as a prerequisite for creating any RDS instance in a VPC, regardless of Single-AZ vs Multi-AZ. It is not a high-availability feature — it is an API requirement. High availability requires explicitly enabling `MultiAZ: true`. We do NOT enable Multi-AZ for MVP (cost trade-off).

---

## AD-26 — API and Worker CloudFormation stacks are siblings, not parent/child

**Decision:** API and Worker stacks share foundational infrastructure but do not depend on each other.

**Worker dependencies:** VPC, RDS, S3, SQS, Secrets Manager, ECR. The Worker does NOT depend on API Gateway.

**Deployment ordering in deploy.sh:**
```
1. network.yaml   (foundational)
2. data.yaml      (foundational; depends on network)
3. queues.yaml    (foundational; depends on network)
4. auth.yaml      (foundational; independent of network)
5. api.yaml       (application; depends on network + data + queues + auth)
6. worker.yaml    (application; depends on network + data + queues; does NOT depend on api.yaml)
```
Steps 5 and 6 could run in parallel; they are sequential for operational simplicity.

---

## AD-27 — Upload size enforcement: content-length-range condition + HeadObject verification

**Decision:** File size is enforced at two independent layers:

1. **Presigned URL generation (API):** The presigned PUT URL includes a `content-length-range` condition: `['content-length-range', 0, 52428800]`. S3 rejects any PUT that falls outside this range at the time of the upload. The browser cannot upload more than 50 MiB regardless of what `file_size_bytes` was declared.

2. **Complete endpoint (API):** After the browser calls `POST /uploads/{job_id}/complete`, the API calls `HeadObject` on the uploaded object and verifies `ContentLength <= 52_428_800`. If `ContentLength` exceeds the limit, the API returns 422 and does NOT transition the job to `queued`.

**Why both layers:**
- The `content-length-range` condition prevents the upload itself. It is the primary enforcement.
- The `HeadObject` check catches any edge cases (e.g., presigned URL generation bug, policy misconfiguration) and enforces that the object in S3 matches the expected constraint before any further processing occurs.
- Client-declared `file_size_bytes` in the intent request is validated by Pydantic but is inherently untrusted. The authoritative size comes from `HeadObject.ContentLength` after the actual upload.

**Security principle:** Never trust client-supplied metadata for a security boundary. Verify the actual artifact.

---

## AD-28 — Worker packaging: copy backend/ into the container image

**Decision:** The worker Docker image copies both `worker/` and `backend/` into `LAMBDA_TASK_ROOT`.

**Rationale:** The worker requires `backend.db.connection`, `backend.db.job_repository`, `backend.models.job`, and `backend.s3_client`. These modules live in `backend/`. Rather than duplicating them in `worker/` or creating a shared package, the Dockerfile copies `backend/` directly into the container alongside `worker/`.

**What is shared:** `backend/db/`, `backend/models/`, `backend/s3_client.py`  
**What is worker-only:** `worker/handler.py`, `worker/csv_processor.py`, `worker/exceptions.py`  
**What is NOT in the container:** `backend/routers/`, `backend/schemas/`, `backend/main.py` (API-only code)

**How Docker imports resolve:** `LAMBDA_TASK_ROOT` is in Python's `sys.path` for Lambda container images. With `backend/` copied to `${LAMBDA_TASK_ROOT}/backend/`, `from backend.db.job_repository import ...` resolves correctly.

**How local execution differs:** Running `python worker/handler.py` locally requires the project root to be in `PYTHONPATH` so that `from backend...` resolves. The `run_local.sh` script sets `PYTHONPATH=$(pwd)` before invoking the handler.

**Trade-off:** The container image includes backend/ code that the worker doesn't use (routers, schemas). This is a minor image size overhead, acceptable for a learning project. A future optimization would be a shared internal package (`csvforge_shared/`) installable as a local wheel.

---

## AD-29 — DB-before-SQS failure window is a known documented limitation

**Decision:** In `POST /uploads/{job_id}/complete`, the database is updated to `queued` BEFORE the SQS message is published. This creates a failure window:

**Scenario:** `UPDATE status=queued` succeeds → `SQS.SendMessage` fails → the job is stuck in `queued` indefinitely.

**Why this is not a transactional outbox:**
A transactional outbox pattern would write the SQS message intent to the database in the same transaction as the status update, then have a separate relay process publish the SQS message. This guarantees exactly-once message delivery. For this MVP, the complexity is not justified.

**How this failure is made observable:**
- A CloudWatch metric alarm on jobs stuck in `queued` for > 10 minutes would surface this scenario.
- Lambda structured logs include `job_id` at each lifecycle step; a job with `queued` logged but no `processing` logged is a signal.
- This is documented as an interview-ready distributed systems discussion point.

**Recovery procedure:** Manually re-publish the SQS message for the stuck job_id, or implement a Lambda-based reaper that finds jobs stuck in `queued` > N minutes and re-queues them.

**Out of MVP scope:** Transactional outbox, automatic reaper job.

---

## AD-30 — CI/CD: CodeBuild builds; CodePipeline Deploy stage deploys

**Decision:** CodeBuild's `buildspec.yml` is responsible for building only: running tests, building the Docker image, pushing to ECR, building the frontend. All deployment actions (CloudFormation stack updates, S3 frontend sync) happen in a separate CodePipeline Deploy stage.

**Rationale:**
- Single responsibility: CodeBuild = build + test; CodePipeline = orchestrate + deploy
- Prevents duplicate deployments
- Clear failure semantics: build failure stops the pipeline before any deployment attempt

**Build artifacts passed to deploy stage:**
- `CODEBUILD_BUILD_ID` as the ECR image tag
- `frontend/dist/` as the frontend artifact

**Deploy stage:**
1. `aws cloudformation deploy --stack-name csvforge-api --template-file infrastructure/api.yaml`
2. `aws cloudformation deploy --stack-name csvforge-worker --template-file infrastructure/worker.yaml --parameter-overrides ImageTag=$IMAGE_TAG`
3. `aws s3 sync frontend/dist/ s3://${FRONTEND_BUCKET}/`

---

## AD-D2-01 — Region choice (ap-south-1)

**Decision:** All CSVForge dev resources are in ap-south-1 (Mumbai).

**Rationale:** Consistency — all clients, all resources, all CloudFormation stacks in one region. Cross-region calls add latency and complexity. Lambda in us-east-1 calling SQS in ap-south-1 would add ~150ms per operation.

**Trade-off:** Not the cheapest region (us-east-1 is ~20% cheaper for most services). Acceptable for a learning project where the developer is in India.

---

## AD-D2-02 — Standard SQS not FIFO

**Decision:** Use Standard SQS queue, not FIFO.

**Rationale:** (1) Each job_id is independent — ordering between different jobs is irrelevant. (2) Standard SQS is ~3× cheaper than FIFO for the same throughput. (3) Standard SQS has effectively unlimited throughput; FIFO is capped at 300 TPS. (4) The idempotency guard in the worker handles duplicate delivery, which Standard SQS may produce but FIFO would not.

**Trade-off:** At-least-once delivery (Standard) vs exactly-once delivery (FIFO). Mitigated by worker idempotency.

---

## AD-D2-03 — Presigned PUT not API proxy

**Decision:** Browser uploads directly to S3 via presigned PUT URL. The CSV never passes through FastAPI or Lambda.

**Rationale:** (1) API Gateway HTTP API has a 10 MB payload limit. (2) Routing 50 MB through Lambda wastes Lambda memory, execution time, and API Gateway bandwidth. (3) Presigned URLs scope the write to a specific key and expire in 15 minutes. (4) content-length-range condition enforces file size limit without the API touching the bytes.

**Security:** The API generates the presigned URL using its IAM role. The URL signature binds the IAM principal, bucket, key, method, and expiry — it is not a capability leak.

---

## AD-D2-04 — Visibility timeout 1800s

**Decision:** SQS visibility timeout = 1800 seconds (30 minutes).

**Rationale:** AWS recommends visibility timeout ≥ 6× Lambda timeout. Lambda timeout = 300s → 6 × 300 = 1800s. This gives substantial buffer beyond worst-case execution, covering network variance and cold-start overhead.

**Trade-off:** A crashed message stays invisible for up to 30 minutes per attempt. Across 5 retries, a permanent failure takes up to 2.5 hours to reach the DLQ. Acceptable for async background processing.

---

## AD-D2-05 — DLQ max receive count 5

**Decision:** MaxReceiveCount = 5 before message moves to DLQ.

**Rationale:** AWS documentation recommends maxReceiveCount ≥ 5. With MaxReceiveCount = 3, a job could reach the DLQ due to cold-start delays or transient network issues, not real failures. 5 gives enough retries to distinguish transient from permanent failures.

**Trade-off:** More retries means longer time before a truly broken job reaches the DLQ (5 × 1800s = 2.5 hours worst case).

---

## AD-D2-06 — Lambda pool_size=2 for DB connections

**Decision:** SQLAlchemy pool_size=2 in SessionLocal for the Lambda context.

**Rationale:** Lambda runs in many concurrent execution environments. A pool_size of 10 (SQLAlchemy default) × many concurrent Lambdas = PostgreSQL connection exhaustion. Pool size 2 with pool_pre_ping=True provides one active connection plus one overflow, appropriate for single-threaded Lambda invocations.

**Future:** RDS Proxy provides connection pooling at the infrastructure layer, making per-Lambda pool size less critical.

---

## AD-D2-07 — S3 BEFORE DB update ordering

**Decision:** Worker uploads processed file to S3 before calling complete_job() to update PostgreSQL.

**Rationale:** S3 writes are not transactional with PostgreSQL. If complete_job() fails after the S3 upload, the Lambda re-raise causes SQS to retry. On retry, the worker re-uploads the same S3 key (idempotent — S3 PutObject is idempotent on the same key), then retries complete_job(). If the DB write happened first and then S3 failed, we'd have a 'completed' record with no output file — an unrecoverable inconsistency. AD-14.

---

## AD-D2-08 — user_id from DB not SQS message

**Decision:** The processed S3 key uses user_id from the DB job record, not from the SQS message body.

**Rationale:** SQS messages are untrusted. If user_id came from the SQS message, a malicious actor who could publish to the SQS queue could specify any user_id in the message body, causing the worker to write processed files to any user's S3 prefix. By sourcing user_id from the DB record (written by the API when it created the job), the S3 key is determined by the authenticated API request, not the queue message.

**Security:** This prevents S3 path traversal attacks via SQS message spoofing.

---

## AD-D2-09 — Single bucket vs two buckets

**Decision:** Single S3 bucket csvforge-files-dev with uploads/ and processed/ prefixes.

**Rationale:** For development, one bucket is simpler to manage: one set of CORS rules, one bucket policy, one CloudFormation resource. The IAM policies enforce prefix-level separation (uploads/* vs processed/*), providing the same security boundary as two buckets.

**Production path:** Separate buckets per principle of least privilege — the Lambda worker's role would have no access to the uploads bucket at all, only the processed bucket. For MVP, the complexity is not justified.

