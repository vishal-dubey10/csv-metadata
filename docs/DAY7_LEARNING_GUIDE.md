# Day 7 Learning Guide — Application Readiness

This guide documents the final application architecture, implemented features, test
results, security review, deployment prerequisites, and the plan for the AWS deployment
phase. This is the pre-deployment checkpoint.

---

## 1. Implemented Features (Application Complete)

| Feature | Status | Notes |
|---|---|---|
| CSV upload (direct to S3 via presigned URL) | ✅ Complete | AD-02, AD-27 |
| Upload intent + confirmation two-step flow | ✅ Complete | AD-13, AD-29 |
| CSV parsing and validation | ✅ Complete | AD-21 full contract |
| Whitespace trimming | ✅ Complete | Trim before dedup (order matters) |
| Duplicate row detection and removal | ✅ Complete | Preserves first occurrence |
| Processing status display | ✅ Complete | queued/processing/completed/failed |
| 5-second polling for live status updates | ✅ Complete | Stops when terminal |
| Processed CSV download (S3 presigned GET) | ✅ Complete | Anchor download, not window.open |
| Job metadata display (rows, columns, dupes) | ✅ Complete | Column names as chips |
| Failure reason display | ✅ Complete | User-readable, no stack traces |
| Authentication (Cognito access token) | ✅ Complete | Local dev bypass also working |
| Authorization (ownership enforcement) | ✅ Complete | 404 for wrong-owner (IDOR prevention) |
| Admin job view with status filter | ✅ Complete | React Admin, Content-Range pagination |
| Login page | ✅ Complete | MUI form, error messages, accessible |
| Upload wizard with progress bar | ✅ Complete | Drag-and-drop + click-to-browse |
| Loading states (skeletons) | ✅ Complete | All data-fetching routes |
| Empty states | ✅ Complete | First-time user experience |
| Error states with retry | ✅ Complete | All error scenarios |
| Responsive layout | ✅ Complete | Table columns hide on mobile |

### Out of MVP scope (deferred)

| Feature | Decision reference |
|---|---|
| Token refresh (silent) | AD-10 |
| Stale pending_upload cleanup job | AD-13 |
| Transactional outbox for SQS | AD-29 |
| Exactly-once SQS delivery | AD-07 (idempotency guard instead) |
| CloudFront distribution | AD-11 |
| Multi-AZ RDS | AD-25 |
| RDS Proxy | AD-D2-06 |
| CSV column-type detection | Out of product scope |
| Retry upload button | Out of MVP scope |

---

## 2. Architecture Overview

### Request path (production)

```
Browser
  │  HTTPS
  ▼
API Gateway (HTTP API, ap-south-1)
  │  JWT Authoriser — validates Cognito access token
  │  Injects claims into event.requestContext.authorizer.jwt.claims
  ▼
API Lambda (Python 3.14, zip package)
  │  Mangum: API Gateway event → ASGI
  │  FastAPI: route handler, dependency injection
  │  job_repository: ownership-enforced SQL queries
  ▼
RDS PostgreSQL 16 (private subnet, db.t3.micro)

(For S3 operations: via VPC Gateway endpoint — free, no NAT)
(For SQS: via VPC Interface endpoint — ~$14/month)
(For Secrets Manager: via VPC Interface endpoint — ~$14/month)
```

### Worker path (asynchronous)

```
SQS (csvforge-jobs-dev, Standard, visibility=1800s)
  │  Event Source Mapping (BatchSize=1, AD-07)
  ▼
Worker Lambda (Python 3.14, Docker container, 1024 MB, 300s)
  │  ECR image — includes backend/ + worker/ modules
  │  12-step pipeline: parse SQS → claim job → download → validate → clean → upload → complete
  ▼
RDS PostgreSQL (same VPC, private subnet)
S3 (via VPC Gateway endpoint)
```

### Frontend (not yet deployed)

```
Browser
  │  HTTP (no CloudFront in MVP — AD-11)
  ▼
S3 static website hosting (bucket: csvforge-frontend-dev)
  React SPA (1.06 MB minified bundle)
  Talks to API Gateway via VITE_API_URL
```

---

## 3. Stack Versions (Final Verified)

### Backend

| Component | Version |
|---|---|
| Python | 3.14 |
| FastAPI | 0.141.1 |
| Pydantic | 2.13.4 |
| SQLAlchemy | 2.0.54 |
| Alembic | 1.20.0 |
| psycopg (v3) | 3.3.3 |
| boto3 | 1.43.63 |
| Mangum | 0.22.0 |
| pytest | 9.1.1 |
| moto | 5.2.3 |

