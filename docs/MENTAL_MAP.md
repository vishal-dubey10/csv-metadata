# CSVForge Mental Map

**Navigation guide, concept index, and GATE 2027 CS syllabus mapping for the CSVForge learning codebase.**

Read this document first. It is the map before the territory. Read it end-to-end once, then use it as a reference index to jump into any specific file or concept using the docstrings already in place.

> **Source note for GATE 2027 syllabus:** The ten GATE 2027 CSE sections (Engineering Mathematics, Digital Logic, Computer Organization, Programming & Data Structures, Algorithms, Theory of Computation, Compiler Design, Operating Systems, Databases, Computer Networks) are stable year-to-year. The topics and sub-topics listed in Section 5 below are drawn from IIT Madras's released 2027 syllabus, confirmed via [Made Easy](https://www.madeeasy.in/blog/gate-syllabus-for-cse) and [GeeksForGeeks](https://www.geeksforgeeks.org/gate/gate-cse-syllabus/) (published 2025). Content was rephrased for compliance with licensing restrictions.

---

## Section 1: Project Overview

CSVForge is an end-to-end CSV processing pipeline. A user uploads a CSV file through a React frontend; the API creates a job record in PostgreSQL and returns a presigned S3 URL, so the browser uploads the file directly to S3 without touching the API server. Once the upload is confirmed, the API publishes a message to an SQS queue and transitions the job to `queued` status. A Lambda worker receives the SQS message, downloads the CSV from S3, deduplicates and validates it, uploads the cleaned result back to S3, and updates the job status to `completed`. The user then polls `GET /jobs/{id}` to track progress and downloads the processed CSV via another presigned URL.

The API never buffers CSV bytes — it only manages job state. PostgreSQL owns the job state (15-column `jobs` table). S3 owns the file bytes. SQS is the async trigger between the API and the worker. The worker and the API are separate Lambda functions that communicate only through the queue and the database — never by calling each other directly.

CSVForge exists as a learning project: every architectural choice is explained in comments and docstrings so that a developer learning SQLAlchemy, FastAPI, PostgreSQL, and AWS serverless architecture can understand not just *what* the code does but *why* it is written that way. The GATE 2027 CS syllabus maps onto almost every layer of this stack — see Section 5.

---

## Section 2: Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│  Browser                                                             │
│  React 19 + TypeScript + MUI + React Admin                          │
│  http://localhost:5173 (dev) or S3 static website (prod)            │
└────────┬────────────────────────────────────────────────────────────┘
         │ [1] HTTPS  Authorization: Bearer <access_token>
         │
┌────────▼────────────────────────────────────────────────────────────┐
│  API Gateway (HTTP API)                                              │
│  JWT Authoriser → validates Cognito access token against JWKS       │
│  On failure: 401. On success: injects JWT claims into requestContext │
└────────┬────────────────────────────────────────────────────────────┘
         │ [2] Lambda proxy event (payload format 2.0)
         │
┌────────▼────────────────────────────────────────────────────────────┐
│  FastAPI (csvforge-api-dev Lambda)                                   │
│  backend/main.py        — 8 endpoints, CORS middleware, Mangum       │
│  backend/auth.py        — get_current_user(), require_admin()        │
│  backend/schemas.py     — Pydantic request/response models           │
│  backend/db/job_repository.py — all SQL for the jobs table           │
│  backend/s3_client.py   — presigned URLs, HeadObject                 │
│  backend/sqs_client.py  — send_job_message(), parse_job_message()    │
└─────┬──────────────────────────┬───────────────────────────────────┘
      │ [3] SQLAlchemy + psycopg  │ [4] boto3 (S3 and SQS)
      │                           │
┌─────▼──────────────┐   ┌────────▼───────────────────────────────────┐
│  PostgreSQL 16      │   │  S3 bucket (csvforge-files-dev)            │
│  RDS, private VPC   │   │  uploads/{user_id}/{job_id}/original.csv   │
│  jobs table (1 tbl) │   │  processed/{user_id}/{job_id}/result.csv   │
│  15 columns         │   │  Private; CORS for browser PUT upload      │
│  3 indexes          │   └────────┬───────────────────────────────────┘
└─────────────────────┘            │ [5] Browser PUT bytes directly to S3
                                   │     (presigned URL, no API involved)
                                   │
┌──────────────────────────────────▼─────────────────────────────────┐
│  SQS (csvforge-jobs-dev)                                            │
│  main queue + DLQ (csvforge-jobs-dev-dlq)                           │
│  Message body: {"job_id": "...", "s3_key": "..."}                   │
│  VisibilityTimeout: 1800s. MaxReceiveCount: 5. DLQ after 5 fails.   │
└──────────────────────────────────┬─────────────────────────────────┘
                                   │ [6] event source mapping → Lambda invoke
                                   │
