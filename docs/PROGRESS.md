# CSVForge Progress

## Project Goal

Build CSVForge in 7 days as a deep-learning exercise. The goal is not maximum implementation speed — it is durable engineering understanding: being able to explain, debug, modify, and defend every component.

## Learning Method

For each CORE task:
1. Read the task and understand why it exists
2. Get the concepts explained
3. Answer prediction questions before implementation
4. Review proposed implementation
5. Implement
6. Run tests
7. Intentionally break something and debug it
8. Explain the implementation back (no looking at code)
9. Review the diff
10. Commit

All implementation uses supervised mode (not autopilot). One task at a time.

## Status

**Phase:** Pre-implementation — planning and architecture audit complete

**Audit date:** October 6, 2026

## Architecture Audit Complete

All issues identified in the planning audit have been resolved in the spec documents. See `docs/DECISIONS.md` for the canonical decision record. Issues resolved:

| Issue | Resolution | AD |
|-------|-----------|-----|
| Duplicate headers should be a permanent validation failure | Revised CSV contract — duplicates = InvalidCSVError | AD-21 |
| Worker memory: 512 MB too low for 50 MiB in-memory processing | Increased to 1024 MB; documented amplification factor | AD-01 |
| Worker Dockerfile missing backend/ modules | Added COPY backend/ to Dockerfile | AD-28 |
| Upload size not enforced server-side | content-length-range + HeadObject verification | AD-27 |
| Worker user_id from S3 key path (untrusted) | Use job.user_id from PostgreSQL DB | AD-08 |
| updated_at not updated on state transitions | Added onupdate=func.now() to SQLAlchemy model | AD-24 |
| DB-before-SQS failure window undocumented | Documented as known limitation; made observable | AD-29 |
| CloudFormation --query-stack-outputs (fictional flag) | Replaced with real AWS CLI patterns | AD-09 |
| CI/CD: build and deploy mixed in buildspec.yml | Separated: CodeBuild builds, Deploy stage deploys | AD-30 |
| S3 CORS missing for direct browser upload | Added to data.yaml and API CORS middleware | AD-28 |
| design.md §7.3 stale visibility timeout (360s) | Fixed to 1800s (matches AD-17) | AD-17 |
| design.md §3.13 React Router v6 (stale) | Updated to v8 | — |
| T-33 worker depends on API Gateway (incorrect) | Corrected: worker is sibling, not dependent | AD-26 |
| Cognito AllowedOAuthFlows: implicit unnecessary | Removed; documented flow in use | — |
| R-AUTH-04 "refresh handled by provider" ambiguous | Clarified: user must re-authenticate on expiry | AD-10 |
| FastAPI 0.140.0 → 0.141.1 | Updated in VERSIONS.md | AD-22 |

## Task Plan

See `.kiro/specs/csv-processing/tasks.md` for the complete 7-day plan.

**CORE tasks:** 28 tasks across Days 1–7  
**STRETCH tasks:** 5+ tasks (CI/CD hardening, CloudFront, etc.)

## Progress Log

*(Updated after each completed task)*

---

## Day 2 — AWS Infrastructure, Worker, and Documentation

**Completed:** Lambda worker (FEAT-003), IAM policy files, deployment scripts, pytest aws marker, AWS integration test skeleton, FAILURE_EXPERIMENTS.md, DAY2_LEARNING_GUIDE.md, aws-standards.md steering file, Day 2 decisions (AD-D2-01 through AD-D2-09), Day 2 infrastructure report.

### What Was Built

**FEAT-001 — Database Layer (Day 1)**
SQLAlchemy models, Alembic migrations, job repository with conditional UPDATE idempotency guard, full unit test suite.

**FEAT-002 — FastAPI REST API (Day 1)**
Upload intent endpoint (presigned PUT URL generation), upload complete endpoint (HeadObject + SQS publish), job status endpoint, download URL endpoint (presigned GET URL). PostgreSQL-backed, tested with moto for AWS mocks.

**FEAT-003 — Lambda Worker Handler (Day 2)**
`worker/handler.py`: receives SQS event, claims job from PostgreSQL, downloads raw CSV from S3, delegates to `csv_processor.py`, uploads processed CSV to S3, updates job to `completed`. Handles `InvalidCSVError` as permanent failure (clean return, job marked `failed`) vs all other exceptions as transient failures (re-raise, SQS retries). user_id sourced from DB record, not SQS message (AD-D2-08).

### Unit Test Count

**140 tests** — all passing. Run with:
```
cd /Users/vishal/csv-metadata && PYTHONPATH=. .venv/bin/pytest tests/unit/ -v
```