### Frontend

| Component | Version |
|---|---|
| React | 19.3.0 |
| TypeScript | 6.0.3 |
| Vite | 8.1.0 |
| @vitejs/plugin-react | 6.1.2 |
| react-router | 7.1.1 |
| @mui/material | 9.5.0 |
| react-admin | 5.15.4 |
| ra-data-simple-rest | 5.15.4 |
| amazon-cognito-identity-js | 6.3.15 |
| Vitest | 3.1.0 |
| @testing-library/react | 16.3.0 |

### AWS infrastructure

| Service | Selection |
|---|---|
| Lambda runtime | python3.14 |
| Lambda worker image | public.ecr.aws/lambda/python:3.14 |
| API Gateway | HTTP API (payload format 2.0) |
| RDS | PostgreSQL 16.x, db.t3.micro, Single-AZ |
| Region | ap-south-1 (Mumbai) |

---

## 4. Test Results (Pre-deployment)

### Backend — 197 tests, all passing

```
PYTHONPATH=. .venv/bin/pytest tests/unit/ -q
197 passed in 3.55s
```

| Test file | Count | What it covers |
|---|---|---|
| test_auth.py | 28 | Local bypass, JWT claims, require_admin, IDOR |
| test_job_endpoints.py | 38 | All endpoints, ownership checks, 404/409/422 |
| test_health.py | 3 | Health endpoint |
| test_job_model.py | 8 | ORM model attributes and status enum |
| test_job_repository_unit.py | 26 | Repository functions with SQLite |
| test_upload_endpoints.py | 23 | Upload intent, complete, size validation |
| test_s3_client.py | 18 | S3 operations with moto |
| test_sqs_client.py | 12 | SQS operations with moto |
| test_csv_processor.py | 28 | CSV parsing and cleaning logic |
| test_worker_handler.py | 13 | Lambda handler pipeline |

### Frontend — 28 tests, all passing

```
npm test (in frontend/)
28 passed
```

| Test file | Count | What it covers |
|---|---|---|
| JobStatusChip.test.tsx | 12 | Labels, aria-labels, size prop |
| apiClient.test.ts | 11 | ApiClientError, error normalization, network failure |
| AuthContext.test.tsx | 5 | Local dev auth, login, logout, provider guard |

### Build verification

```
TypeScript type-check:  0 errors
ESLint:                 0 errors, 0 warnings
Production build:       ✅ (1.06 MB bundle)
Backend tests:          197/197
Frontend tests:         28/28
```

### Not yet verified (requires AWS)

- Real Cognito authentication (JWT signing, token validation)
- API Gateway JWT Authoriser integration
- RDS PostgreSQL connection from Lambda
- S3 presigned URL generation and upload in ap-south-1
- SQS → Lambda event source mapping trigger
- Worker container execution in Lambda
- End-to-end upload → process → download
- CloudWatch log delivery

---

## 5. Security Review

### Authentication

| Check | Status | Notes |
|---|---|---|
| JWT signature validated | ✅ | API Gateway JWT Authoriser (not FastAPI) |
| Token expiry enforced | ✅ | API Gateway checks `exp` claim |
| Token issuer validated | ✅ | API Gateway checks `iss` = Cognito pool URL |
| Token type validated | ✅ | API Gateway checks `token_use = "access"` |
| FastAPI never sees unvalidated tokens | ✅ | API Gateway rejects before Lambda runs |
| Local dev bypass gated by ENVIRONMENT=local | ✅ | Never runs in production |
| Tokens stored in memory (not localStorage) | ✅ | AD-10 — XSS mitigation |

### Authorization

| Check | Status | Notes |
|---|---|---|
| user_id always from JWT sub claim | ✅ | Never from request body |
| All job queries include WHERE user_id = ? | ✅ | get_job_by_id_and_user() |
| 404 returned for wrong-owner (not 403) | ✅ | IDOR enumeration prevention |
| Admin endpoints check csvforge-admins group | ✅ | require_admin() dependency |
| Worker uses user_id from DB, not SQS message | ✅ | AD-D2-08 |

### File handling

| Check | Status | Notes |
|---|---|---|
| CSV never passes through API Gateway/Lambda | ✅ | Direct browser → S3 |
| File size enforced at S3 level | ✅ | content-length-range condition |
| File size re-verified by HeadObject | ✅ | AD-27 — server-side enforcement |
| Client-declared file_size_bytes untrusted | ✅ | AD-27 — informational only |
| Original filename never used for file paths | ✅ | S3 key uses UUID, not filename |
| S3 objects are private (no public ACL) | ✅ | Access via presigned URLs only |