┌──────────────────────────────────▼─────────────────────────────────┐
│  Worker Lambda (csvforge-worker-dev)                                 │
│  worker/handler.py      — lambda_handler()                           │
│  worker/csv_processor.py — parse_and_analyse(), clean(), write_csv() │
│  Container image from ECR; 1024 MB memory, 300s timeout             │
└─────────────────────────────────────────────────────────────────────┘
```

### Arrow-by-arrow explanation

| Arrow | From → To | What travels | Why |
|-------|-----------|-------------|-----|
| [1] | Browser → API Gateway | HTTP request with `Authorization: Bearer <access_token>` | Cognito access token authenticates the caller; API Gateway validates it before FastAPI sees anything |
| [2] | API Gateway → Lambda | API Gateway event (JSON, payload format 2.0) | Mangum in `main.py` translates this event into an ASGI request that FastAPI understands |
| [3] | FastAPI → PostgreSQL | SQL (via SQLAlchemy ORM + psycopg 3 driver) | Job state (create/read/update) lives in PostgreSQL; SQLAlchemy builds the SQL from Python objects |
| [4] | FastAPI → S3 / SQS | HTTPS API calls (boto3) | S3 for presigned URL generation and HeadObject verification; SQS for publishing job messages |
| [5] | Browser → S3 | Raw CSV bytes (HTTP PUT) | Direct upload using a presigned URL; the API never buffers the file — avoids Lambda's 10 MB payload limit |
| [6] | SQS → Worker Lambda | Event (array of Records, each with `body`: JSON string) | SQS event source mapping triggers the worker Lambda; the worker processes the CSV and updates job state |

---

## Section 3: File Index

One entry per source file. Read the file's own docstring for the full deep-dive.

---

### `backend/__init__.py`
**Purpose:** Empty package marker — makes `backend/` importable as a Python package.
**Key symbols:** none
**Jump-to:** Module docstring → "ARCHITECTURE" block shows the full `backend/` sub-package tree. The "IMPORTANT CONCEPTS" block explains why `__init__.py` must exist and why it should never contain executable code.

---

### `backend/main.py`
**Purpose:** FastAPI application entry point — creates the app instance, registers CORS middleware, defines all 8 HTTP endpoints, and exposes the Mangum Lambda handler.

| Symbol | Description |
|--------|-------------|
| `app` | The FastAPI ASGI application instance |
| `handler` | `Mangum(app)` — AWS Lambda entry point; must match the CloudFormation `Handler` property exactly |
| `_get_cors_origins()` | Reads `ALLOWED_ORIGINS` env var; falls back to `localhost:5173` for local dev |
| `health_check()` | `GET /health` — liveness probe, no auth required |
| `create_upload_job()` | `POST /jobs` — create job + presigned S3 URL (REST-conventional alias) |
| `upload_intent()` | `POST /uploads/intent` — same logic, original route name; kept for backwards compatibility |
| `_create_upload_job_logic()` | Shared implementation for both create routes; builds S3 key from `{user_id}/{job_id}` |
| `upload_complete()` | `POST /uploads/{job_id}/complete` — verify S3 upload via HeadObject, transition to queued, publish to SQS |
| `list_jobs()` | `GET /jobs` — list caller's jobs, excludes `pending_upload` |
| `get_job()` | `GET /jobs/{job_id}` — get one job, 404 for missing or wrong owner |
| `download_job()` | `GET /jobs/{job_id}/download` — presigned S3 GET URL for completed job output |
| `admin_list_jobs()` | `GET /admin/jobs` — paginated admin view; sets `Content-Range` header for React Admin |
| `_job_to_response()` | Converts ORM `Job` → `JobResponse` Pydantic model (bridges `job.id` → `job_id` naming gap) |
| `_job_to_admin_response()` | Converts ORM `Job` → `AdminJobResponse` (includes internal S3 keys, user_id) |

**Jump-to:** Module docstring → "FULL REQUEST LIFECYCLE" for the complete path from browser HTTP request through CORS → route → dependency resolution → repository → ORM → PostgreSQL and back. "INTERVIEW CONCEPT — 404 vs 403" for the security reasoning behind returning 404 for wrong-owner access.

---

### `backend/auth.py`
**Purpose:** Provides `get_current_user()` FastAPI dependency that extracts authenticated user identity from API Gateway JWT claims (production) or trusted headers (local dev).

| Symbol | Description |
|--------|-------------|
| `CurrentUser` | Dataclass: `user_id` (Cognito `sub`), `is_admin` (bool), optional `email` |
| `get_current_user()` | Async FastAPI dependency; local dev reads `X-Dev-User-Id` header; production reads `scope["aws.event"]` claims |
| `require_admin()` | Wraps `get_current_user()`; raises HTTP 403 if `is_admin` is False |

**Jump-to:** Module docstring → "ARCHITECTURE — how auth flows in production" for the full API Gateway → Mangum → FastAPI → claims path diagram. `get_current_user()` docstring → "TWO PATHS" for the local dev bypass vs production JWT path. `require_admin()` docstring → "INTERVIEW CONCEPT: 403 vs 401" for when to use each status code.

---

### `backend/schemas.py`
**Purpose:** Pydantic v2 request/response models that validate incoming JSON at the API boundary and serialize Python objects to JSON for responses.

| Symbol | Description |
|--------|-------------|
| `UploadIntentRequest` | `POST /uploads/intent` body: `user_email`, `original_filename`, `file_size_bytes` |
| `UploadIntentResponse` | Response: `job_id`, `upload_url`, `s3_key` |
| `UploadCompleteResponse` | Response: `job_id`, `status` |
| `JobResponse` | Public job view (no internal S3 keys); `model_config = {"from_attributes": True}` enables ORM-to-Pydantic bridge |
| `JobListResponse` | Envelope: `{"jobs": [...], "total": N}` |
| `JobDownloadResponse` | `download_url`, `filename`, `expires_in_seconds` |
| `PostJobRequest` | Body for `POST /jobs` (alias for `UploadIntentRequest`) |
| `AdminJobResponse` | Admin-only view — adds `user_id`, `original_s3_key`, `processed_s3_key` |

**Jump-to:** Module docstring → "WHY THIS FILE EXISTS AS A SEPARATE MODULE" for five numbered reasons (validation, OpenAPI docs, serialization, type safety, separation of concerns). `JobResponse` class body → inline comment above `model_config` for `from_attributes=True` explanation and the Pydantic v1 `orm_mode` → v2 `from_attributes` rename.

---

### `backend/s3_client.py`
**Purpose:** Single place for all S3 operations — presigned URL generation, existence checks, size retrieval, file upload and download for the worker.

| Symbol | Description |
|--------|-------------|
| `MAX_UPLOAD_BYTES` | `52_428_800` (50 MiB) — enforced by presigned URL condition and complete endpoint |
| `get_s3_client()` | Creates a boto3 S3 client; supports `AWS_ENDPOINT_URL` for LocalStack |
| `generate_presigned_upload_url()` | Presigned PUT URL; embeds `ContentType: text/csv` in the signature |
| `generate_presigned_download_url()` | Presigned GET URL for the processed output file |
| `object_exists()` | HeadObject check — returns True/False; re-raises non-404 errors |
| `get_object_size()` | HeadObject → `ContentLength` bytes (authoritative size, not client-reported) |
| `upload_file()` | Worker uses this to store processed CSV output in S3 (uses boto3 multipart under the hood) |
| `download_file()` | Worker uses this to fetch raw CSV from S3 to `/tmp` |

**Jump-to:** Module docstring → "ANALOGY — presigned URL as a visitor pass" for the mental model. `generate_presigned_upload_url()` docstring → "Interview concept: HMAC-SHA256 signing" for how signatures work cryptographically. `object_exists()` docstring → "Interview concept: strong read-after-write consistency in S3" for the December 2020 S3 consistency change.

---

### `backend/sqs_client.py`
**Purpose:** Single place for encoding and decoding SQS messages — sends job messages from the API and parses them in the worker.

| Symbol | Description |
|--------|-------------|
| `get_sqs_client()` | Creates a boto3 SQS client; supports `AWS_ENDPOINT_URL` for LocalStack |
| `send_job_message()` | JSON-encodes `{job_id, s3_key}` and calls SQS `SendMessage`; returns SQS `MessageId` |
| `parse_job_message()` | `json.loads(body)` — decodes the raw SQS event body string into a dict |

**Jump-to:** Module docstring → "MESSAGE FORMAT" for the exact JSON schema and the rationale for sending identifiers not data. "IMPORTANT CONCEPTS" block for at-least-once delivery, the claim-check pattern, and the distinction between SQS `MessageId` and CSVForge `job_id`. `send_job_message()` docstring → "Interview concept: loose coupling via message queues."

---

### `backend/db/__init__.py`
**Purpose:** Empty package marker making `backend/db/` importable.
**Key symbols:** none
**Jump-to:** Module docstring → "ARCHITECTURE" block shows the three consumers of this sub-package (main.py, worker, conftest.py) and the one-way dependency graph that prevents circular imports.

---

### `backend/db/connection.py`
**Purpose:** Database connection management — resolves `DATABASE_URL`, creates the SQLAlchemy Engine and SessionLocal factory, defines Base, and provides the `get_db()` FastAPI dependency.

| Symbol | Description |
|--------|-------------|
| `_build_database_url()` | Reads env vars; local path reads `DATABASE_URL` from `.env`; production path fetches credentials from AWS Secrets Manager |
| `DATABASE_URL` | Module-level resolved URL — computed once per process/Lambda container at import time |
| `engine` | Long-lived SQLAlchemy `Engine`; `pool_size=1` for Lambda, `pool_pre_ping=True` for stale connections |
| `SessionLocal` | `sessionmaker(bind=engine, expire_on_commit=False)` — the session factory |
| `Base` | `DeclarativeBase` subclass — schema registry; all ORM models inherit from it; Alembic reads `Base.metadata` |
| `get_db()` | Generator dependency: `SessionLocal()` → `yield db` → `commit/rollback` → `close` |

**Jump-to:** Large inline comment block above `engine = create_engine(...)` for the complete pool analogy (restaurant manager, tables, connections, sessions), the stale connection / `pool_pre_ping` deep-dive, and the `pool_size=1` Lambda rationale. Inline block above `SessionLocal` for `expire_on_commit=False` explained step-by-step. `get_db()` docstring → "HOW (execution order)" numbered lifecycle diagram and the "ANALOGY" (library checkout desk). Final "Interview concept: Unit of Work" block.

---

### `backend/db/job_repository.py`
**Purpose:** All database operations for the `jobs` table — the only place in the codebase that writes SQL for jobs.

| Symbol | Description |
|--------|-------------|
| `create_job()` | INSERT + `db.flush()` + `db.refresh()` — creates a new job in `pending_upload` status |
| `get_job_by_id()` | SELECT by primary key (no ownership check — worker/admin use only) |
| `get_job_by_id_and_user()` | SELECT by id AND user_id — returns None for missing or wrong owner |
| `list_jobs_for_user()` | SELECT excluding `pending_upload`, ordered by `created_at DESC` |
| `list_all_jobs()` | Admin paginated SELECT with optional status filter (OFFSET/LIMIT) |
| `count_all_jobs()` | `SELECT COUNT(id)` — for `Content-Range` header in admin endpoint |
| `transition_to_queued()` | Conditional UPDATE `pending_upload → queued`; enforces ownership in WHERE clause |
| `claim_job_for_processing()` | Atomic conditional UPDATE `queued/processing → processing`; SQS at-least-once idempotency guard |
| `complete_job()` | Single-statement UPDATE writing all result columns; S3 write must precede this call (AD-14) |
| `fail_job()` | UPDATE `status=failed` + `failure_reason`; returns normally so SQS deletes the message (no retry loop) |

**Jump-to:** Module docstring → "SQLALCHEMY 2.0 RESULT CHAIN" for `db.execute().scalars().all()` step-by-step. `claim_job_for_processing()` docstring for the deepest dive in the codebase: covers SELECT+UPDATE race condition, why conditional UPDATE wins, MVCC, rowcount semantics, `synchronize_session=False`, `expire_all()`, and the parking spot analogy. `create_job()` docstring for `flush()` vs `refresh()` vs `commit()` distinctions. Module docstring → "IMPORTANT CONCEPTS" for the identity map explanation.

---

### `backend/models/__init__.py`
**Purpose:** Empty package marker making `backend/models/` importable.
**Key symbols:** none
**Jump-to:** Module docstring → "ARCHITECTURE" block for the full one-way dependency chain: `Base` → `Job` → `job_repository` → `main.py/worker`.

---

### `backend/models/job.py`
**Purpose:** Defines the `JobStatus` enum and the `Job` SQLAlchemy ORM class that maps to the `jobs` table.

| Symbol | Description |
|--------|-------------|
| `JobStatus` | `str + enum.Enum` with 5 members: `PENDING_UPLOAD`, `QUEUED`, `PROCESSING`, `COMPLETED`, `FAILED` |
| `Job` | ORM model with 15 columns and 3 indexes; inherits from `Base` |
| `Job.__tablename__` | `"jobs"` |
| `Job.id` | `VARCHAR(36)` UUID primary key; generated by Python `uuid.uuid4()` in `create_job()` |
| `Job.user_id` | `VARCHAR(128)` Cognito `sub` claim — authoritative, immutable user identity |
| `Job.status` | `VARCHAR(32)` state machine column (not native ENUM — see inline comment) |
| `Job.column_names` | `ARRAY(TEXT).with_variant(TEXT, "sqlite")` — PostgreSQL uses ARRAY; unit tests use TEXT |
| `Job.created_at` | `TIMESTAMP(timezone=True)`, `server_default=text("CURRENT_TIMESTAMP")` |
| `Job.updated_at` | `TIMESTAMP(timezone=True)`, `server_default + onupdate=func.now()` |
| `Job.__table_args__` | Three `Index(...)` definitions: `idx_jobs_user_id`, `idx_jobs_status`, `idx_jobs_created_at` |
| `Job.__repr__()` | Developer-friendly `<Job id='...' status='...' user_id='...'>` — never triggers a SELECT |

**Jump-to:** `JobStatus` docstring → "WHY `str` mixin" for the equality comparison gotcha and serialization benefit. Inline comment above `status` column → "WHY VARCHAR(32) instead of a PostgreSQL native ENUM" for the `ALTER TYPE` locking problem. Inline block above `column_names` → full `.with_variant()` explanation with PostgreSQL vs SQLite trade-off. Inline block above `created_at` → three-row comparison table for `server_default` vs Python `default` vs `onupdate`. Inline block above `id` → UUID vs sequential integer trade-offs.

---

### `backend/alembic/env.py`
**Purpose:** Alembic runtime configuration — connects SQLAlchemy models to the migration engine; runs offline (SQL-only) or online (live DB) migrations.

| Symbol | Description |
|--------|-------------|
| `target_metadata` | `Base.metadata` — the schema Alembic diffs against the live DB |
| `run_migrations_offline()` | Generates SQL text without a DB connection (`--sql` flag) |
| `run_migrations_online()` | Connects to PostgreSQL and executes pending migrations using `NullPool` |
| `from backend.models.job import Job` | Side-effect import — registers `Job` on `Base.metadata` so Alembic sees the `jobs` table |

**Jump-to:** Module docstring → "WHY NOT `Base.metadata.create_all()`?" for the blueprint vs paper trail analogy. `run_migrations_online()` docstring → "THE alembic_version TABLE" for how Alembic tracks applied migrations. Inline comment on `pool.NullPool` for why a CLI tool uses a different pool than a server.

---

### `backend/alembic/versions/0001_initial_jobs_table.py`
**Purpose:** First Alembic migration — creates the `jobs` table with all 15 columns, constraints, and 3 B-tree indexes.

| Symbol | Description |
|--------|-------------|
| `revision = "0001"` | This migration's unique ID |
| `down_revision = None` | No predecessor — root of the revision chain |
| `upgrade()` | `op.create_table("jobs", ...)` + `op.create_index(...)` × 3 |
| `downgrade()` | `op.drop_index(...)` × 3 + `op.drop_table("jobs")` |

**Jump-to:** Module docstring → "HOW MIGRATIONS CHAIN TOGETHER" for the `None ← 0001 ← 0002 ← ...` linked list and the `alembic_version` table. `upgrade()` docstring → "COLUMN DECISIONS" for per-column rationale (UUID, VARCHAR vs native ENUM, ARRAY, TIMESTAMPTZ). Index creation comments for B-tree explanation and why three separate indexes beat one composite.

---

### `worker/__init__.py`
**Purpose:** Empty package marker for the `worker/` package; placeholder until Day 2 worker implementation.
**Key symbols:** none
**Jump-to:** Module docstring → "ARCHITECTURE" block for the planned `handler.py` + `csv_processor.py` structure and the Lambda processing flow (SQS → claim → download → process → complete/fail). "WHY THIS IS ITS OWN PACKAGE" for the deployment boundary rationale (separate IAM role, separate zip file, no FastAPI dependency).

---

### `tests/conftest.py`
**Purpose:** Shared pytest fixtures — SQLite in-memory sessions for unit tests, PostgreSQL sessions for integration tests, and a FastAPI `TestClient`.

| Symbol | Description |
|--------|-------------|
| `os.environ.setdefault("ENVIRONMENT", "local")` | Must precede all `backend.*` imports — prevents `_build_database_url()` from calling Secrets Manager in tests |
| `sqlite_engine` | Module-level SQLite in-memory engine (`sqlite:///:memory:`, `check_same_thread=False`) |
| `SqliteSession` | `sessionmaker` factory for SQLite unit test sessions |
| `sqlite_db` | Function-scoped fixture: `create_all` → yield → `drop_all` — fresh schema per test |
| `pg_engine` | Session-scoped PostgreSQL engine; skips all integration tests if DB is unreachable |
| `pg_db` | Function-scoped PostgreSQL session with per-test transaction rollback isolation |
| `client` | `TestClient(app)` used as context manager to trigger ASGI lifespan events |