### AWS Resource Names (No Credentials)

| Resource | Type | Name |
|---|---|---|
| File storage | S3 Bucket | `csvforge-files-dev` |
| Job queue | SQS Queue | `csvforge-jobs-dev` |
| Failed jobs | SQS DLQ | `csvforge-jobs-dlq-dev` |
| Worker function | Lambda | `csvforge-worker-dev` |
| Worker permissions | IAM Role | `csvforge-worker-role-dev` |

Region: `ap-south-1` (Mumbai) — all resources in one region (AD-D2-01).

### Files Created / Modified in Day 2

**New files:**
- `worker/handler.py` — Lambda handler entry point (FEAT-003)
- `worker/csv_processor.py` — CSV parsing, validation, cleaning logic
- `worker/exceptions.py` — `InvalidCSVError`
- `worker/requirements.txt` — Worker pip dependencies
- `infrastructure/iam/lambda-execution-policy.json` — Lambda IAM permissions
- `infrastructure/iam/lambda-trust-policy.json` — Lambda trust policy
- `infrastructure/iam/api-service-policy.json` — API service IAM permissions
- `infrastructure/iam/README.md` — Least privilege documentation
- `infrastructure/create_dev_resources.sh` — Provision S3, SQS, IAM idempotently
- `infrastructure/deploy_lambda.sh` — Build zip, deploy Lambda, configure ESM
- `tests/integration/test_aws_integration.py` — Real-AWS integration tests (marked `aws`)
- `docs/FAILURE_EXPERIMENTS.md` — 4 failure scenarios with AWS CLI demonstrations
- `docs/DAY2_LEARNING_GUIDE.md` — Comprehensive architecture guide (23 interview Q&As)
- `.kiro/steering/aws-standards.md` — AWS/distributed systems coding standards
- `.agents/tasks/day2-report.md` — Implementation summary

**Modified files:**
- `pytest.ini` — Added `aws` marker
- `docs/DECISIONS.md` — Appended AD-D2-01 through AD-D2-09
- `docs/PROGRESS.md` — This Day 2 section

### What Day 3 Should Tackle

- **React frontend** — upload wizard (file select, presigned PUT, progress bar), job status polling (GET /jobs/{job_id} every 3s), download button (GET /jobs/{job_id}/download)
- **Cognito authentication** — user pool setup, hosted UI or custom login page, JWT token injection into API requests
- **API Gateway integration** — HTTP API with JWT authorizer pointing to Cognito user pool
- **CloudFormation stacks** — `network.yaml` (VPC, subnets, security groups), `compute.yaml` (Lambda functions, API Gateway)
- **CI/CD pipeline** — CodeBuild buildspec.yml (test → build Docker image → push ECR), CodePipeline (source → build → deploy)


---

## Days 3 & 4 — CSV Processing Pipeline, API, Authentication, and Authorization

**Completed:** October 9, 2026

### What Was Built

**CSV Processing Pipeline (Day 3)**

`worker/csv_processor.py` — `parse_and_analyse()` with full AD-21 validation contract, `clean()` with trim-before-dedup ordering, `write_csv()` with RFC 4180 CRLF output. Pure Python, no AWS dependencies.

`worker/handler.py` — 12-step pipeline: parse SQS → open DB → claim job (conditional UPDATE) → download from S3 → validate/parse CSV → clean → write /tmp → upload to S3 (before DB, AD-14) → update DB to completed. Permanent failures (InvalidCSVError) return cleanly; transient failures re-raise so SQS retries.

`worker/Dockerfile` — Lambda container image using `public.ecr.aws/lambda/python:3.14`. Build context is project root (not `./worker/`) per AD-18. Copies backend/ shared modules into LAMBDA_TASK_ROOT.

**API Layer (Day 4)**

`backend/schemas.py` — Added `JobResponse`, `JobListResponse`, `JobDownloadResponse`, `PostJobRequest`, `AdminJobResponse`. All use `from_attributes=True` for ORM compatibility.

`backend/auth.py` — `CurrentUser` dataclass, `get_current_user()` dependency (local bypass via `X-Dev-User-Id` header; production reads API Gateway JWT claims from `scope["aws.event"]["requestContext"]["authorizer"]["jwt"]["claims"]`), `require_admin()` dependency.