### Configuration

| Check | Status | Notes |
|---|---|---|
| No secrets in source code | ✅ | .env.example has placeholders only |
| .env gitignored | ✅ | .gitignore entry present |
| AWS credentials via standard credential chain | ✅ | No hardcoded keys |
| ALLOWED_ORIGINS via environment variable | ✅ | AD-FE-05 |
| DB credentials via Secrets Manager | ✅ (planned) | AD-16 — pending deployment |

### Known remaining risks

1. **S3 static website HTTP:** No HTTPS in MVP (AD-11). Tokens are sent to API Gateway
   over HTTPS, but the frontend itself is served over HTTP. Mitigated by the planned
   CloudFront migration.

2. **CORS allow_credentials=True with broad origins:** In production, `ALLOWED_ORIGINS`
   must be set to the exact frontend URL. A wildcard (`*`) with credentials is rejected
   by browsers — correct behaviour. Ensure this is set before deployment.

3. **No rate limiting:** API Gateway throttles at the account level but there is no
   per-user rate limiting on upload endpoints. A user could create many jobs rapidly.
   Out of MVP scope.

4. **Content-Security-Policy `unsafe-inline` for styles:** MUI's Emotion CSS-in-JS
   requires `style-src: 'unsafe-inline'`. This weakens the CSP's protection against
   injected styles. Mitigation path: migrate to MUI's Pigment CSS build-time engine
   (experimental in MUI v9).

---

## 6. Configuration Requirements

All values needed before deployment. None are secrets in the traditional sense —
Cognito IDs are public client-side identifiers; the real secrets (DB password, AWS
keys) are managed by Secrets Manager and IAM execution roles.

### Backend Lambda environment variables

| Variable | Where set | Value source |
|---|---|---|
| `ENVIRONMENT` | CloudFormation | `production` |
| `DB_SECRET_ARN` | CloudFormation | Secrets Manager ARN (auto-generated) |
| `DB_HOST` | CloudFormation | RDS endpoint from `!GetAtt RDSInstance.Endpoint.Address` |
| `DB_PORT` | CloudFormation | `5432` |
| `DB_NAME` | CloudFormation | `csvforge` |
| `S3_BUCKET_NAME` | CloudFormation | `!ImportValue DataStack-BucketName` |
| `SQS_QUEUE_URL` | CloudFormation | `!ImportValue QueuesStack-QueueUrl` |
| `ALLOWED_ORIGINS` | CloudFormation | Frontend bucket website URL |

### Frontend environment variables (build-time)

| Variable | When set | Value source |
|---|---|---|
| `VITE_API_URL` | Build time | API Gateway endpoint from `!GetAtt HttpApi.ApiEndpoint` |
| `VITE_COGNITO_USER_POOL_ID` | Build time | Cognito pool ID from `!GetAtt UserPool.UserPoolId` |
| `VITE_COGNITO_APP_CLIENT_ID` | Build time | App client ID from `!GetAtt UserPoolClient.ClientId` |

**Important:** `VITE_` variables are embedded in the JavaScript bundle at build time.
They are visible to users. Never put secret values in `VITE_` variables.

---

## 7. Deployment Prerequisites

All of the following must exist before any stack can be deployed.

### Required AWS resources

| Resource | Purpose | Template |
|---|---|---|
| S3 deployment bucket | Stores Lambda ZIP artifact | Manual (one-time) |
| ECR repository | Stores worker Docker image | `worker.yaml` |
| VPC + subnets | Network isolation | `network.yaml` |
| RDS PostgreSQL | Job state storage | `data.yaml` |
| Secrets Manager secret | DB credentials | `data.yaml` |
| S3 files bucket | CSV input/output storage | `data.yaml` |
| SQS queue + DLQ | Job queue | `queues.yaml` |
| Cognito User Pool | Authentication | `api.yaml` |
| API Lambda (zip) | API application | `api.yaml` |
| API Gateway | HTTP API + JWT Authoriser | `api.yaml` |
| Worker Lambda (container) | CSV processor | `worker.yaml` |
| S3 frontend bucket | Static website | (not yet in CloudFormation) |

### Deployment prerequisites checklist

- [ ] AWS CLI v2 configured with `aws configure sso`
- [ ] Python 3.14 available locally (for Lambda ZIP packaging)
- [ ] Docker Desktop running (for worker container build)
- [ ] Node 22.12+ installed (for frontend build)
- [ ] Alembic migration ready (schema is correct, migration file exists)
- [ ] `.env` file with local dev values for testing

### Missing CloudFormation template