**Jump-to:** Module docstring → "MODULE-IMPORT-TIME ORDERING CONSTRAINT" for why the `os.environ` line must come before any `backend.*` import. "FIXTURE SCOPE: session VS function" for the cost model of scope choices. `pg_db` fixture docstring and the inline rollback-isolation diagram. `sqlite_engine` inline block for `check_same_thread=False` and the in-memory URL explanation.

---

### `tests/unit/test_health.py`
**Purpose:** Tests the `GET /health` endpoint for HTTP 200, correct response shape, correct content-type, and correct environment field.

| Symbol | Description |
|--------|-------------|
| `test_health_returns_200` | Smoke test: route is reachable |
| `test_health_returns_status_ok` | Response body contains `{"status": "ok"}` |
| `test_health_returns_environment` | `environment` field reflects `ENVIRONMENT` env var |
| `test_health_response_shape` | Exactly two keys — prevents accidental leak of internal data |
| `test_health_content_type_is_json` | Content-Type is `application/json` |

**Jump-to:** Module docstring → "HOW TestClient WORKS" for the in-process ASGI testing model. "WHY TEST THE HEALTH ENDPOINT" for the load balancer / ECS liveness probe context. "FIXTURE INJECTION" for how pytest discovers the `client` fixture from `conftest.py`.

