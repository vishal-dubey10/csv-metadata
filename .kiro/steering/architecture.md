# CSVForge Architecture

## Project Name

**CSVForge** — A CSV processing pipeline with async job tracking.

Users upload CSV files through a React frontend. The files land in S3. A worker
Lambda reads them, cleans them (deduplication, validation), and writes the results
back to S3. The frontend downloads the processed file via a presigned URL.

---

## Architecture Summary

```
┌─────────────────────────────────────────────────────┐
│  Browser (React + React Admin)                       │
└────────────────────┬────────────────────────────────┘
                     │ HTTP (API Gateway → Lambda / uvicorn local)
┌────────────────────▼────────────────────────────────┐
│  FastAPI (Python)                                    │
│  • Deployed as AWS Lambda via Mangum (production)    │
│  • Runs locally via uvicorn (development)            │
└───────────┬────────────────────┬────────────────────┘
            │ SQLAlchemy ORM     │ boto3 (Day 2+)
┌───────────▼──────────┐  ┌─────▼──────────────────────┐
│  PostgreSQL 16 (RDS) │  │  AWS S3                    │
│  jobs table           │  │  uploads/  processed/      │
└──────────────────────┘  └─────────────────────────────┘
                                    │ SQS message (Day 2+)
                          ┌─────────▼──────────────────┐
                          │  Worker Lambda (Python)     │
                          │  Reads CSV from S3          │
                          │  Cleans + deduplicates      │
                          │  Writes result to S3        │
                          │  Updates job via repository │
                          └────────────────────────────┘
```

### Components by day

| Component | Technology | Status |
|-----------|-----------|--------|
| API | FastAPI (Lambda via Mangum) | Day 1 ✅ |
| Database | PostgreSQL 16 (RDS) via SQLAlchemy 2.0 + psycopg 3 | Day 1 ✅ |
| Storage | AWS S3 for CSV files | Day 2+ |
| Queue | AWS SQS for job triggers | Day 2+ |
| Worker | Python Lambda — CSV cleaning | Day 2+ |
| Frontend | React + React Admin | Day 3+ |

---

## Python Package Layout

```
csv-metadata/
├── backend/                    FastAPI application package
│   ├── __init__.py
│   ├── main.py                 FastAPI app, CORS, routes
│   ├── requirements.txt        Production dependencies
│   ├── requirements-dev.txt    Test + dev dependencies
│   ├── db/
│   │   ├── __init__.py
│   │   ├── connection.py       Engine, SessionLocal, Base, get_db()
│   │   └── job_repository.py   All SQL operations for the jobs table
│   ├── models/
│   │   ├── __init__.py
│   │   └── job.py              Job ORM model, JobStatus enum
│   └── alembic/
│       ├── env.py              Alembic runtime config
│       └── versions/
│           └── 0001_initial_jobs_table.py
├── worker/                     CSV processing Lambda (placeholder, Day 2)
│   ├── __init__.py
│   └── requirements.txt
├── tests/
│   ├── conftest.py             Shared fixtures (SQLite engine, test client)
│   ├── unit/                   Fast tests — SQLite in-memory, no Docker
│   │   ├── test_health.py
│   │   ├── test_job_model.py
│   │   └── test_job_repository_unit.py
│   └── integration/            PostgreSQL-required tests (Docker)
│       ├── test_db_connection.py
│       ├── test_job_repository_integration.py
│       └── test_migration.py
├── docs/
│   ├── DECISIONS.md            AD-01 through AD-30
│   ├── PROGRESS.md
│   └── DAY1_LEARNING_GUIDE.md
├── alembic.ini
├── docker-compose.yml
├── Dockerfile
└── .env.example
```

---

## Database Layer Details

- **ORM**: SQLAlchemy 2.0 with typed mapping (`Mapped[T]`, `mapped_column()`)
- **Driver**: psycopg 3 — the `psycopg` package (not `psycopg2`)
- **Migrations**: Alembic with a linear revision chain
- **Session pattern**: one session per request via FastAPI `Depends(get_db)`
- **Schema**: one table — `jobs` — with 15 columns and 3 indexes

---

## Test Strategy

| Layer | Database | Speed | What it tests |
|-------|----------|-------|--------------|
| Unit | SQLite in-memory | Fast (~1s) | ORM model, repository logic, API endpoints |
| Integration | Real PostgreSQL (Docker) | Slower | ARRAY columns, TIMESTAMPTZ, MVCC, Alembic migration |

**Canonical unit test command:**
```bash
cd /Users/vishal/csv-metadata && PYTHONPATH=. .venv/bin/pytest tests/unit/ -v
```

Expected: **53 tests, all passing**.

**Integration test command** (requires Docker):
```bash
docker compose up -d
alembic upgrade head
PYTHONPATH=. .venv/bin/pytest tests/integration/ -v
```

---

## Environment Detection

The `_build_database_url()` function in `backend/db/connection.py` reads
`ENVIRONMENT` to choose the credentials path:

| `ENVIRONMENT` | How `DATABASE_URL` is resolved |
|--------------|-------------------------------|
| `local` (default for dev) | Read `DATABASE_URL` from `.env` file |
| `production` | Fetch username + password from AWS Secrets Manager; host/port from Lambda env vars |

This keeps secrets out of CloudFormation templates and Lambda environment variables.
See AD-16 and AD-24 in `docs/DECISIONS.md`.

---

## Key Design Decisions

All significant architectural choices are documented in `docs/DECISIONS.md` as
Architecture Decision Records (AD-01 through AD-30). Read that file before making
any change that affects the API shape, database schema, or deployment model.

Notable decisions:
- **AD-02** — Direct browser → S3 upload via presigned URL (bypasses Lambda payload limit)
- **AD-04** — PostgreSQL over DynamoDB for ACID transaction guarantees
- **AD-08** — Conditional UPDATE for worker idempotency (not SELECT FOR UPDATE)
- **AD-13** — `pending_upload` jobs hidden from user-facing list
- **AD-23** — Authorization uses Cognito `sub` (not email)

---

## Current Status

**Day 1 complete:**
- `jobs` table created and migrated
- Full repository layer (`create_job`, `get_job_by_id`, `list_jobs_for_user`,
  `claim_job_for_processing`, `complete_job`, `fail_job`, etc.)
- FastAPI app with health check endpoint
- 53 unit tests passing
- Docker Compose stack with PostgreSQL

**Day 2 preview:**
- S3 presigned URL generation (upload intent endpoint)
- SQS job dispatch after upload confirmation
- Worker Lambda skeleton (reads from SQS, processes CSV)