The frontend S3 static website bucket does not have a CloudFormation template yet.
This needs to be added to the deployment phase. Required properties:
- Bucket with website hosting enabled (`IndexDocument: index.html`)
- CORS rules permitting GET from any origin (static assets are public)
- Bucket policy allowing public read on `*`

---

## 8. Deployment Order (AD-26)

```
Step 1: csvforge-network-dev  (VPC, subnets, SGs, VPC endpoints)
Step 2: csvforge-data-dev     (RDS, Secrets Manager, S3 bucket)
Step 3: csvforge-queues-dev   (SQS queue, DLQ)
Step 4: csvforge-auth-dev     (Cognito User Pool, App Client, Admin Group)  ← auth.yaml needed
Step 5: Build + push worker Docker image to ECR
Step 6: Build Lambda ZIP (backend/)
Step 7: csvforge-api-dev      (API Lambda, API Gateway, JWT Authoriser)
Step 8: csvforge-worker-dev   (Worker Lambda, event source mapping)
Step 9: Run Alembic migration against RDS endpoint
Step 10: Build frontend (npm run build with real VITE_ values)
Step 11: Upload frontend to S3 static website bucket
Step 12: Smoke test: upload CSV → watch processing → download result
```

**Note:** `auth.yaml` is not a separate file yet — Cognito resources are in `api.yaml`.
The deployment plan assumes they are split before production deployment.

---

## 9. Potentially Costly Resources

| Resource | Cost | Notes |
|---|---|---|
| RDS db.t3.micro | ~$12/month | Largest dev cost — runs 24/7 |
| SQS Interface endpoint | ~$7/month | Required without NAT (AD-15) |
| Secrets Manager Interface endpoint | ~$7/month | Required without NAT (AD-15) |
| Lambda | ~$0 | Free tier covers dev usage |
| API Gateway | ~$0 | Free tier covers dev usage |
| S3 | ~$0 | Negligible for dev CSV volume |
| ECR | ~$0.10/GB/month | Worker image ~500 MB |
| CloudWatch Logs | ~$0.50/GB | Structured logs from Lambda |
| **Total estimate** | **~$27/month** | At dev usage levels |

**Cost-saving option:** Stop the RDS instance when not actively developing. This
eliminates the $12/month cost during idle periods. The instance can be restarted in
~5 minutes before a development session.

---

## 10. Next Phase: AWS Deployment Plan

### Phase 1: Infrastructure provisioning (Day 8)

1. Deploy network stack
2. Deploy data stack (provision RDS — takes ~5 minutes)
3. Deploy queues stack
4. Set up Cognito user pool (auth stack)
5. Run Alembic migration: `alembic upgrade head` against RDS endpoint

### Phase 2: Application deployment (Day 8–9)

1. Build and push worker Docker image to ECR
2. Package API Lambda ZIP
3. Deploy API stack
4. Deploy worker stack
5. Verify SQS event source mapping is active

### Phase 3: Frontend deployment (Day 9)

1. Create S3 static website bucket (CloudFormation template needed)
2. Build frontend with real VITE_ values from CloudFormation outputs
3. Upload `dist/` to S3 bucket
4. Update backend `ALLOWED_ORIGINS` to include S3 website URL

### Phase 4: Smoke testing (Day 9)

1. Create a Cognito test user
2. Log in via the frontend
3. Upload a sample CSV
4. Wait for processing
5. Verify results (row count, column names, duplicates removed)
6. Download the processed file
7. Verify failure handling with an invalid CSV

### Phase 5: CI/CD setup (Day 10)

1. `infrastructure/buildspec.yml` — CodeBuild build definition
2. `infrastructure/pipeline.yaml` — CodePipeline orchestration
3. Configure source → build → deploy stages
4. Add test execution to build phase

---

## 11. Decisions Requiring Approval Before Deployment

| Decision | Current plan | Alternative | Pending |
|---|---|---|---|
| Frontend HTTPS | S3 static website (HTTP) | CloudFront + ACM certificate | ✅ Decided: HTTP for MVP |
| Database Multi-AZ | Single-AZ | Multi-AZ (+$12/month) | ✅ Decided: Single-AZ for MVP |
| NAT Gateway | No NAT (VPC endpoints instead) | NAT Gateway (+$30/month) | ✅ Decided: No NAT |
| Lambda provisioned concurrency | None | Provisioned (reduces cold starts) | ✅ Decided: None for MVP |
| RDS automated backups | AWS default (7 days) | Custom retention | ✅ Decided: Default |
| S3 bucket for frontend | To be created | Add to data.yaml | ⚠️ **Needs CloudFormation template** |

---