---

### `tests/unit/test_job_model.py`
**Purpose:** Structural tests for the `Job` ORM model — column count, enum values, nullable constraints, indexes, and `onupdate` configuration.

| Symbol | Description |
|--------|-------------|
| `TestJobStatus` | Tests enum members, string values, and `str` mixin equality behavior |
| `TestJobModelStructure` | Tests `__tablename__`, exactly 15 columns, expected column names, nullable/non-nullable split, 3 indexes, `onupdate` presence |

**Jump-to:** Module docstring → "HOW SQLAlchemy INTROSPECTION WORKS" for `sa_inspect(Job)` and `Job.__table__.columns`. "WHAT WOULD FAIL IF THESE TESTS BREAK?" maps each test to the real-world bug it catches.

---

### `tests/unit/test_job_repository_unit.py`
**Purpose:** Repository function tests over SQLite — covers create, read, ownership, list filtering, and all state transition functions.

| Symbol | Description |
|--------|-------------|
| `_make_job()` | Test factory helper ("Object Mother" pattern) — creates a job with sensible defaults, overridable per-test |
| `TestCreateJob` | Verifies field population, nullable result fields start as None |
| `TestGetJobById` | Returns None for missing ids (no exception) |
| `TestGetJobByIdAndUser` | Returns None for wrong owner — security test for IDOR prevention |
| `TestListJobsForUser` | Excludes `pending_upload` (AD-13); only returns caller's jobs |
| `TestTransitionToQueued` | Returns False for wrong user and already-queued jobs |
| `TestClaimJobForProcessing` | Returns False for completed/failed/pending_upload; True for queued and processing |
| `test_second_claim_returns_false` | Intentionally misnamed — documents that the second claim returns **True** (idempotency via `IN ('queued', 'processing')`) |
| `TestFailJob` | Sets status=failed, stores failure_reason, prevents re-claim |

**Jump-to:** Module docstring → "NOTE on `test_second_claim_returns_false`" — this test's name documents a surprising behavior; do not rename it. `claim_job_for_processing()` in `job_repository.py` for the full idempotency explanation.

---

## Section 4: Concept Map

Every significant concept, where it lives, and a brief explanation.

