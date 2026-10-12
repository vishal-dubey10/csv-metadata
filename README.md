# CSVForge

A full-stack CSV processing pipeline built as a practical engineering learning laboratory.

Users upload CSV files through a React frontend. The files land directly in S3 via a
presigned URL — the API server never touches the bytes. A Lambda worker reads the CSV,
cleans it (trimming whitespace, removing duplicate rows), and writes the result back
to S3. The user downloads the processed file via a second presigned URL. A React Admin
panel gives administrators a live view of all jobs.

---

## Architecture

```
Browser (React + React Admin)
    │
    │  HTTPS (Authorization: Bearer <access_token>)
    ▼
API Gateway (HTTP API) — JWT Authoriser validates Cognito access token
    │
    ▼
FastAPI on Lambda (Python 3.14, Mangum ASGI adapter)
    │                     │
    │ SQLAlchemy ORM       │ boto3
    ▼                     ▼
PostgreSQL 16        AWS S3 bucket
(RDS, private VPC)   uploads/  processed/
                          │
                          │ SQS message: {job_id, s3_key}
                          ▼
                   Lambda Worker (Python 3.14, container image)
                   Reads CSV → cleans → writes processed CSV
                   Updates job status in PostgreSQL
```

### Component status

| Component | Technology | Status |
|-----------|-----------|--------|
| API | FastAPI + Mangum (Lambda) | ✅ Built and unit-tested |
| Database | PostgreSQL 16 via SQLAlchemy 2.0 + psycopg 3 | ✅ Built and unit-tested |
| Storage | AWS S3 (presigned URLs, CORS) | ✅ Built, tested with moto |
| Queue | AWS SQS (main queue + DLQ) | ✅ Built, tested with moto |
| Worker | Python Lambda (container image) | ✅ Built and unit-tested |
| Frontend | React 19 + TypeScript + MUI v9 + React Admin | ✅ Built and unit-tested |
| CloudFormation stacks | network, data, queues, api, worker | ✅ Templates ready, not yet deployed |

---

## Technology stack

| Layer | Technology | Version |
|-------|-----------|---------|
| Language (backend/worker) | Python | 3.14.7 |
| Web framework | FastAPI | 0.141.x |
| ASGI server (local) | Uvicorn | 0.34.x |
| Lambda adapter | Mangum | 0.22.0 |
| ORM | SQLAlchemy | 2.0.x |
| DB driver | psycopg | 3.x |
| Migrations | Alembic | 1.13.x |
| AWS SDK | boto3 / botocore | latest |
| Language (frontend) | TypeScript | 6.0.3 |
| Build tool | Vite | 8.x |
| UI framework | React | 19.x |
| Component library | MUI | v9 |
| Admin panel | React Admin | 5.15.4 |
| Router | React Router | v7 |
| Test runner (frontend) | Vitest | latest |
| Database (production) | PostgreSQL | 16 |
| Database (unit tests) | SQLite in-memory | — |
| AWS region | ap-south-1 (Mumbai) | — |

---

## Local setup

### Prerequisites

- Python 3.14.7 (via pyenv or system install)
- Node.js 22.x
- Docker Desktop (for PostgreSQL integration tests)
- AWS CLI v2 configured (for AWS labs only)

### Backend

```bash
# From the project root
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt -r backend/requirements-dev.txt

# Copy environment config
cp .env.example .env
# Edit .env — set DATABASE_URL for local PostgreSQL

# Start local PostgreSQL via Docker Compose
docker compose up -d

# Run Alembic migration
PYTHONPATH=. .venv/bin/alembic upgrade head

# Start the API server
PYTHONPATH=. ENVIRONMENT=local .venv/bin/uvicorn backend.main:app --reload
```

The API is available at http://localhost:8000.
Interactive API docs: http://localhost:8000/docs

### Frontend

```bash
cd frontend
npm install
cp .env.example .env.local
# Edit .env.local — set VITE_API_BASE_URL=http://localhost:8000
npm run dev
```

The frontend is available at http://localhost:5173.

---

## Environment variables