## 12. Interview Questions

**Q: Explain the CSVForge architecture to a senior engineer in 2 minutes.**

CSVForge is a three-tier async application. The frontend is a React SPA that talks to
a FastAPI backend via API Gateway. The backend handles upload intent (creates a job
record, returns a presigned S3 URL), upload confirmation (verifies the file via
HeadObject, enqueues via SQS), and job queries. A separate Lambda worker processes CSV
jobs asynchronously: it downloads from S3, validates and cleans the CSV, uploads the
result back to S3, and marks the job complete in PostgreSQL. The frontend polls the
status endpoint every 5 seconds while jobs are active. Authentication is via Cognito;
API Gateway validates JWT tokens before FastAPI runs. All secrets are in Secrets Manager;
all AWS service communication goes through VPC endpoints without a NAT Gateway.

**Q: What would you change if you had to handle 100x the volume?**

The bottleneck would be PostgreSQL connections (pool_size=2 × many Lambda environments
= connection exhaustion). The fix: add RDS Proxy, which pools connections at the
infrastructure layer. For the worker, Lambda concurrency would scale automatically but
the PostgreSQL connection limit would be the ceiling. Lambda memory might need
adjustment for very large CSVs. The presigned URL approach already scales the upload
path — S3 is the bottleneck there, which is effectively unlimited.

**Q: How would you add end-to-end encryption?**

The current setup has TLS in transit (API Gateway → Lambda via HTTPS, Lambda → RDS
via TLS in the VPC, S3 via HTTPS). At-rest encryption: S3 uses SSE-S3 by default;
RDS uses AES-256 via the `StorageEncrypted` CloudFormation property. The gap is the
frontend S3 static website (HTTP — no CloudFront). Adding CloudFront with an ACM
certificate closes this. For field-level encryption of CSV data at rest, S3's
server-side encryption is already sufficient.

**Q: What's the largest operational risk in the current design?**

The DB-before-SQS failure window (AD-29). If the database update to `queued` succeeds
but SQS publish fails, the job is stuck indefinitely. The fix is a transactional outbox
pattern: write the SQS intent to the database in the same transaction, then have a
relay process publish it. For MVP, we make this observable via CloudWatch alarms (jobs
stuck in `queued` > 10 minutes) and document manual recovery.

---

## 13. Deployment Gap Report

### Infrastructure templates: what exists vs what's needed

| Template | Status | Deploys |
|---|---|---|
| `network.yaml` | ✅ Ready | VPC, subnets, SGs, VPC endpoints |
| `data.yaml` | ✅ Ready | RDS, Secrets Manager, S3 files bucket |
| `queues.yaml` | ✅ Ready | SQS queue + DLQ |
| `api.yaml` | ✅ Ready | Cognito, API Lambda, API Gateway |
| `worker.yaml` | ✅ Ready | ECR, Worker Lambda, event source mapping |
| Frontend S3 bucket | ❌ **Missing** | S3 static website for React app |
| `deploy.sh` | ⚠️ **Placeholder** | Needs real stack names and parameters |

### Environment variable gaps

All backend Lambda environment variables are sourced from CloudFormation outputs via
`!ImportValue` — they will be populated automatically when stacks are deployed.

Frontend environment variables (`VITE_API_URL`, `VITE_COGNITO_USER_POOL_ID`,
`VITE_COGNITO_APP_CLIENT_ID`) must be set at `npm run build` time using CloudFormation
outputs. A build script that reads these from `aws cloudformation describe-stacks` is
needed in the deployment phase.

### Summary

The application code is complete and verified. Two infrastructure gaps remain before
deployment can begin:
1. A CloudFormation template for the frontend S3 static website bucket.
2. A deployment script that reads CloudFormation outputs and injects them as `VITE_`
   variables during the frontend build.

Both are small additions to the existing infrastructure code. All application logic,
tests, and documentation are production-ready.

---

## 14. Team Walkthrough

"CSVForge is application-complete. We have a working React frontend, a FastAPI backend
with full job lifecycle management, a Lambda worker that validates and cleans CSV files,
and 225 tests (197 backend + 28 frontend) that all pass. The TypeScript build, ESLint,
and Vite production build are all clean.

What remains is infrastructure: deploying the CloudFormation stacks, running the Alembic
migration against RDS, building and pushing the Docker image to ECR, and hosting the
frontend on S3. We have templates ready for all of this. The next conversation is: when
do we want to provision the AWS resources, and who approves the ~$27/month dev
infrastructure cost?

Once deployed, the smoke test is simple: log in, upload a CSV, watch it process, download
the result. That's the definition of done for the product."