`backend/main.py` — Complete rewrite. All endpoints:
- `GET /health` (public)
- `POST /jobs` → 201 (REST alias for upload intent)
- `POST /uploads/intent` → 200 (original route, backwards-compatible)
- `POST /uploads/{job_id}/complete` → 200/404/422/409
- `GET /jobs` → 200 (ownership-filtered list, pending_upload excluded per AD-13)
- `GET /jobs/{job_id}` → 200/404 (ownership-checked, 404 for wrong owner per AD-13)
- `GET /jobs/{job_id}/download` → 200/404/409 (completed-only presigned URL)
- `GET /admin/jobs` → 200/403 (admin-only, Content-Range header for React Admin)

**Infrastructure (CloudFormation templates prepared, not yet deployed)**

`infrastructure/network.yaml` — VPC, 2 private subnets, Lambda/RDS/VPCEndpoint security groups, S3 gateway endpoint (free), SQS and Secrets Manager interface endpoints (AD-15).

`infrastructure/api.yaml` — Cognito User Pool + App Client + Admin Group, API Lambda (zip, python3.14), API Gateway HTTP API, JWT Authoriser, Lambda permission.

`infrastructure/worker.yaml` — ECR repository with lifecycle policy, Worker Lambda (container image, 1024 MB, 300s timeout), SQS event source mapping (BatchSize=1 per AD-07).

### Unit Test Count

**197 tests** — all passing. Run with:
```
cd /Users/vishal/csv-metadata && PYTHONPATH=. .venv/bin/pytest tests/unit/ -v
```

New test files added:
- `tests/unit/test_job_endpoints.py` — 38 tests for all new job endpoints including IDOR regression tests
- `tests/unit/test_auth.py` — 28 tests for the auth dependency (local bypass, production JWT claims, require_admin, cross-user ownership)

### Issues Found and Fixed During Testing

1. `asyncio.get_event_loop()` removed in Python 3.14 → replaced with `asyncio.run()` in `test_auth.py`
2. SQLite cannot bind Python `list` in a Core `UPDATE` statement (ARRAY `.with_variant()` only affects DDL) → `column_names=None` in unit test helpers; ARRAY tests belong in integration tests
3. SQLite `CURRENT_TIMESTAMP` has 1-second granularity → ordering assertion made order-agnostic

### Files Created / Modified in Days 3 & 4

**New files:**
- `worker/Dockerfile` — Lambda container image build
- `backend/auth.py` — authentication dependency
- `infrastructure/network.yaml` — VPC and networking
- `infrastructure/api.yaml` — API Lambda and API Gateway
- `infrastructure/worker.yaml` — Worker Lambda and ECR
- `tests/unit/test_job_endpoints.py` — job endpoint tests
- `tests/unit/test_auth.py` — auth dependency tests
- `docs/DAY3_LEARNING_GUIDE.md` — Day 3 architecture guide
- `docs/DAY4_LEARNING_GUIDE.md` — Day 4 architecture guide

**Modified files:**
- `backend/main.py` — complete rewrite with all endpoints
- `backend/schemas.py` — added job response schemas
- `.env.example` — fixed ap-south-1 regions, added Cognito vars

### AWS Resources (not yet deployed — pending Day 5/6)

| Stack | Resources | Status |
|---|---|---|
| csvforge-network-dev | VPC, subnets, SGs, VPC endpoints | Template ready |
| csvforge-api-dev | Cognito, API Lambda, API Gateway | Template ready |
| csvforge-worker-dev | ECR, Worker Lambda, ESM | Template ready |
| csvforge-data-dev | S3 bucket | Deployed (Day 2) |
| csvforge-queues-dev | SQS + DLQ | Deployed (Day 2) |

### Known Blockers (AWS Verification Phase)

- **RDS PostgreSQL:** Lambda worker requires RDS in the same VPC. Local Docker PostgreSQL is not reachable from Lambda. RDS provisioning is a Day 5/6 task.
- **Cognito User Pool:** JWT validation in API Gateway requires a real Cognito User Pool ID and App Client ID. Local dev uses `X-Dev-User-Id` bypass.
- **ECR image push:** Worker Lambda needs the Docker image pushed to ECR before deployment.
- **Actual SQS → Lambda → S3 → DB end-to-end:** Requires all above plus deployed CloudFormation stacks.

### What Days 5 & 6 Should Tackle

- CloudFormation network stack deployment (VPC, subnets, SGs, endpoints)
- RDS instance provisioning (db.t3.micro, PostgreSQL 16, private subnet)
- Alembic migration against real RDS
- Cognito User Pool creation and test user setup
- ECR repository creation and worker Docker image push
- API Lambda zip packaging and deployment
- End-to-end smoke test: upload CSV → SQS message → Lambda processes → download result