### Backend (.env for local dev)

| Variable | Description | Example |
|----------|-------------|---------|
| `ENVIRONMENT` | `local` uses DATABASE_URL directly; `production` uses Secrets Manager | `local` |
| `DATABASE_URL` | Full SQLAlchemy connection URL (local only) | `postgresql+psycopg://postgres:postgres@localhost:5432/csvforge` |
| `S3_BUCKET_NAME` | S3 bucket for uploads and processed files | `csvforge-files-dev` |
| `SQS_QUEUE_URL` | Full SQS queue URL for job messages | `https://sqs.ap-south-1.amazonaws.com/...` |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins | `http://localhost:5173` |
| `DB_SECRET_ARN` | Secrets Manager ARN (production only) | `arn:aws:secretsmanager:...` |
| `DB_HOST` | RDS endpoint hostname (production only) | `csvforge.abc.ap-south-1.rds.amazonaws.com` |

### Frontend (.env.local)

| Variable | Description | Example |
|----------|-------------|---------|
| `VITE_API_BASE_URL` | Backend API base URL | `http://localhost:8000` |
| `VITE_COGNITO_USER_POOL_ID` | Cognito User Pool ID (empty = local dev bypass) | `ap-south-1_XXXXXXXX` |
| `VITE_COGNITO_CLIENT_ID` | Cognito App Client ID | `abc123...` |

---

## Running tests

### Backend unit tests (fast, no Docker required)

```bash
cd /Users/vishal/csv-metadata
PYTHONPATH=. .venv/bin/pytest tests/unit/ -v
```

Expected: **197 tests, all passing**. Uses SQLite in-memory. Runs in ~4 seconds.

### Backend integration tests (requires Docker PostgreSQL)

```bash
docker compose up -d
PYTHONPATH=. .venv/bin/pytest tests/integration/ -v
```

Tests ARRAY columns, TIMESTAMPTZ, MVCC, and Alembic migrations against real PostgreSQL.

### Real AWS integration tests (requires deployed infrastructure + credentials)

```bash
PYTHONPATH=. .venv/bin/pytest -m aws -v
```

These tests hit real S3, SQS, and RDS. Never run accidentally as part of the unit suite.
See `tests/integration/test_aws_integration.py`.

### Frontend tests

```bash
cd frontend
npm test
```

Expected: **28 tests, all passing**.

---

## Upload-to-download workflow

The complete user journey, step by step:

1. **Upload intent** — `POST /jobs` with `{user_email, original_filename, file_size_bytes}`.
   The API creates a Job row (status: `pending_upload`) and returns a presigned S3 PUT URL.

2. **Direct S3 upload** — The browser PUTs the CSV file directly to S3 using the presigned URL.
   The API server is not involved. The file size limit (50 MiB) is enforced by S3 via
   a `content-length-range` condition embedded in the presigned URL signature.

3. **Upload confirmation** — `POST /uploads/{job_id}/complete`.
   The API verifies the S3 object exists, checks the actual file size via `HeadObject`,
   transitions the job to `queued`, and publishes an SQS message: `{job_id, s3_key}`.

4. **Worker processing** — The SQS event source mapping triggers the Lambda worker.
   The worker claims the job atomically (conditional UPDATE), downloads the CSV from S3,
   validates it, trims whitespace, removes duplicate rows, uploads the result to S3
   (`processed/{user_id}/{job_id}/result.csv`), and marks the job `completed`.

5. **Polling** — The frontend calls `GET /jobs/{job_id}` every 5 seconds until the
   job status is `completed` or `failed`.

6. **Download** — `GET /jobs/{job_id}/download` returns a presigned S3 GET URL.
   The browser downloads the processed CSV directly from S3.

---

## AWS architecture overview

Five CloudFormation stacks, deployed in dependency order:

| Stack | Template | Contents |
|-------|---------|----------|
| `csvforge-network-dev` | `infrastructure/network.yaml` | VPC, 2 private subnets, security groups, S3 gateway endpoint, SQS + Secrets Manager interface endpoints |
| `csvforge-data-dev` | `infrastructure/data.yaml` | S3 bucket (encrypted, CORS-configured, public access blocked) |
| `csvforge-queues-dev` | `infrastructure/queues.yaml` | SQS main queue + DLQ (redrive after 5 failures, 1800s visibility timeout) |
| `csvforge-api-dev` | `infrastructure/api.yaml` | Cognito User Pool + App Client, API Lambda (zip), API Gateway HTTP API, JWT Authoriser |
| `csvforge-worker-dev` | `infrastructure/worker.yaml` | ECR repository, Worker Lambda (container image), SQS event source mapping |

**Not yet in CloudFormation (deployment gaps):**
- RDS PostgreSQL instance — required by both Lambda functions
- Frontend S3 bucket (for static hosting)
- CloudFront distribution (optional, for HTTPS on the frontend)

See `docs/AWS_DEPLOYMENT_RUNBOOK.md` for the step-by-step deployment guide.

---

## Known limitations

- **RDS not yet deployed** — the application cannot run end-to-end in AWS until an RDS instance is provisioned. The worker and API both require the PostgreSQL VPC connection.
- **Frontend not yet hosted** — no CloudFormation template for the frontend S3 bucket or CloudFront distribution.
- **DB-before-SQS gap** — if the API updates the DB to `queued` but the SQS `SendMessage` call fails, the job is stuck in `queued` with no worker trigger. No retry or outbox pattern is currently implemented (AD-29).
- **updated_at not set on bulk UPDATEs** — SQLAlchemy's `onupdate=func.now()` does not fire for Core `UPDATE` statements. The worker would need to explicitly include `updated_at` in bulk update VALUES to maintain accurate timestamps.
- **50 MiB file limit** — hardcoded in both the presigned URL `content-length-range` condition and the completion endpoint.
- **No refresh token rotation** — users must re-authenticate when the 1-hour access token expires (AD-10).
- **No CloudFront** — the frontend S3 static website serves over HTTP; HTTPS requires CloudFront.

---

## Documentation

| Document | Purpose |
|----------|---------|
| `docs/MENTAL_MAP.md` | One-page architecture and job lifecycle revision |
| `docs/AWS_DEPLOYMENT_RUNBOOK.md` | Step-by-step AWS deployment and verification |
| `docs/TEAM_WALKTHROUGH.md` | 10–15 minute project presentation guide |
| `docs/LEARNING_LABS.md` | Lab index, commands, objectives, and revision tasks |
| `docs/INTERVIEW_QUESTIONS.md` | Interview questions linked to real implementation |
| `docs/PROGRESS.md` | Completed work, evidence, and next steps |
| `docs/DECISIONS.md` | Architecture Decision Records (AD-01 through AD-FE-05) |
| `docs/FAILURE_EXPERIMENTS.md` | Controlled failure scenarios and diagnostic procedures |
| `docs/DAY1_LEARNING_GUIDE.md` | Backend foundations deep-dive |
| `docs/DAY2_LEARNING_GUIDE.md` | S3, SQS, and worker architecture |
| `docs/DAY3_LEARNING_GUIDE.md` | CSV processing pipeline |
| `docs/DAY4_LEARNING_GUIDE.md` | API, authentication, and authorization |
| `docs/DAY5_LEARNING_GUIDE.md` | React frontend architecture |
| `docs/DAY6_LEARNING_GUIDE.md` | Integration and contracts |
| `docs/DAY7_LEARNING_GUIDE.md` | Deployment readiness |

---

## Development conventions

- **No AWS changes without approval** — read safety rules in the introduction.
- **Unit tests must pass before any commit**: `PYTHONPATH=. .venv/bin/pytest tests/unit/ -v` (197 tests).
- **No behavior changes in tests** — test names, assertion values, and fixtures are the source of truth.
- **ENVIRONMENT=local** for all local development. Never set `ENVIRONMENT=production` locally.
- **X-Dev-User-Id header** replaces JWT authentication in local development.
  `curl http://localhost:8000/jobs -H "X-Dev-User-Id: user-1"`