| Concept | File | Symbol / Location | Brief explanation |
|---------|------|-------------------|-------------------|
| Connection pooling | `backend/db/connection.py` | Inline block above `engine = create_engine(...)` | Pre-created connections shared across requests; avoids TCP handshake + TLS + auth overhead per request |
| Engine (SQLAlchemy) | `backend/db/connection.py` | `engine` module-level variable | Long-lived object that manages the connection pool; one per process/Lambda container; never create per-request |
| Session | `backend/db/connection.py` | Inline comment above `SessionLocal` | Per-request Unit of Work; tracks ORM object changes; commits or rolls back as one atomic group |
| SessionLocal | `backend/db/connection.py` | `SessionLocal = sessionmaker(...)` | Factory class that stamps out consistently-configured Session instances; the "coffee machine" in the analogy |
| Base (DeclarativeBase) | `backend/db/connection.py` | `class Base(DeclarativeBase)` | Schema registry; all ORM models inherit from it; Alembic reads `Base.metadata` to generate migrations |
| ORM mapping | `backend/models/job.py` | Module docstring "ANALOGY" | Translates Python object operations into SQL; `session.add(job)` → `INSERT INTO jobs ...` |
| `Mapped[T]` + `mapped_column()` | `backend/models/job.py` | `id: Mapped[str] = mapped_column(...)` | SQLAlchemy 2.0 typed mapping — Python type annotation for IDE/mypy + database descriptor for SQL type and constraints |
| `JobStatus` enum + `str` mixin | `backend/models/job.py` | `class JobStatus(str, enum.Enum)` | `str` mixin makes `JobStatus.QUEUED == "queued"` True without `.value`; safe for JSON serialization |
| `server_default` vs Python `default` vs `onupdate` | `backend/models/job.py` | Inline block above `created_at` | `server_default`: DB computes at INSERT; `default`: Python computes before INSERT; `onupdate`: ORM adds to every UPDATE |
| TIMESTAMPTZ vs TIMESTAMP | `backend/models/job.py` + `0001_initial_jobs_table.py` | `created_at`, `updated_at` column definitions | TIMESTAMPTZ stores UTC internally; TIMESTAMP has no timezone awareness — risky across timezones or regions |
| UUID primary key | `backend/models/job.py` | `id` column inline block | Non-enumerable (prevents resource enumeration), globally unique, no DB round-trip for generation |
| VARCHAR vs native PostgreSQL ENUM | `backend/models/job.py` | `status` column inline block | `ALTER TYPE` on native ENUM acquires ACCESS EXCLUSIVE lock; VARCHAR avoids zero-downtime migration problem |
| `ARRAY(TEXT).with_variant()` | `backend/models/job.py` | `column_names` column inline block | SQLite has no ARRAY type; `.with_variant(TEXT, "sqlite")` uses TEXT in unit tests, native ARRAY in PostgreSQL |
| B-tree index | `backend/models/job.py` + `0001_initial_jobs_table.py` | `__table_args__` + `op.create_index()` | Balanced tree; O(log N) lookup; three separate single-column indexes for three independent access patterns |
| Alembic migrations, revision chain | `backend/alembic/versions/0001_initial_jobs_table.py` | Module docstring | Linked list of `revision`/`down_revision` IDs; Alembic walks the chain to find un-applied migrations |
| `alembic_version` table | `backend/alembic/versions/0001_initial_jobs_table.py` + `env.py` | Module docstrings | Single-row table Alembic creates to track the latest applied `revision` |
| Offline vs online migration mode | `backend/alembic/env.py` | `run_migrations_offline()` / `run_migrations_online()` | Offline: generates SQL text without a DB connection; online: connects and executes directly |
| FastAPI dependency injection | `backend/db/connection.py` + `backend/main.py` | `get_db()`, route function signatures | FastAPI calls the `get_db()` generator, injects the yielded Session, runs cleanup after handler returns |
| `Depends(get_db)` | `backend/main.py` | Route function signatures | Declares a dependency; FastAPI resolves it before the handler and injects the result |
| Pydantic `BaseModel` + `from_attributes` | `backend/schemas.py` | `JobResponse.model_config = {"from_attributes": True}` | `from_attributes=True` enables reading from ORM attribute access instead of dict access; replaces Pydantic v1 `orm_mode` |
| JWT / Cognito auth, Bearer token | `backend/auth.py` | `get_current_user()`, module docstring | API Gateway validates JWT; FastAPI reads already-validated claims from `scope["aws.event"]` |
| `sub` claim (authoritative identity) | `backend/auth.py` + `backend/models/job.py` | `CurrentUser.user_id`, `Job.user_id` | Immutable Cognito UUID assigned at account creation; used for all ownership checks; never use email |
| 404 vs 403 for ownership failures | `backend/main.py` | `get_job()` docstring "WHY 404 FOR WRONG OWNER" | 403 leaks existence; 404 for both "not found" and "wrong owner" prevents resource enumeration attacks |
| Presigned URL (S3 upload flow) | `backend/s3_client.py` | `generate_presigned_upload_url()`, module docstring | HMAC-SHA256 signed URL; grants bearer one specific S3 operation for a limited time; browser needs no AWS credentials |
| `content-length-range` condition | `backend/s3_client.py` | `generate_presigned_upload_url()` docstring | S3 policy condition embedded in URL that rejects PUTs outside size bounds; enforces 50 MiB limit server-side |
| HeadObject (S3 existence check) | `backend/s3_client.py` | `object_exists()` | Metadata-only request — 0 bytes transferred; correct API for "does this file exist?" |
| SQS at-least-once delivery | `backend/sqs_client.py` | Module docstring "IMPORTANT CONCEPTS" | Standard SQS delivers each message at least once; rare duplicates possible — worker must be idempotent |
| Visibility timeout | Architecture diagram + `backend/sqs_client.py` | SQS block | While a Lambda is processing, the message is hidden (1800s); if Lambda times out, message becomes visible again for retry |
| Dead Letter Queue (DLQ) | Architecture diagram | SQS block | After `MaxReceiveCount` (5) failed deliveries, message is moved to DLQ for human inspection |
| Claim-check pattern | `backend/sqs_client.py` | Module docstring "IMPORTANT CONCEPTS" | SQS message carries `job_id` + `s3_key` (identifiers only), not file bytes; consumer fetches payload from S3 |
| Repository pattern | `backend/db/job_repository.py` | Module docstring | All SQL for one entity in one module; routes never write raw queries; separates "what query" from "when to query" |
| Conditional UPDATE (idempotency) | `backend/db/job_repository.py` | `claim_job_for_processing()` | Atomic check + set in one SQL statement; no SELECT+UPDATE race condition; `rowcount` tells if condition matched |
| Unit of Work (SQLAlchemy Session) | `backend/db/connection.py` | `get_db()` docstring "Interview concept" | Groups related DB operations into one transaction; all succeed (commit) or all fail (rollback) together |
| Identity map (Session cache) | `backend/db/connection.py` + `job_repository.py` | `expire_on_commit=False` block + `expire_all()` | Session keeps `(Model, pk) → object` dict; avoids redundant SELECTs; must be invalidated after bulk Core UPDATEs |
| `expire_on_commit=False` | `backend/db/connection.py` | `SessionLocal` inline block | Keeps ORM attribute values readable after commit; prevents `DetachedInstanceError` during JSON serialization |
| `expire_all()` | `backend/db/job_repository.py` | `claim_job_for_processing()`, `complete_job()`, `fail_job()` | After a Core UPDATE (which skips identity-map sync), marks all loaded objects stale to force fresh SELECTs |
| `synchronize_session` | `backend/db/job_repository.py` | `transition_to_queued()` and `claim_job_for_processing()` | `"evaluate"`: SQLAlchemy re-evaluates WHERE in Python to sync in-memory objects; `False`: skip sync, caller must `expire_all()` |
| `flush()` vs `commit()` | `backend/db/job_repository.py` | `create_job()` docstring | `flush()`: writes SQL within transaction ("pencil") — visible to same session; `commit()`: makes durable ("ink") |
| `pool_size=1` (Lambda) | `backend/db/connection.py` | `engine = create_engine(...)` block | Lambda handles 1 request/container; 500 containers × pool_size=1 = 500 connections — predictable and RDS-safe |
| `pool_pre_ping` (stale connections) | `backend/db/connection.py` | `pool_pre_ping=True` inline block | Sends `SELECT 1` probe before each checkout; detects NAT-dropped connections; ~1 ms cost |
| Lambda cold start | `worker/__init__.py` + `backend/db/connection.py` | Module docstrings | First invocation of a new Lambda container; module-level code (engine creation, Secrets Manager call) runs once here |
| MVCC (PostgreSQL) | `backend/db/job_repository.py` | `claim_job_for_processing()` deep dive | Multi-Version Concurrency Control — PostgreSQL creates row versions on UPDATE; readers never block writers |
| `pytest` fixtures + scope | `tests/conftest.py` | `sqlite_db`, `pg_engine`, `pg_db`, `client` | Fixtures provide shared setup/teardown; `scope` controls lifetime (function vs session); pytest DI injects by parameter name |
| SQLite in-memory vs PostgreSQL | `tests/conftest.py` | `SQLITE_URL = "sqlite:///:memory:"` | SQLite: fast, no server, no ARRAY/TIMESTAMPTZ; PostgreSQL: all types, MVCC, ACID — integration tests use real Postgres |
| `ARRAY(TEXT).with_variant()` | `backend/models/job.py` | `column_names` attribute | See above; key bridging mechanism between unit test (SQLite) and production (PostgreSQL) environments |
| Finite state machine (JobStatus) | `backend/models/job.py` | `JobStatus` docstring "State machine" | 5 states, explicit transitions, terminal states (`completed`, `failed`) — makes lifecycle auditable and predictable |

