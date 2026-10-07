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