---

## Section 5: GATE 2027 CS Syllabus Mapping

GATE 2027 CSE has ten sections. This table maps each GATE topic and sub-topic to a concrete, touchable example in CSVForge.

> Source: GATE 2027 CSE syllabus topics confirmed from [Made Easy](https://www.madeeasy.in/blog/gate-syllabus-for-cse) and [GeeksForGeeks](https://www.geeksforgeeks.org/gate/gate-cse-syllabus/). Content was rephrased for compliance with licensing restrictions.

### Databases (GATE Section 9)

| GATE Sub-topic | CSVForge Concept | File | How they connect |
|---------------|-----------------|------|-----------------|
| ER model — entities, attributes, relationships | `jobs` table as an entity with attributes (user_id, status, filename, etc.) | `backend/models/job.py` | Every column in `Job` is an attribute of the `jobs` entity; `user_id` is a foreign-key-like reference to a Cognito user entity |
| Relational model — schema, keys | UUID primary key (`id`); `user_id` as a natural foreign key | `backend/models/job.py`, `0001_initial_jobs_table.py` | `id` is the relation's key; `user_id` links to an external user store (Cognito) |
| SQL — DDL, DML, SELECT, UPDATE | `op.create_table()` in migration (DDL); `select(Job).where(...)` and `update(Job).values(...)` in repository (DML) | `backend/db/job_repository.py`, `0001_initial_jobs_table.py` | Repository functions directly illustrate GATE SQL questions; `claim_job_for_processing` is a conditional UPDATE in one statement |
| Transactions and ACID properties | `get_db()` session lifecycle; `db.commit()` / `db.rollback()` | `backend/db/connection.py` | Atomicity: all writes in one request commit or all roll back. Isolation: MVCC in PostgreSQL. Durability: `commit()` writes to disk. Consistency: NOT NULL constraints |
| Normalisation (1NF, 2NF, 3NF) | `jobs` table design — all columns depend on the full primary key (`id`) | `backend/models/job.py` | The table is in 3NF: every non-key column (`status`, `user_email`, etc.) depends only on `id`, not on any other non-key column |
| Indexing — B-tree structure | Three B-tree indexes on `user_id`, `status`, `created_at` | `backend/models/job.py`, `0001_initial_jobs_table.py` | B-tree is the default PostgreSQL index type; O(log N) lookup; comments explain sequential scan vs index scan trade-off |
| Query processing — selectivity, ORDER BY | `ORDER BY created_at DESC` in `list_jobs_for_user`; `status != 'pending_upload'` filter | `backend/db/job_repository.py` | High-selectivity filter (user_id = ?) uses `idx_jobs_user_id`; ORDER BY uses `idx_jobs_created_at` |
| Conditional UPDATE (concurrency control) | `claim_job_for_processing()` atomic UPDATE | `backend/db/job_repository.py` | One SQL statement atomically checks status AND updates it; `rowcount` tells if the condition matched — no explicit lock |
| Schema migration and versioning | Alembic revision chain, `alembic_version` table | `backend/alembic/versions/0001_initial_jobs_table.py`, `env.py` | Migration files are a versioned, reversible history of schema changes — each file is `upgrade()` + `downgrade()` |
| NULL semantics, NOT NULL constraints | Nullable result columns vs non-nullable identity columns | `backend/models/job.py`, `test_job_model.py` | Result columns (`row_count`, `column_names`) are NULL until the worker completes; identity columns (`user_id`, `status`) are always NOT NULL |

### Operating Systems (GATE Section 8)

| GATE Sub-topic | CSVForge Concept | File | How they connect |
|---------------|-----------------|------|-----------------|
| Processes and threads — lifecycle | Lambda container lifecycle (cold start, warm invocations) | `backend/db/connection.py` | A Lambda container is analogous to a process; module-level code runs once (process start); `get_db()` runs per-request (per-thread analogy) |
| Memory management — address space | Lambda `/tmp` ephemeral storage (512 MB); worker downloads CSV to `/tmp` | `backend/s3_client.py` (`download_file()`) | Worker writes to `/tmp` — Lambda's in-memory scratch space, reset between cold starts but not warm invocations |
| Process synchronisation — race conditions | `claim_job_for_processing()` conditional UPDATE to prevent two workers claiming the same job | `backend/db/job_repository.py` | Two Lambda workers receiving the same SQS message is a race condition; the conditional UPDATE is the synchronization primitive |
| Mutual exclusion — locks | SELECT FOR UPDATE (not used) vs conditional UPDATE (used) | `backend/db/job_repository.py` | Comment in `claim_job_for_processing` explains why conditional UPDATE has lower lock contention than SELECT FOR UPDATE |
| CPU scheduling — concurrency | Lambda horizontal scaling (one request per container); `pool_size=1` per container | `backend/db/connection.py` | Lambda's concurrency model: scale OUT by adding containers, not UP by adding threads — pool_size=1 reflects this |
| Deadlock prevention | `synchronize_session=False` + `expire_all()` pattern | `backend/db/job_repository.py` | Using Core UPDATE (not ORM-tracked updates) avoids holding ORM locks; `expire_all()` clears stale state |
| File system — I/O | CSV download to `/tmp`, processing, upload back to S3 | `backend/s3_client.py` | `download_file()` → local `/tmp` file I/O → `upload_file()` mirrors file system read/write operations |
| Resource management — connection lifecycle | Connection pool borrowing and returning | `backend/db/connection.py` | `db = SessionLocal()` borrows a connection; `db.close()` returns it — classic OS resource acquire/release pattern |

### Computer Networks (GATE Section 10)

| GATE Sub-topic | CSVForge Concept | File | How they connect |
|---------------|-----------------|------|-----------------|
| Application layer — HTTP/REST | 8 REST endpoints, HTTP verbs (GET, POST), status codes (200, 201, 404, 409, 422) | `backend/main.py` | Each endpoint is a real-world REST design; status codes are explained in docstrings (404 vs 403 security reasoning) |
| Client-server model | Browser → API Gateway → FastAPI → PostgreSQL/S3 | Architecture diagram (Section 2) | Multi-tier client-server with API Gateway as reverse proxy; Lambda as stateless application server |
| TLS/HTTPS — encryption in transit | All communication is HTTPS; presigned URLs include TLS | `backend/s3_client.py` | Every boto3 call uses HTTPS; browser PUT to S3 uses the HTTPS presigned URL |
| HTTP headers — CORS | `CORSMiddleware` with `allow_origins`, `allow_credentials`, `expose_headers` | `backend/main.py` | CORS prevents browsers from reading cross-origin responses; `Content-Range` header must be exposed for React Admin |
| Request/response model — authentication | `Authorization: Bearer <token>` header; JWT claims in `requestContext` | `backend/auth.py` | Bearer token is the HTTP mechanism; JWT is the token format; Cognito is the issuer |
| Message queues — asynchronous communication | SQS as the async channel between API and worker Lambda | `backend/sqs_client.py` | SQS decouples the API (producer) from the worker (consumer); standard FIFO-ish delivery with at-least-once guarantee |
| Protocol layers — ASGI vs WSGI | FastAPI is ASGI; Mangum bridges Lambda events to ASGI | `backend/main.py` | ASGI (async) vs WSGI (sync) parallels the network model's connection-oriented vs connectionless distinction |
| Flow control — visibility timeout | SQS visibility timeout (1800s) hides in-flight messages from other consumers | Architecture diagram (Section 2) | Analogous to network flow control: a message in flight is temporarily "reserved" for one receiver |

### Programming and Data Structures (GATE Section 4)

| GATE Sub-topic | CSVForge Concept | File | How they connect |
|---------------|-----------------|------|-----------------|
| Queue (FIFO) | SQS Standard Queue — messages enqueued by API, dequeued by worker | `backend/sqs_client.py` | `send_job_message()` is enqueue; Lambda event source mapping is dequeue; FIFO ordering approximate (Standard queue) |
| Hash map / dictionary | SQLAlchemy identity map: `(Model, pk) → object` dict | `backend/db/connection.py` + `job_repository.py` | The Session's identity map is literally a Python dict keyed by (class, primary_key); `expire_all()` clears it |
| B-tree / balanced search tree | PostgreSQL B-tree indexes on `user_id`, `status`, `created_at` | `backend/models/job.py` | O(log N) lookup; self-balancing; leaf nodes store column value + row pointer — exact GATE exam content |
| Linked list | Alembic revision chain (`revision` → `down_revision`) | `backend/alembic/versions/0001_initial_jobs_table.py` | Migration history is a singly-linked list of revision IDs; Alembic walks it from the current applied revision to `head` |
| Sorting — ORDER BY | `ORDER BY created_at DESC` in all list queries | `backend/db/job_repository.py` | PostgreSQL uses the `idx_jobs_created_at` B-tree index for this sort; without the index, it would sort the full table |
| String operations | UUID v4 as `VARCHAR(36)`; S3 key construction with f-strings | `backend/main.py` | `s3_key = f"uploads/{user_id}/{job.id}/original.csv"` — string concatenation; `.uuid4()` generates a 36-character hyphenated UUID |

### Algorithms (GATE Section 5)

| GATE Sub-topic | CSVForge Concept | File | How they connect |
|---------------|-----------------|------|-----------------|
| Searching — binary search | B-tree index scan (logarithmic in index size) | `backend/models/job.py` | `WHERE user_id = $1` uses the B-tree index: O(log N) lookups vs O(N) full scan without the index |
| Sorting | `ORDER BY created_at DESC` | `backend/db/job_repository.py` | Index-ordered scan vs sort step — when index exists, PostgreSQL avoids an explicit sort operation |
| Hashing | UUID generation (`uuid.uuid4()`) distributes keys uniformly | `backend/db/job_repository.py` | UUID v4 is randomly distributed — no clustering, no enumeration; UUID v7 would give time-ordered keys |
| Greedy / conditional decision | `claim_job_for_processing()` — claim the job if and only if it's in a claimable state | `backend/db/job_repository.py` | The conditional UPDATE is a greedy, single-pass decision: succeed (rowcount=1) or fail (rowcount=0), no retry needed from the DB layer |
| Asymptotic complexity | O(log N) index scan vs O(N) sequential scan; `pool_size=1` makes connection-count O(concurrency) | `backend/models/job.py`, `backend/db/connection.py` | Complexity analysis of index structures is a direct GATE topic; the connection count analysis is O(containers × pool_size) |

### Theory of Computation (GATE Section 6)

| GATE Sub-topic | CSVForge Concept | File | How they connect |
|---------------|-----------------|------|-----------------|
| Finite automata — states and transitions | `JobStatus` enum + state machine transitions | `backend/models/job.py` | `JobStatus` defines the state alphabet; `transition_to_queued`, `claim_job_for_processing`, `complete_job`, `fail_job` define the transition function |
| Deterministic vs non-deterministic | Conditional UPDATE is deterministic: `rowcount` tells you exactly one outcome | `backend/db/job_repository.py` | Unlike NFA, the conditional UPDATE has a single defined outcome per input (job state) — deterministic state transition |
| Regular expressions | UUID format `[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}` | `backend/models/job.py` | UUID v4 format follows a regular language; its structure can be described by a regular expression |
| Accepting/rejecting states — terminal states | `completed` and `failed` are terminal states of the job FSM | `backend/models/job.py` | Terminal states accept no further transitions — the worker checks for these before `claim_job_for_processing()` |
| Context-free grammars | JSON message body `{"job_id": "...", "s3_key": "..."}` | `backend/sqs_client.py` | JSON's nested structure can be described by a context-free grammar; Python's `json.loads()` is a parser for that grammar |

### Software Engineering (GATE Section — part of CS core)

| GATE Sub-topic | CSVForge Concept | File | How they connect |
|---------------|-----------------|------|-----------------|
| Layered architecture | Browser → API Gateway → FastAPI → Repository → ORM → PostgreSQL | Architecture diagram | Classic N-tier architecture: presentation, API, business logic, data access, persistence layers are distinct |
| Repository pattern | `job_repository.py` as the data access layer | `backend/db/job_repository.py` | Repository pattern: business logic (routes) never writes SQL; routes call repository functions; data access is swappable |
| Separation of concerns | `schemas.py` (validation), `auth.py` (identity), `s3_client.py` (storage), `sqs_client.py` (messaging) each own one concern | Each respective file | Every module has a single, clear responsibility — classic SRP (Single Responsibility Principle) |
| Unit testing vs integration testing | Unit tests (SQLite, fast, no infra) vs integration tests (PostgreSQL Docker, slow, real types) | `tests/conftest.py`, `tests/unit/` | Unit tests exercise logic; integration tests exercise real infrastructure; two-speed test suite is a GATE software engineering concept |
| Test fixtures — setup/teardown | `sqlite_db` fixture: `create_all` → yield → `drop_all` | `tests/conftest.py` | Generator fixtures implement the xUnit setup/teardown pattern; `scope` controls fixture lifetime |
| Schema migration — versioning | Alembic revision chain as a linear migration history | `backend/alembic/versions/0001_initial_jobs_table.py` | Database schema versioning is a software configuration management concept; each migration is an auditable, reversible change |
| CI/CD — deployment pipeline | `ENVIRONMENT` env var controls local vs production credential path | `backend/db/connection.py` | Same code artifact runs in dev and prod; env vars configure behavior — the twelve-factor app approach |
| API contract — versioning and backwards compatibility | `POST /jobs` alias for `POST /uploads/intent`; both kept for compatibility | `backend/main.py` | API versioning: never remove a route that existing clients depend on; add aliases and deprecate gradually |

### Cloud / Distributed Systems

| GATE Sub-topic | CSVForge Concept | File | How they connect |
|---------------|-----------------|------|-----------------|
| Serverless / FaaS | FastAPI deployed as Lambda via Mangum; worker as a separate Lambda | `backend/main.py`, `worker/__init__.py` | Lambda is Function-as-a-Service: no server to manage; scale to zero when idle; horizontal scaling by spinning up containers |
| Eventual consistency | SQS Standard queue: at-least-once, approximate ordering | `backend/sqs_client.py` | Standard SQS does not guarantee exactly-once or strict FIFO; the system is designed for this with idempotent workers |
| Idempotency | `claim_job_for_processing()` conditional UPDATE; SQS duplicate message handling | `backend/db/job_repository.py` | An idempotent operation produces the same result when applied multiple times; the conditional UPDATE is the implementation |
| Fault tolerance — retry logic | SQS MaxReceiveCount=5; DLQ after 5 failed deliveries | Architecture diagram | If a Lambda crashes, SQS re-delivers the message up to 5 times; the DLQ captures permanently unprocessable messages |
| CAP theorem | PostgreSQL (CP): strong consistency + partition tolerance; SQS: AP-leaning (available, eventually consistent) | Architecture diagram | The system uses PostgreSQL for job state (requires consistency) and SQS for async work (tolerates eventual consistency) |
| Distributed tracing | SQS `MessageId` vs CSVForge `job_id` | `backend/sqs_client.py` | `MessageId` is the SQS-internal trace identifier; `job_id` is the application-level correlation ID used across DB, S3, and logs |
| Presigned URL — delegated access | S3 presigned PUT/GET URLs | `backend/s3_client.py` | Security concept: delegate a specific operation to an untrusted client for a limited time; the client never holds long-lived credentials |
| Least-privilege IAM | Worker Lambda role has only `s3:GetObject`/`s3:PutObject` and `sqs:ReceiveMessage` | `backend/s3_client.py` (boto3 credential chain) | Least privilege: each Lambda execution role has exactly the permissions it needs, nothing more |

---

## Section 6: Learning Path

Recommended reading order for a new teammate. Read each file's docstring in full before moving to the next.

| Step | File | Why this order |
|------|------|---------------|
| 1 | `docs/DECISIONS.md` | Architecture Decision Records (AD-01 through AD-30) explain every major choice before you read any code; you'll encounter references like "AD-13" throughout — understand them first |
| 2 | `backend/__init__.py` | Simplest file; confirms Python package mechanics before anything complex |
| 3 | `backend/models/job.py` | Read the data model before the code that uses it; understand the state machine (5 statuses), UUID key, nullable result columns, and ARRAY type |
| 4 | `backend/alembic/versions/0001_initial_jobs_table.py` | See the same table defined as DDL; understand `server_default`, B-tree indexes, VARCHAR vs ENUM, and TIMESTAMPTZ |
| 5 | `backend/alembic/env.py` | Understand how Alembic connects the Python model to the database; revision chain and `alembic_version` table |
| 6 | `backend/db/connection.py` | The engine, pool, session, and `get_db()` are referenced everywhere; understand the restaurant analogy, `pool_size=1`, `expire_on_commit=False`, and the request lifecycle before reading routes |
| 7 | `backend/db/job_repository.py` | All SQL in one place; read `create_job` → `get_job_by_id` → `list_jobs_for_user` → `claim_job_for_processing` (the deepest concept in the codebase) |
| 8 | `backend/schemas.py` | Understand Pydantic validation, `from_attributes`, and the API request/response shapes before reading the routes |
| 9 | `backend/auth.py` | Understand JWT, `sub` claim, local dev bypass, and the `get_current_user` dependency before reading the routes that use it |
| 10 | `backend/s3_client.py` | Presigned URL mechanics, HeadObject, and strong S3 consistency — concepts used directly in the upload flow routes |
| 11 | `backend/sqs_client.py` | Claim-check pattern, at-least-once delivery, and `send_job_message` — used in the upload complete route |
| 12 | `backend/main.py` | Read all 8 endpoints now that you understand every dependency; pay special attention to the full request lifecycle diagram and the 404-vs-403 security comment |
| 13 | `backend/db/__init__.py` | Package marker; the "ARCHITECTURE" block shows the dependency graph from a different angle |
| 14 | `backend/models/__init__.py` | Package marker; the "ARCHITECTURE" block shows how models sit at the base of the dependency graph |
| 15 | `worker/__init__.py` | Placeholder; understand the deployment boundary (separate Lambda, separate requirements, separate IAM role) |
| 16 | `tests/conftest.py` | Read the import ordering constraint, fixture scopes, and per-test rollback isolation before reading any test |
| 17 | `tests/unit/test_job_model.py` | Start with structural/introspection tests — easiest to understand; learn `sa_inspect` and `Job.__table__.columns` |
| 18 | `tests/unit/test_health.py` | Understand the TestClient pattern and ASGI in-process testing |
| 19 | `tests/unit/test_job_repository_unit.py` | Hardest test file; pay special attention to `test_second_claim_returns_false` — its intentionally misleading name documents a real idempotency subtlety |

---

## Appendix: Quick Command Reference

```bash
# Run all unit tests (must pass 53 tests):
cd /Users/vishal/csv-metadata && PYTHONPATH=. .venv/bin/pytest tests/unit/ -v

# Run integration tests (requires Docker PostgreSQL):
docker compose up -d
alembic upgrade head
PYTHONPATH=. .venv/bin/pytest tests/integration/ -v

# Run a specific test file:
PYTHONPATH=. .venv/bin/pytest tests/unit/test_job_repository_unit.py -v

# Check migration status:
alembic current
alembic history

# Generate a new migration from model changes:
alembic revision --autogenerate -m "add column foo"

# Start the local API server:
uvicorn backend.main:app --reload
```

---

*Last updated: MENTAL_MAP.md fully rewritten after Python annotation pass. All 18 source files read and indexed. GATE 2027 syllabus section sourced from IIT Madras released syllabus via Made Easy and GeeksForGeeks (2025 publications). Content was rephrased for compliance with licensing restrictions.*
