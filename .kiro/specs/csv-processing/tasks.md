# Implementation Plan

## Overview

CSVForge is a 7-day deep-learning implementation plan for a full-stack CSV processing application. The goal is not maximum velocity — it is durable, transferable understanding. Every CORE task follows the Explain → Predict → Implement → Break → Debug → Explain-back cycle.

**Audit revision:** October 6, 2026. All architecture issues identified in the audit have been resolved. See `docs/DECISIONS.md` for the canonical decision record and `docs/VERSIONS.md` for the verified version matrix.

## Tasks

### CORE vs STRETCH

**CORE** — finish only these and you have a complete, demonstrable, interview-defensible MVP.

**STRETCH** — production hardening, CI/CD automation, and additional observability. Do these if time permits after all CORE tasks are done.

---

## Verified Stack

Python 3.14 · FastAPI 0.141.1 · Pydantic 2.13.4 · SQLAlchemy 2.0.54 · Alembic 1.20.1 · psycopg 3.3.3 (`psycopg[binary]`) · boto3 1.43.63 · Mangum 0.22.0 · pytest 9.1.1 · moto 5.2.4  
React 19.3.0 · TypeScript 6.0.0 · Vite 8.1.0 · React Router 8.2.0 · MUI 9.0.0 · React Admin 5.8.0 · Vitest 3.1.0 · Node.js 22.12.0+  
Lambda python3.14 · API Gateway HTTP API · RDS PostgreSQL 16 · AWS CLI v2

---

## Task Dependency Graph

```
Day 1 ──┬── T-01 (repo + Docker Compose + health)
        ├── T-02 (DB connection + Secrets Manager)
        ├── T-03 (Job model + Alembic)
        └── T-04 (Job repository)

Day 2 ──┬── T-05 (CSV parser — depends T-01)
        ├── T-06 (CSV cleaner — depends T-05)
        ├── T-07 (S3 client — depends T-01)
        └── T-08 (SQS client — depends T-01)

Day 3 ──┬── T-09 (Worker handler — depends T-04, T-06, T-07, T-08)
        ├── T-10 (Worker Dockerfile + RIE test — depends T-09)
        └── T-11 (Frontend scaffold — depends nothing)

Day 4 ──┬── T-12 (Pydantic schemas + logging — depends T-03)
        ├── T-13 (JWT dependency + auth bypass — depends T-12)
        ├── T-14 (Upload endpoints — depends T-04, T-07, T-08, T-13)
        └── T-15 (Job status + download endpoints — depends T-14)

Day 5 ──┬── T-16 (Admin endpoints — depends T-13, T-15)
        ├── T-17 (network.yaml — depends T-01 for infra dir)
        ├── T-18 (data.yaml — depends T-17)
        └── T-19 (queues.yaml — depends T-17)

Day 6 ──┬── T-20 (auth.yaml — depends T-17)
        ├── T-21 (api.yaml — depends T-18, T-19, T-20)
        ├── T-22 (worker.yaml — depends T-18, T-19, T-10)
        ├── T-23 (deploy.sh — depends T-17 through T-22)
        └── T-24 (React auth + login form — depends T-11, T-20)

Day 7 ──┬── T-25 (Upload UI + polling — depends T-14, T-24)
        ├── T-26 (Download UI — depends T-15, T-25)
        ├── T-27 (React Admin — depends T-16, T-24)
        ├── T-28 (Backend integration tests — depends T-09, T-15)
        └── T-29 (End-to-end smoke test — depends all)

STRETCH ─┬── T-30 (Failure + DLQ integration tests)
         ├── T-31 (Frontend Vitest suite)
         ├── T-32 (buildspec.yml CodeBuild)
         ├── T-33 (pipeline.yaml CodePipeline)
         └── T-34 (CloudWatch alarms + DLQ verification)
```

---

## Kiro Workflow — Every CORE Task

1. Read the task (especially "Why this task exists")
2. Kiro explains the key concepts
3. Kiro asks prediction questions — answer them before seeing the code
4. Kiro proposes the implementation
5. Review the proposal
6. Implement
7. Run tests
8. Do the failure exercise (intentionally break something)
9. Debug it
10. Explain back the implementation without looking at the code
11. Review the diff
12. Commit
13. Update `docs/PROGRESS.md`

---

# DAY 1 — Python + FastAPI + PostgreSQL + SQLAlchemy

**Learning goal:** By end of Day 1 you can explain, without notes:  
Browser → HTTP → FastAPI → dependency injection → SQLAlchemy → PostgreSQL transaction → response

---

### T-01 — Repository scaffold, Docker Compose, health endpoint
**Day:** 1 | **CORE** | **Estimated time:** 2–3 hours  
**Dependencies:** none

#### Why this task exists
Every subsequent task needs a consistent project structure, a running FastAPI server, and a running PostgreSQL database. This task creates that foundation before touching any AWS service.

#### What you must understand BEFORE coding
- What is a Python virtual environment and why not use the system Python?
- What is the difference between `requirements.txt` (pin file) and `pyproject.toml` (project metadata)?
- What does `uvicorn --reload` do and why is it only for development?
- What is Docker Compose and how does it differ from a plain `docker run`?
- What does ASGI mean and how does FastAPI use it?

#### Predict before implementation
1. If you start a second uvicorn process on port 8000, what happens?
2. What would `GET /health` return if `ENVIRONMENT` is not set?
3. Why is `.env` in `.gitignore` and not checked in?
4. What is the difference between `docker compose up` and `docker compose up -d`?

#### Implementation scope
- Top-level directories: `backend/`, `worker/`, `frontend/`, `infrastructure/`, `tests/unit/`, `tests/integration/`
- `backend/requirements.txt`: exact pins — `fastapi==0.141.1`, `uvicorn[standard]==0.51.0`, `pydantic==2.13.4`, `sqlalchemy==2.0.54`, `alembic==1.20.1`, `psycopg[binary]==3.3.3`, `boto3==1.43.63`, `mangum==0.22.0`, `python-dotenv==1.1.0`
- `backend/requirements-dev.txt`: `pytest==9.1.1`, `moto[s3,sqs]==5.2.4`, `httpx` (for FastAPI TestClient)
- `worker/requirements.txt`: `boto3==1.43.63`, `sqlalchemy==2.0.54`, `psycopg[binary]==3.3.3`, `python-dotenv==1.1.0`
- `.env.example`: document every env var — `DATABASE_URL`, `ENVIRONMENT=local`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`, `S3_BUCKET_NAME`, `SQS_QUEUE_URL`, `DB_SECRET_ARN`, `DB_HOST`, `DB_PORT=5432`, `DB_NAME=csvforge`
- `.gitignore`: `.env`, `__pycache__/`, `*.pyc`, `.venv/`, `dist/`, `node_modules/`
- `docker-compose.yml`: `api` service (FastAPI, port 8000, volume mount for hot-reload, `ENVIRONMENT=local`) and `postgres` service (postgres:16, port 5432, named volume)
- `backend/main.py`: `FastAPI()` app, `GET /health` returns `{"status": "ok", "environment": os.getenv("ENVIRONMENT", "production")}`
- Add `CORSMiddleware` allowing `http://localhost:5173` (Vite dev server) with `expose_headers=["Content-Range"]`

#### Tests
- `GET /health` → HTTP 200, body `{"status":"ok","environment":"local"}`
- `.env` is absent from `git status`
- `docker compose ps` shows both services running

#### Failure exercise
Stop the `postgres` container while the API is running. What error do you see? What happens to in-flight requests?

#### Explain-back
- What is the role of `docker-compose.yml` versus `Dockerfile`?
- Why is `uvicorn[standard]` used instead of plain `uvicorn`?
- What would change if you removed `CORSMiddleware`?

#### Interview defense
1. What is ASGI and how does it differ from WSGI?
2. Why is dependency pinning important for reproducible builds?
3. What is the risk of storing `.env` in version control?

#### Verification
```bash
curl http://localhost:8000/health
# → {"status":"ok","environment":"local"}
docker compose ps
git status  # .env must not appear
python -c "import fastapi, sqlalchemy, psycopg; print('ok')"
```

#### Commit checkpoint
`feat: project scaffold, Docker Compose, FastAPI health endpoint`

---

### T-02 — Database connection module (SQLAlchemy engine + Secrets Manager path)
**Day:** 1 | **CORE** | **Estimated time:** 2 hours  
**Dependencies:** T-01

#### Why this task exists
Every API endpoint and the worker needs a database connection. The connection logic differs between local dev (plain `DATABASE_URL`) and production (Secrets Manager ARN + env vars). This module is the single place that resolves both cases.

#### What you must understand BEFORE coding
- What is a SQLAlchemy engine versus a session versus a connection?
- What is connection pooling? Why does `pool_size=1` make sense for Lambda?
- What does `pool_pre_ping=True` do and why is it important for Lambda warm invocations?
- What is the `postgresql+psycopg://` dialect string and how does it differ from `postgresql+psycopg2://`?
- Why does the Secrets Manager secret contain only username/password and not the host? (AD-24)

#### Predict before implementation
1. What happens if you forget `pool_pre_ping=True` and reuse a Lambda that has been idle for 30 minutes?
2. What is the difference between `expire_on_commit=False` and `expire_on_commit=True` in a Lambda context?
3. What does `get_db()` as a FastAPI dependency yield vs return?

#### Implementation scope
- `backend/db/__init__.py`
- `backend/db/connection.py`:
  - Read `ENVIRONMENT` from env
  - **Local:** use `DATABASE_URL` from env directly
  - **Production:** read `DB_SECRET_ARN`, `DB_HOST`, `DB_PORT`, `DB_NAME`; call `boto3.client("secretsmanager").get_secret_value(SecretId=DB_SECRET_ARN)` → parse JSON → assemble `postgresql+psycopg://{user}:{password}@{host}:{port}/{dbname}`
  - `engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=1, max_overflow=0)`
  - `SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)`
  - `Base = declarative_base()`
  - `get_db()`: FastAPI dependency that yields a `SessionLocal()`, commits on success, rolls back on exception, closes always

#### Tests
- Unit: `get_db()` yields a session (mock engine with `unittest.mock.patch`)
- Integration: engine connects to Docker PostgreSQL; `session.execute(text("SELECT 1"))` returns 1

#### Failure exercise
Deliberately enter a wrong `DATABASE_URL`. What exception is raised? Where in the stack does it appear?

#### Explain-back
- Draw the call chain: FastAPI request → `get_db()` → SQLAlchemy session → psycopg3 → PostgreSQL TCP socket
- Why do you NOT want `pool_size=10` in Lambda?

#### Interview defense
1. What is connection pool exhaustion? When does it happen with Lambda?
2. What is the difference between a SQLAlchemy engine and a session?
3. Why is `expire_on_commit=False` important in Lambda?

#### Verification
```bash
DATABASE_URL=postgresql+psycopg://csvforge:password@localhost:5432/csvforge python -c \
  "from backend.db.connection import SessionLocal; from sqlalchemy import text; \
   db = SessionLocal(); print(db.execute(text('SELECT 1')).fetchone())"
```

#### Commit checkpoint
`feat: SQLAlchemy connection module with local/production credential paths`

---

### T-03 — Job model + Alembic initial migration
**Day:** 1 | **CORE** | **Estimated time:** 2 hours  
**Dependencies:** T-02

#### Why this task exists
The `jobs` table is the central state machine of CSVForge. Every upload, every worker run, every status poll reads or writes this table. The Alembic migration makes this reproducible and reversible.

#### What you must understand BEFORE coding
- What is the difference between a SQLAlchemy ORM model and a raw SQL table?
- Why use `gen_random_uuid()` (server-side) instead of Python's `uuid.uuid4()` (application-side)?
- Why `TIMESTAMPTZ` and not `TIMESTAMP`? What happens if you use `TIMESTAMP` in a multi-region system?
- What is an Alembic migration chain (up_revision, down_revision)?
- What does `onupdate=func.now()` do — and why is `server_default=text("NOW()")` not sufficient for UPDATE operations?

#### Predict before implementation
1. What happens if two Python processes try to insert a row with the same UUID simultaneously?
2. If you have `server_default=text("NOW()")` on `updated_at` but no `onupdate`, what value does `updated_at` have after a row is updated?
3. Why do you create an index on `user_id` and `status` separately rather than a composite index?

#### Implementation scope
- `backend/models/__init__.py`
- `backend/models/job.py`:
  - `JobStatus` enum: `pending_upload`, `queued`, `processing`, `completed`, `failed`
  - `Job` SQLAlchemy model with all 15 columns from design §6.1
  - `updated_at`: `server_default=text("NOW()")`, **`onupdate=func.now()`** — required so UPDATE operations change `updated_at`
  - Indexes: `idx_jobs_user_id`, `idx_jobs_status`, `idx_jobs_created_at`
- Initialize Alembic: `alembic init backend/alembic/`
- Configure `backend/alembic/env.py`: import `Base` and `DATABASE_URL` logic from `backend.db.connection`
- Write initial migration: create `jobs` table with all columns, constraints, indexes

#### Tests
- Unit: `Job` model has exactly 15 column attributes
- Unit: `JobStatus` enum has exactly 5 members
- Integration: `alembic upgrade head` creates table; all 15 columns and 3 indexes confirmed via `information_schema`
- Integration: insert a job, wait 1ms, update status — verify `updated_at` > `created_at`

#### Failure exercise
Delete the `updated_at` column from the migration and run it. Then try to update a row. What error do you get?

#### Explain-back
- What is the state machine encoded in `JobStatus`? Draw it.
- Why does the migration exist as a versioned file rather than `Base.metadata.create_all()`?

#### Interview defense
1. What is the difference between `TIMESTAMPTZ` and `TIMESTAMP WITHOUT TIME ZONE`?
2. What is an Alembic migration chain and how do you roll back?
3. Why does PostgreSQL use `gen_random_uuid()` for primary keys instead of auto-increment integers in this design?

#### Verification
```bash
alembic upgrade head
# Then in psql:
\d jobs
SELECT column_name FROM information_schema.columns WHERE table_name='jobs';
SELECT indexname FROM pg_indexes WHERE tablename='jobs';
```

#### Commit checkpoint
`feat: Job model, JobStatus enum, Alembic initial migration`

---

### T-04 — Job repository (CRUD + conditional UPDATE)
**Day:** 1 | **CORE** | **Estimated time:** 2–3 hours  
**Dependencies:** T-03

#### Why this task exists
The repository pattern puts all database operations in one tested module. The key insight here is `claim_job_for_processing` — a conditional UPDATE that atomically assigns ownership to one worker without a lock. This is the core of idempotency.

#### What you must understand BEFORE coding
- Why is a conditional UPDATE (`WHERE status IN (...)`) safer than `SELECT ... FOR UPDATE` for this pattern?
- Why does `get_job_by_id_and_user` return `None` (not raise) when ownership fails?
- Why does `list_jobs_for_user` exclude `pending_upload`?
- What is the difference between SQLAlchemy 2.0's `select(Model).where(...)` and the legacy `.query()` API?
- What does `rows_affected` (rowcount) mean on an UPDATE and how do you get it from SQLAlchemy?

#### Predict before implementation
1. If two workers call `claim_job_for_processing` on the same `job_id` at the same moment, what happens at the PostgreSQL level?
2. What would happen if `list_jobs_for_user` returned `pending_upload` rows?
3. Why is `expire_on_commit=False` relevant to the `create_job` function?

#### Implementation scope
- `backend/db/job_repository.py` with these functions (all accept `db: Session` as first argument):
  - `create_job(db, user_id, user_email, original_filename, original_s3_key) -> Job`
  - `get_job_by_id(db, job_id) -> Job | None`
  - `get_job_by_id_and_user(db, job_id, user_id) -> Job | None` — returns `None` if not found or wrong owner
  - `list_jobs_for_user(db, user_id) -> list[Job]` — excludes `pending_upload`, ordered `created_at DESC`
  - `list_all_jobs(db, skip, limit, status_filter=None) -> list[Job]` — for admin pagination
  - `count_all_jobs(db, status_filter=None) -> int` — for `Content-Range` header
  - `transition_to_queued(db, job_id, user_id) -> bool` — `UPDATE WHERE status='pending_upload' AND user_id=?`; returns `True` if 1 row updated
  - `claim_job_for_processing(db, job_id) -> bool` — `UPDATE WHERE status IN ('queued','processing')`; returns `True` if 1 row updated
  - `complete_job(db, job_id, processed_s3_key, file_size_bytes, row_count, column_count, column_names, duplicate_rows_removed) -> None`
  - `fail_job(db, job_id, failure_reason) -> None`
  - All writes use SQLAlchemy ORM operations — no `text()` with string interpolation

#### Tests
- `claim_job_for_processing` on a `completed` job returns `False`
- `claim_job_for_processing` called twice in sequence: second call returns `False`
- `list_jobs_for_user` with a `pending_upload` row present: that row is excluded
- `get_job_by_id_and_user` with wrong `user_id` returns `None` (does not raise)
- `fail_job` then `claim_job_for_processing` on same job: `False` (terminal state)
- After `complete_job`: verify `updated_at` > `created_at`

#### Failure exercise
Remove the `AND user_id=?` from `transition_to_queued`. Write a test that shows user B can now queue user A's job. Understand why this is a security flaw.

#### Explain-back
- Why does `claim_job_for_processing` check `status IN ('queued', 'processing')` and not just `status = 'queued'`?
- What is the race condition that the conditional UPDATE prevents?

#### Interview defense
1. What is the repository pattern and why does it matter for testability?
2. How does PostgreSQL guarantee that two concurrent conditional UPDATEs on the same row don't both succeed?
3. What is `rowcount` on a SQLAlchemy UPDATE result?

#### Verification
```bash
pytest tests/unit/test_job_repository.py -v
pytest tests/integration/test_job_repository.py -v
```

#### Commit checkpoint
`feat: Job repository with conditional UPDATE idempotency`

---

# DAY 2 — CSV Processing + S3 + SQS

**Learning goal:** By end of Day 2 you can explain exactly what happens to a CSV from raw bytes → parsed Python → cleaned rows → output bytes, and you understand why files never pass through the API.

---

### T-05 — CSV parser and analyser
**Day:** 2 | **CORE** | **Estimated time:** 2–3 hours  
**Dependencies:** T-01

#### Why this task exists
The parser is the domain core of CSVForge. Every validation decision — what is valid, what is permanently invalid, what causes a retry — is defined here. Getting this wrong causes either silent data loss or infinite retries of broken files.

#### What you must understand BEFORE coding
- How does Python's `csv.DictReader` handle duplicate column names? (Answer: the last value wins — prior values are silently dropped. This is data loss.)
- What is the difference between `utf-8` and `utf-8-sig` encoding? What is a BOM?
- Why does the worker NOT raise an exception for a permanent failure? (It would trigger SQS retries.)
- What is `csv.Error` and when is it raised?

#### Predict before implementation
1. What does `csv.DictReader` return for a file that has only a header row and no data rows?
2. If a CSV row has more fields than the header, what does `csv.DictReader` do by default?
3. What encoding does `utf-8-sig` transparently strip?

#### Implementation scope
- `worker/exceptions.py`: `InvalidCSVError(Exception)` with `.message` attribute
- `worker/csv_processor.py`: `AnalysisResult` dataclass with `column_names: list[str]`, `column_count: int`, `row_count: int`, `rows: list[dict]`
- `parse_and_analyse(file_path: str) -> AnalysisResult`:
  - Open with `encoding='utf-8-sig'`
  - If file is 0 bytes: raise `InvalidCSVError("File is empty")`
  - Use `csv.reader` first to read the header row — check for:
    - Duplicate column names → `InvalidCSVError("Duplicate column name: 'X' appears N times")`
    - Empty column names → `InvalidCSVError("Column has an empty name at position N")`
  - Then use `csv.DictReader` for remaining rows
  - Check each row's field count against header count:
    - Row with fewer fields OR more fields (check `None` key from DictReader) → `InvalidCSVError("Row N has inconsistent field count")`
  - Catch `UnicodeDecodeError` → `InvalidCSVError("File encoding is not valid UTF-8")`
  - Catch `csv.Error` → `InvalidCSVError("Malformed CSV: ...")`
  - Return `AnalysisResult`

**Validation contract (AD-21):**
| Input | Behaviour |
|-------|-----------|
| Empty file (0 bytes) | `InvalidCSVError("File is empty")` |
| Header-only, 0 data rows | Valid; `row_count=0` |
| Duplicate column names | `InvalidCSVError("Duplicate column name: ...")` |
| Empty header name | `InvalidCSVError("Column has an empty name at position N")` |
| Invalid UTF-8 | `InvalidCSVError("File encoding is not valid UTF-8: ...")` |
| Binary content | `InvalidCSVError("Malformed CSV: ...")` |
| Rows with wrong field count | `InvalidCSVError("Row N has inconsistent field count")` |
| UTF-8 BOM | Valid — `utf-8-sig` strips BOM transparently |
| Mixed CRLF/LF | Valid — `csv.reader` handles natively |
| Quoted newlines | Valid — `csv.reader` handles natively |

#### Tests
- Valid CSV: correct `row_count`, `column_count`, `column_names`
- Header-only CSV: `row_count=0`, `column_names` populated
- Empty file: raises `InvalidCSVError`
- Binary file: raises `InvalidCSVError`
- Duplicate headers (`name,name,age`): raises `InvalidCSVError`
- Empty header (`name,,age`): raises `InvalidCSVError`
- Row with extra fields: raises `InvalidCSVError`
- BOM: first column name does NOT start with `\ufeff`
- Mixed CRLF/LF: parsed correctly

#### Failure exercise
Change the duplicate-header detection to allow duplicates. Write a test with `name,name,age / Alice,Bob,25` and show that Alice's name is silently dropped. Understand why this is data loss.

#### Explain-back
- Why does the parser use `utf-8-sig` instead of `utf-8`?
- Why does a permanent validation failure NOT raise an exception out of the worker?
- What is the difference between `InvalidCSVError` and a transient `S3Error`?

#### Interview defense
1. What is at-least-once delivery and why does it require you to distinguish permanent from transient failures?
2. How does `csv.DictReader` handle a CSV with duplicate headers? Why is this a problem for a data cleaning tool?
3. What is a BOM and where does it come from?

#### Verification
```bash
pytest tests/unit/test_csv_parser.py -v
```

#### Commit checkpoint
`feat: CSV parser with strict validation contract (AD-21)`

---

### T-06 — CSV cleaner (trim + deduplicate) and writer
**Day:** 2 | **CORE** | **Estimated time:** 1.5 hours  
**Dependencies:** T-05

#### Why this task exists
The cleaning pipeline is the core business logic of CSVForge. Trim MUST run before deduplicate — rows that become duplicates only after trimming should be treated as duplicates. The order is not arbitrary.

#### What you must understand BEFORE coding
- Why does order matter: if you deduplicate before trimming, which rows survive?
- How do you deduplicate a list of dicts while preserving order (first occurrence wins)?
- Why use `tuple(sorted(row.items()))` for a hashable row key vs `frozenset(row.items())`?

#### Predict before implementation
1. Two rows: `"foo "` and `" foo"`. After trim: both become `"foo"`. Should one be removed?
2. What does `{k: v.strip() for k, v in row.items()}` do to a `None` value?

#### Implementation scope
- `CleanResult` dataclass: `cleaned_rows: list[dict]`, `duplicate_rows_removed: int`
- `clean(rows: list[dict]) -> CleanResult`:
  - Step 1: Trim — `{k: v.strip() if isinstance(v, str) else v for k, v in row.items()}`
  - Step 2: Deduplicate using `seen: set[tuple]` with `tuple(row.items())` as the key; retain first occurrence
- `write_csv(file_path: str, column_names: list[str], rows: list[dict]) -> None`:
  - `csv.DictWriter` with `extrasaction='ignore'`; write header then rows

#### Tests
- No duplicates: all rows retained, `duplicate_rows_removed=0`
- All identical rows: one retained, `duplicate_rows_removed=n-1`
- Rows identical only after trim: deduped — proves trim-before-dedup order
- Rows differing in non-whitespace content: both retained
- Empty input: `duplicate_rows_removed=0`, empty output
- `write_csv` output is re-readable by `csv.DictReader` with identical content

#### Failure exercise
Swap the order: deduplicate before trim. What test now fails? Why?

#### Explain-back
- Why does trim need to run before dedup?
- What is the time complexity of the deduplication using a set?

#### Interview defense
1. What is the difference between set-based and sort-based deduplication? What are the trade-offs?
2. If a row has 1 million cells, what does `tuple(row.items())` do to memory?

#### Verification
```bash
pytest tests/unit/test_csv_cleaner.py -v
```

#### Commit checkpoint
`feat: CSV cleaner — trim-then-deduplicate pipeline`

---

### T-07 — S3 client wrapper (presigned URLs, HeadObject, upload/download)
**Day:** 2 | **CORE** | **Estimated time:** 2 hours  
**Dependencies:** T-01

#### Why this task exists
All S3 operations — generating presigned PUT URLs (with size limit enforcement), verifying uploaded objects, downloading for the worker, uploading processed output — live here. This is the module that implements AD-02 and AD-27.

#### What you must understand BEFORE coding
- How does a presigned URL work? Who validates the signature?
- What is a `content-length-range` condition and when does S3 enforce it?
- What is the difference between `HeadObject` (check existence/metadata) and `GetObject` (download content)?
- What IAM permission does the Lambda need to generate a presigned PUT URL? (Answer: `s3:PutObject` on the target key)
- What does `AWS_ENDPOINT_URL` do for boto3?

#### Predict before implementation
1. If you generate a presigned PUT URL with `content-length-range=[0, 100]` and the browser uploads 200 bytes, what HTTP status does S3 return?
2. If the Lambda has `s3:PutObject` but NOT `s3:GetObject`, can it generate a presigned GET URL?
3. What does `HeadObject` return if the key doesn't exist?

#### Implementation scope
- `backend/s3_client.py`:
  - `get_s3_client()`: boto3 S3 client; respects `AWS_ENDPOINT_URL` for LocalStack
  - `generate_presigned_upload_url(bucket, key, expiry_seconds=900) -> str`: presigned PUT with `ContentType: text/csv` and `content-length-range` condition `[0, 52428800]`
  - `generate_presigned_download_url(bucket, key, expiry_seconds=900) -> str`: presigned GET
  - `object_exists(bucket, key) -> bool`: HeadObject; returns `False` for 404; re-raises other errors
  - `get_object_size(bucket, key) -> int`: HeadObject; returns `ContentLength`; raises if key absent
  - `upload_file(bucket, key, file_path) -> None`: worker use
  - `download_file(bucket, key, dest_path) -> None`: worker use
- `worker/s3_client.py`: import and re-export from `backend.s3_client` (the worker image includes `backend/` — see AD-28)

#### Tests (using moto)
- `generate_presigned_upload_url` returns a string containing bucket name and key
- `object_exists` returns `True` for existing object, `False` for missing key
- `object_exists` does NOT raise for a missing key
- `get_object_size` returns correct byte count for an uploaded object
- `upload_file` + `download_file` round-trip a small file without data corruption

#### Failure exercise
Remove `content-length-range` from the presigned URL generation. Write a test that uploads a 60 MiB object and verify it succeeds (proving the enforcement is gone). Then put it back and see S3 reject the upload.

#### Explain-back
- Walk through: browser wants to upload → what happens step by step, including who holds what credentials?
- Why does the file never travel through the API Lambda?

#### Interview defense
1. Who validates a presigned URL — the browser, the API, or S3?
2. What is the difference between a presigned URL and a bucket policy?
3. What AWS IAM permissions are required to *generate* a presigned PUT URL?

#### Verification
```bash
pytest tests/unit/test_s3_client.py -v
```

#### Commit checkpoint
`feat: S3 client wrapper — presigned URLs with content-length-range, HeadObject, upload/download`

---

### T-08 — SQS client wrapper
**Day:** 2 | **CORE** | **Estimated time:** 1 hour  
**Dependencies:** T-01

#### Why this task exists
The SQS message is the trigger for the entire async processing pipeline. The message contract — job_id and s3_key only, never file content — is a security and architectural decision (SQS has a 256 KB limit; file content belongs in S3).

#### What you must understand BEFORE coding
- Why does the SQS message contain only `job_id` and `s3_key` and not the CSV content?
- What is at-least-once delivery? What does it mean for the worker?
- What is the visibility timeout and what happens when it expires?

#### Predict before implementation
1. If the worker takes 400 seconds and visibility timeout is 1800 seconds, is the message redelivered during processing?
2. If two workers start at the same time for the same message (visibility timeout has expired during the first attempt), what prevents double-processing?

#### Implementation scope
- `backend/sqs_client.py`:
  - `get_sqs_client()`: boto3 SQS client; respects `AWS_ENDPOINT_URL`
  - `publish_job_message(queue_url, job_id, s3_key) -> None`: sends `json.dumps({"job_id": ..., "s3_key": ...})`

**Message contract:**
```json
{"job_id": "<uuid>", "s3_key": "uploads/<user_id>/<job_id>/original.csv"}
```
File content is NEVER in the message.

#### Tests (using moto)
- `publish_job_message` sends message to mocked queue
- Message body parses to dict with exactly `job_id` and `s3_key`
- No other fields in the message body

#### Failure exercise
Add a `file_content` field to the message. Write a 300 KB string. What happens when you try to send it? What AWS error do you get?

#### Explain-back
- Why is SQS used instead of direct Lambda invocation?
- What is a Dead Letter Queue and when does a message end up there?

#### Interview defense
1. What is the difference between SQS and SNS?
2. What does at-least-once delivery mean for system design?
3. Why is `batch_size=1` used in the event source mapping?

#### Verification
```bash
pytest tests/unit/test_sqs_client.py -v
```

#### Commit checkpoint
`feat: SQS client wrapper — job message publishing`

---

# DAY 3 — Worker Lambda + Docker + Frontend Scaffold

**Learning goal:** By end of Day 3 the worker is locally runnable, the Docker image builds and passes a real Lambda Runtime Interface Emulator invocation test, and the frontend scaffold is running.

---

### T-09 — Worker Lambda handler (complete 12-step idempotent flow)
**Day:** 3 | **CORE** | **Estimated time:** 3–4 hours  
**Dependencies:** T-04, T-06, T-07, T-08

#### Why this task exists
This is the heart of the async pipeline. The handler must be idempotent — safe to run multiple times on the same job. It must distinguish permanent failures (don't retry) from transient failures (do retry). It must write S3 before PostgreSQL. Every one of these choices has a real consequence if violated.

#### What you must understand BEFORE coding
- The idempotency double-check: why both a status check AND a conditional UPDATE?
- Why S3 is written BEFORE PostgreSQL (AD-14): what happens to each order if a crash occurs?
- Why the handler returns normally for permanent failures and raises for transient failures
- Why `user_id` comes from `job.user_id` (database), NOT from the S3 key path (AD-28)
- Why `/tmp` files must be deleted at the end of every invocation (warm Lambda reuse)

#### Predict before implementation
1. The worker writes the processed file to S3. Then PostgreSQL raises a connection error. SQS retries. What happens on the retry?
2. The worker updates PostgreSQL to `completed`. Then S3 raises an error during write. Is this possible? Why was S3 written first?
3. Two SQS messages for the same job arrive simultaneously. The first worker claims it (UPDATE sets `processing`). What does the second worker's conditional UPDATE return? What does the second worker do?

#### Implementation scope
- `worker/handler.py`: `lambda_handler(event: dict, context) -> None`
- 12-step flow (from design §5.2):
  1. Parse `job_id` and `s3_key` from `event["Records"][0]["body"]` (JSON)
  2. Fetch `job = get_job_by_id(db, job_id)` — **`user_id = job.user_id` from DB, NOT from s3_key**
  3. Idempotency: if `completed` or `failed` → log + return (do NOT raise)
  4. Claim: `claim_job_for_processing()` → if False → log + return
  5. Download: `download_file(bucket, s3_key, /tmp/{job_id}/original.csv)`
  6. Parse: `parse_and_analyse()` → catch `InvalidCSVError` → `fail_job()`, clean /tmp, return normally
  7. Analyse: `row_count`, `column_count`, `column_names` from `AnalysisResult`
  8. Clean: `clean(result.rows)`
  9. Write output: `write_csv(/tmp/{job_id}/result.csv, ...)`
  10. **Upload to S3 BEFORE DB update**: `upload_file(bucket, processed_key, /tmp/{job_id}/result.csv)`
  11. `complete_job()` with all metadata
  12. Delete `/tmp/{job_id}/`, return normally
- Transient errors (S3, DB): let exception propagate — SQS retries
- Never log CSV cell values; only log `job_id` and status
- `if __name__ == "__main__":` block for local CLI execution: `python worker/handler.py --job-id <uuid>`

#### Tests
- `completed` job: `claim_job_for_processing` NOT called (mock repo)
- `failed` job: same
- `claim_job_for_processing` returns False: no processing occurs
- `InvalidCSVError`: `fail_job()` called; no exception raised; function returns normally
- Transient S3 error: exception propagates (SQS will retry)
- Successful processing: `complete_job()` called with correct metadata
- `/tmp` files deleted after success
- `/tmp` files deleted after permanent failure
- `user_id` in `processed_s3_key` comes from `job.user_id`, NOT parsed from `s3_key`

#### Failure exercise
Comment out the S3 upload step (step 10). Run the worker. The job shows `completed` in the DB but no processed file exists. Now try to download it. What happens? Understand why S3 must come before the DB update.

#### Explain-back
- Trace a message from SQS → handler step 1 → PostgreSQL step 4 → S3 step 10 → PostgreSQL step 11
- What makes this handler idempotent?
- What is the "crash window" between S3 write and DB update? (AD-14)

#### Interview defense
1. What is idempotency and why does it matter for SQS consumers?
2. What happens when a Lambda function times out mid-processing? What does SQS do?
3. Why is the S3 output key deterministic? What would happen with a random suffix?

#### Verification
```bash
pytest tests/unit/test_worker_handler.py -v
# Also: python worker/handler.py --job-id <real-uuid-in-local-db>
```

#### Commit checkpoint
`feat: Worker Lambda handler — 12-step idempotent processing flow`

---

### T-10 — Worker Dockerfile + ECR build + Lambda RIE invocation test
**Day:** 3 | **CORE** | **Estimated time:** 2 hours  
**Dependencies:** T-09

#### Why this task exists
The Docker image is the production packaging for the worker. The build context decision (project root, not `./worker/`) and the need to include `backend/` alongside `worker/` are real, non-obvious decisions. The RIE test is the only way to verify the Lambda handler actually executes inside the container (not just that the container starts).

#### What you must understand BEFORE coding
- What is the Docker build context? Why does `docker build ./worker/` prevent copying `backend/`?
- What is `LAMBDA_TASK_ROOT`? How does Python's `sys.path` work inside the container?
- What is the Lambda Runtime Interface Emulator (RIE) and what endpoint does it expose?
- What is the difference between "container starts without errors" and "Lambda handler invoked successfully"?

#### Predict before implementation
1. If you run `docker build ./worker/` and the Dockerfile says `COPY backend/ ...`, what error do you get?
2. If `LAMBDA_TASK_ROOT` is `/var/task` and `backend/` is at `/var/task/backend/`, what import path resolves `from backend.db.connection import ...`?

#### Implementation scope
`worker/Dockerfile`:
```dockerfile
FROM public.ecr.aws/lambda/python:3.14
COPY worker/requirements.txt .
RUN pip install -r requirements.txt --no-cache-dir
COPY backend/ ${LAMBDA_TASK_ROOT}/backend/
COPY worker/ ${LAMBDA_TASK_ROOT}/
CMD ["handler.lambda_handler"]
```

Build context = project root (AD-18):
```bash
docker build -f worker/Dockerfile -t csvforge-worker .
```

- `.dockerignore` at project root: `.env`, `__pycache__`, `.git`, `frontend/`, `tests/`, `*.pyc`
- `worker/run_local.sh`: sets `PYTHONPATH=$(pwd)` (project root), runs `python worker/handler.py --job-id $1`

#### Tests
- `docker build -f worker/Dockerfile -t csvforge-worker .` exits 0
- **Real Lambda handler invocation via RIE:**
  ```bash
  docker run -p 9000:8080 --env-file .env csvforge-worker &
  curl -XPOST "http://localhost:9000/2015-03-31/functions/function/invocations" \
    -d '{"Records":[{"body":"{\"job_id\":\"test\",\"s3_key\":\"uploads/u/j/original.csv\"}"}]}'
  # Expected: handler runs (may fail on missing job, but must not fail on import errors)
  ```

#### Failure exercise
Change the Dockerfile to use `./worker/` as the build context: `docker build -f worker/Dockerfile -t bad-worker ./worker/`. Show the COPY error for `backend/`. Understand why context must be the project root.

#### Explain-back
- Why does the Dockerfile copy both `backend/` and `worker/` into `LAMBDA_TASK_ROOT`?
- What is the difference between "the container starts" and "the handler executes"?

#### Interview defense
1. What is a Docker build context and what determines it?
2. How does Lambda resolve Python imports from a container image?
3. What is the Lambda Runtime Interface Emulator used for?

#### Verification
```bash
docker build -f worker/Dockerfile -t csvforge-worker .
docker run --env-file .env csvforge-worker python -c "from backend.db.connection import Base; print('imports ok')"
```

#### Commit checkpoint
`feat: Worker Dockerfile with project-root build context (AD-18); RIE test`

---

### T-11 — Frontend scaffold (React + TypeScript + Vite + MUI + React Router)
**Day:** 3 | **CORE** | **Estimated time:** 1.5 hours  
**Dependencies:** none (can run parallel to T-09 and T-10)

#### Why this task exists
The frontend needs a working dev server, typed component skeleton, and test runner before any UI work begins. All subsequent UI tasks depend on this scaffold.

#### What you must understand BEFORE coding
- Why Vite instead of Create React App? (CRA is deprecated; Vite is faster and actively maintained)
- Why TypeScript? What does strict mode catch that plain JavaScript misses?
- Why `react-router` (v8 package) and not `react-router-dom`? (v8 re-exports from the root package)
- What is the `VITE_` prefix on environment variables?

#### Implementation scope
- Scaffold `frontend/` with Vite + React + TypeScript
- Install exact versions: `react@19.3.0`, `react-dom@19.3.0`, `@mui/material@9.0.0`, `@mui/icons-material@9.0.0`, `@emotion/react`, `@emotion/styled`, `react-router@8.2.0`, `react-admin@5.8.0`, `ra-data-simple-rest`, `amazon-cognito-identity-js@6.3.15`
- Dev deps: `vitest@3.1.0`, `@testing-library/react@16.3.0`, `@testing-library/user-event`, `@vitest/coverage-v8`, `jsdom`, `typescript@6.0.0`
- `vite.config.ts`: dev-server proxy `/api` → `http://localhost:8000`; Vitest jsdom environment
- `src/main.tsx`: `<BrowserRouter>` wrapping `<App />`
- `src/App.tsx`: placeholder routes `/login`, `/upload`, `/jobs/:jobId`, `/admin`
- `frontend/.env.example`: `VITE_COGNITO_USER_POOL_ID`, `VITE_COGNITO_APP_CLIENT_ID`, `VITE_API_BASE_URL`
- Add `test` and `test:watch` scripts

#### Tests
- Vitest smoke test: `<App />` renders without throwing

#### Failure exercise
Remove the Vite proxy. Call `fetch('/api/health')` from the browser. What error do you see in the browser console? Why?

#### Verification
```bash
npm run dev        # serves on localhost:5173
npm run test       # passes smoke test
npm run build      # produces dist/ without TS errors
```

#### Commit checkpoint
`feat: React + TypeScript + Vite + MUI + React Router scaffold`

---

# DAY 4 — FastAPI API Layer

**Learning goal:** By end of Day 4 the API endpoints are implemented, Pydantic validates all input, JWT auth dependency extracts identity, and upload + status + download flows are wired end-to-end.

---

### T-12 — Pydantic v2 schemas + structured JSON logging
**Day:** 4 | **CORE** | **Estimated time:** 1.5 hours  
**Dependencies:** T-03

#### Why this task exists
Pydantic is the first line of defence — it rejects bad input before it reaches the database or S3. Structured JSON logging makes every request traceable in CloudWatch without exposing CSV content.

#### What you must understand BEFORE coding
- What is the difference between Pydantic v1 and v2 API? (`model_config = ConfigDict(...)` vs `class Config:`)
- What is a `@field_validator` and when does it run?
- Why is 52,428,800 bytes the correct limit for "50 MB"? (50 MiB = 50 × 1024 × 1024)
- What is structured logging and why is it preferable to unstructured strings in production?

#### Implementation scope
- `backend/schemas/upload.py`: `UploadIntentRequest` (filename `.csv` required, `file_size_bytes` 1–52428800), `UploadIntentResponse`, `UploadCompleteResponse`
- `backend/schemas/job.py`: `JobResponse` (user-visible fields only; no S3 keys), `DownloadResponse`
- `backend/schemas/admin.py`: `AdminJobResponse` (all fields including `user_email`, `failure_reason`)
- Structured JSON logging middleware in `backend/main.py`: logs `method`, `path`, `status_code`, `duration_ms`, `job_id` — never request body or response body

#### Tests
- `UploadIntentRequest` rejects non-`.csv` filename
- `UploadIntentRequest` rejects `file_size_bytes=0`
- `UploadIntentRequest` rejects `file_size_bytes=52428801`
- `UploadIntentRequest` accepts `file_size_bytes=52428800` (boundary: must succeed)
- `UploadIntentRequest` accepts `file_size_bytes=1` (boundary)

#### Failure exercise
Change the validator to use `50_000_000` (50 MB decimal) instead of `52_428_800` (50 MiB). Show a test that uploads a 51 MB file and passes one limit but not the other. Understand the binary/decimal distinction.

#### Commit checkpoint
`feat: Pydantic v2 schemas + structured JSON logging middleware`

---

### T-13 — JWT dependency + local dev bypass
**Day:** 4 | **CORE** | **Estimated time:** 2 hours  
**Dependencies:** T-12

#### Why this task exists
This is the authentication/authorization boundary. The JWT dependency extracts `user_id` from the API Gateway-validated token and provides it to every protected endpoint. Getting this wrong means either broken auth or a broken local dev experience.

#### What you must understand BEFORE coding
- What does API Gateway do before FastAPI even runs? (Validates JWT signature against Cognito JWKS)
- Why does FastAPI NOT re-validate the JWT? (API Gateway already did)
- Why is `email` NOT in the access token? (It's in the ID token only — AD-23)
- How does Mangum map the API Gateway event to ASGI scope?
- Why is the local dev bypass strictly gated on `ENVIRONMENT=local`?

#### Predict before implementation
1. If API Gateway passes `sub: "abc-123"` in the JWT claims, what is `user_id`?
2. If a user has no Cognito groups, what does `claims.get("cognito:groups", [])` return?
3. If `ENVIRONMENT=production` and `X-Dev-User-Id: test` is sent, what happens?

#### Implementation scope
- `backend/auth.py`:
  - `UserContext` dataclass: `user_id: str`, `user_email: str`, `groups: list[str]`
  - `get_current_user(request: Request) -> UserContext`:
    - `ENVIRONMENT=local`: read `X-Dev-User-Id` and `X-Dev-User-Email` headers
    - Production: read from `request.scope["aws.event"]["requestContext"]["authorizer"]["jwt"]["claims"]`; raise `HTTPException(401)` if missing
  - `require_admin` dependency: `get_current_user` + check `"csvforge-admins" in user.groups`; raise `HTTPException(403)` if not admin

#### Tests
- Local bypass with headers → correct `UserContext`
- Production: missing `sub` claim → 401
- `require_admin`: non-admin → 403
- `require_admin`: admin (groups contains `csvforge-admins`) → passes
- `require_admin`: NO `cognito:groups` claim at all → empty list → 403 (not KeyError)
- **Security test:** `user_email` in request body does NOT affect ownership checks

#### Failure exercise
Remove the `ENVIRONMENT=local` gate from the bypass. What security vulnerability have you introduced? Write a test that exploits it.

#### Commit checkpoint
`feat: JWT auth dependency + strictly-gated local dev bypass (AD-19, AD-23)`

---

### T-14 — Upload intent and complete endpoints
**Day:** 4 | **CORE** | **Estimated time:** 2 hours  
**Dependencies:** T-04, T-07, T-08, T-13

#### Why this task exists
This is the upload orchestration: create the job record, generate the presigned URL, then verify the upload happened and transition to `queued`. The two-step design exists because S3 doesn't notify the API when an upload completes — the browser must call back.

#### What you must understand BEFORE coding
- Why are there TWO API calls for one upload? (S3 has no callback mechanism)
- What does the complete endpoint verify before transitioning to `queued`?
- What is the DB-before-SQS failure window? (AD-29)
- Why does the presigned URL include `content-length-range`? (AD-27)

#### Implementation scope
`backend/routers/uploads.py`:

**Intent endpoint** (`POST /api/v1/uploads/intent`):
1. Validate `UploadIntentRequest`
2. Extract `user_id`, `user_email` from `get_current_user`
3. Generate `job_id` (UUID4)
4. S3 key: `uploads/{user_id}/{job_id}/original.csv`
5. `create_job()` → status `pending_upload`
6. `generate_presigned_upload_url()` with `content-length-range` → 15-min expiry
7. Return 200 `UploadIntentResponse`

**Complete endpoint** (`POST /api/v1/uploads/{job_id}/complete`):
1. `get_job_by_id_and_user()` → 404 if not found or wrong owner
2. `object_exists()` → 422 if S3 object missing
3. `get_object_size()` → 422 if `ContentLength > 52_428_800` (AD-27)
4. `transition_to_queued()` → 409 if already queued
5. `publish_job_message()` with `job_id` and `s3_key`
6. Return 202 `UploadCompleteResponse`

#### Tests
- Intent: valid → job created `pending_upload`; response has `upload_url` and `job_id`
- Intent: non-`.csv` filename → 422
- Intent: file too large (declared) → 422
- Complete: valid → job transitions to `queued`; SQS message sent
- Complete: S3 object missing → 422
- Complete: S3 object too large (> 52 MiB) → 422
- Complete: wrong user → 404
- Complete: called twice → 409

#### Failure exercise
Remove the `HeadObject` ContentLength check from the complete endpoint. Show that a client can declare `file_size_bytes=1000` in the intent call, upload a 100 MiB file, and the API marks the job `queued` for the worker to process. Understand why server-side size verification matters.

#### Commit checkpoint
`feat: Upload intent + complete endpoints (AD-02, AD-27)`

---

### T-15 — Job status + download endpoints
**Day:** 4 | **CORE** | **Estimated time:** 1.5 hours  
**Dependencies:** T-14

#### Why this task exists
Users need to poll status and download their results. The download endpoint enforces ownership + status before generating a presigned URL. These endpoints are the simplest expression of authorization: always scope by `user_id`.

#### Implementation scope
`backend/routers/jobs.py`:
- `GET /api/v1/jobs` → `list_jobs_for_user()`; excludes `pending_upload`
- `GET /api/v1/jobs/{job_id}` → `get_job_by_id_and_user()`; 404 if wrong owner
- `GET /api/v1/jobs/{job_id}/download` → ownership check + `status == completed` check (409 if not complete) + `generate_presigned_download_url()`

#### Tests
- `GET /jobs` excludes `pending_upload` jobs
- `GET /jobs/{id}` returns 404 for a different user's job
- `GET /jobs/{id}/download` on `completed` → returns `download_url`
- `GET /jobs/{id}/download` on `processing` → 409
- `GET /jobs/{id}/download` for different user → 404

#### Commit checkpoint
`feat: Job status and download endpoints with ownership enforcement`

---

# DAY 5 — AWS Networking + IAM + CloudFormation Foundation

**Learning goal:** By end of Day 5 you can explain why each AWS service needs the VPC configuration it has, what VPC endpoints do and why, and how IAM least privilege is enforced via exact ARNs.

---

### T-16 — Admin endpoints (React Admin conventions)
**Day:** 5 | **CORE** | **Estimated time:** 1.5 hours  
**Dependencies:** T-13, T-15

#### Why this task exists
React Admin's `ra-data-simple-rest` provider expects specific conventions: `_start`/`_end` query params, `Content-Range` response header. If the API doesn't match these exactly, the admin table breaks silently.

#### Implementation scope
`backend/routers/admin.py`:
- `GET /api/v1/admin/jobs` → `require_admin`; reads `_start`, `_end`, `_sort`, `_order`, optional `status` filter; returns list + `Content-Range: jobs {start}-{end}/{total}` header

#### Tests
- Non-admin: 403
- Admin: 200 with jobs list and `Content-Range` header
- Status filter `?status=failed`: only failed jobs
- Pagination: `_start=0&_end=5` returns max 5 jobs

#### Commit checkpoint
`feat: Admin endpoints with React Admin ra-data-simple-rest conventions`

---

### T-17 — network.yaml — VPC, private subnets, VPC endpoints
**Day:** 5 | **CORE** | **Estimated time:** 2–3 hours  
**Dependencies:** T-01

#### Why this task exists
Lambda functions in private subnets cannot reach the internet. Without VPC endpoints, they cannot reach S3, SQS, or Secrets Manager. This template is what makes the no-NAT architecture work.

#### What you must understand BEFORE coding
- What is the difference between a Gateway endpoint (S3/DynamoDB only; free; route-table-based) and an Interface endpoint (PrivateLink; per-AZ ENI; hourly cost)?
- Why is no NAT Gateway provisioned? What does that mean for connectivity?
- Why does S3 use a Gateway endpoint and SQS uses an Interface endpoint?
- What does `EnableDnsHostnames: true` enable for Interface endpoints?

#### Predict before implementation
1. Lambda is in a private subnet with a Gateway endpoint for S3. Can it reach `amazonaws.com` (the domain), or does it use a route table entry?
2. Lambda is in a private subnet with no SQS Interface endpoint. Can it call `sqs.us-east-1.amazonaws.com`?
3. What CIDR would you give two subnets in `10.0.0.0/16`?

#### Implementation scope
`infrastructure/network.yaml`:
- `AWS::EC2::VPC`: CIDR 10.0.0.0/16, `EnableDnsHostnames: true`, `EnableDnsSupport: true`
- Two private subnets (10.0.1.0/24, 10.0.2.0/24) in different AZs
- Two private route tables + subnet associations
- Lambda SG: all egress allowed; no inbound
- VPC Endpoint SG: inbound 443 from Lambda SG
- S3 Gateway endpoint: added to both private route tables
- SQS Interface endpoint: `com.amazonaws.{region}.sqs`; both private subnets; VPC Endpoint SG
- Secrets Manager Interface endpoint: `com.amazonaws.{region}.secretsmanager`; same placement
- **Outputs:** VPC ID, SubnetId1, SubnetId2, LambdaSecurityGroupId

#### Failure exercise
Remove the SQS Interface endpoint and deploy. Run the worker. What error do you get when it tries to call SQS? (Expected: connection timeout or DNS resolution failure)

#### Interview defense
1. What is the difference between a Gateway endpoint and an Interface (PrivateLink) endpoint?
2. Why is there no Internet Gateway in this VPC?
3. Why does each Interface endpoint cost money but the S3 Gateway endpoint is free?

#### Commit checkpoint
`feat: network.yaml — VPC, private subnets, VPC endpoints (no NAT, AD-15)`

---

### T-18 — data.yaml — RDS PostgreSQL + S3 bucket + CORS
**Day:** 5 | **CORE** | **Estimated time:** 2 hours  
**Dependencies:** T-17

#### Why this task exists
The data layer: the database that stores job state, the S3 bucket that stores CSV files. The S3 CORS configuration is required for the browser to make direct PUT requests to S3.

#### What you must understand BEFORE coding
- What is a DB subnet group and why must it span 2 AZs? (AD-25)
- What does `PubliclyAccessible: false` do for RDS?
- What is the difference between S3 bucket CORS and API Gateway CORS?
- Why does the Secrets Manager secret contain only username/password (not host)? (AD-24)

#### Implementation scope
`infrastructure/data.yaml`:
- `AWS::RDS::DBSubnetGroup` — both private subnets
- RDS SG: inbound 5432 from Lambda SG only
- `AWS::SecretsManager::Secret` — auto-generated password; `{"username":"csvforge","password":"<generated>"}` only
- `AWS::RDS::DBInstance` — PostgreSQL 16, db.t3.micro, single-AZ, private, `PubliclyAccessible: false`, `StorageEncrypted: true`
- `AWS::S3::Bucket` — `PublicAccessBlockConfiguration` all true; `BucketEncryption: AES256`; S3 CORS rule: PUT allowed from `localhost:5173` and frontend bucket origin; `Content-Type` allowed
- **Outputs:** RDS endpoint, Secret ARN, S3 bucket name and ARN

#### Commit checkpoint
`feat: data.yaml — RDS, S3 bucket with CORS, Secrets Manager (AD-24, AD-27)`

---

### T-19 — queues.yaml + auth.yaml — SQS + Cognito
**Day:** 5 | **CORE** | **Estimated time:** 2 hours  
**Dependencies:** T-17

#### Why this task exists
The queue with correct visibility timeout is what makes the worker retry-safe. The Cognito User Pool is what makes the frontend authentication work. Both are foundational — everything else depends on them.

#### What you must understand BEFORE coding
- Why is visibility timeout 1800s (6× Lambda timeout)? What happens if it's too short? (AD-17)
- What is `maxReceiveCount` and what happens after it's exceeded?
- What is the difference between a Cognito User Pool and an Identity Pool?
- Why does the App Client have no client secret? (SPA cannot keep a secret)

#### Implementation scope
`infrastructure/queues.yaml`:
- DLQ `csv-jobs-dlq`; retention 14 days
- Main queue `csv-jobs`: `VisibilityTimeout: 1800`, `MaxReceiveCount: 5`, redrive to DLQ
- CloudWatch Alarm: `ApproximateNumberOfMessagesVisible` on DLQ > 0
- **Outputs:** main queue URL, main queue ARN, DLQ ARN

`infrastructure/auth.yaml`:
- `AWS::Cognito::UserPool` — email sign-in, password policy, `DeletionProtection: ACTIVE`
- `AWS::Cognito::UserPoolClient` — no secret; `ALLOW_USER_PASSWORD_AUTH`; NO `AllowedOAuthFlows` (not needed for direct SDK auth — AD-05 of Cognito decisions)
- `AWS::Cognito::UserPoolGroup` — `csvforge-admins`
- **Outputs:** User Pool ID, App Client ID, Cognito issuer URL

#### Failure exercise
Set `VisibilityTimeout: 300` (equal to Lambda timeout). Explain why this creates a race condition where SQS might redeliver a message while the worker is still processing it.

#### Commit checkpoint
`feat: queues.yaml (SQS 1800s, DLQ, alarm) + auth.yaml (Cognito User Pool, AD-17)`

---

# DAY 6 — Application CloudFormation + React Authentication

**Learning goal:** By end of Day 6 the API and Worker Lambdas are deployed, authenticated requests reach FastAPI, and the React login form obtains a real Cognito access token.

---

### T-20 — api.yaml + worker.yaml + deploy.sh
**Day:** 6 | **CORE** | **Estimated time:** 3–4 hours  
**Dependencies:** T-17, T-18, T-19, T-10

#### Why this task exists
The application stacks deploy the Lambda functions with correct IAM roles, VPC placement, and API Gateway JWT authorizer. IAM least privilege — exact ARNs, no wildcards — is a deliberate learning exercise here.

#### What you must understand BEFORE coding
- How does API Gateway HTTP API validate JWTs without calling Cognito on every request? (JWKS caching)
- What IAM permissions does the API Lambda need to *generate* a presigned PUT URL?
- What is the difference between API Lambda and Worker Lambda IAM roles?
- Why does the Worker not depend on API Gateway outputs? (AD-26)
- How does `aws cloudformation deploy` differ from `aws cloudformation create-stack`?

#### Implementation scope
`infrastructure/api.yaml`: API Lambda (zip, python3.14, 256 MB, 30s timeout), API Gateway HTTP API, JWT authorizer (Cognito issuer + App Client audience), IAM role (AD-20 exact ARN S3 permissions, SQS SendMessage, Secrets Manager GetSecretValue, CloudWatch Logs, VPC)

`infrastructure/worker.yaml`: ECR repo, Worker Lambda (container image, 1024 MB, 300s timeout), SQS event source mapping (BatchSize=1, no ReportBatchItemFailures), IAM role (AD-20 exact ARN S3 permissions, SQS receive/delete, Secrets Manager, CloudWatch Logs, VPC)

`infrastructure/deploy.sh`: sequential `aws cloudformation deploy` calls; outputs passed as parameters between stacks; documents bootstrap vs CI/CD stacks:
```
Bootstrap (once): network → data → queues → auth
CI/CD managed:    api → worker
```

#### Failure exercise
Deliberately add an extra IAM permission (`s3:DeleteObject`) that's not in AD-20. Note which AD this violates and why least privilege matters.

#### Interview defense
1. What does API Gateway do before FastAPI's Lambda is invoked?
2. What is the difference between `PackageType: Zip` and `PackageType: Image`?
3. How does CloudFormation pass outputs from one stack to another?

#### Commit checkpoint
`feat: api.yaml, worker.yaml, deploy.sh — application stacks with least-privilege IAM (AD-20, AD-26)`

---

### T-21 — React auth context + Cognito login form
**Day:** 6 | **CORE** | **Estimated time:** 2–3 hours  
**Dependencies:** T-11, T-19

#### Why this task exists
The authentication flow connects the React frontend to Cognito. The key architectural decisions — access token in Bearer header (not ID token), tokens in memory (not localStorage), `user_email` from ID token only for informational storage — are all made concrete here.

#### What you must understand BEFORE coding
- What is the difference between the access token and the ID token? Which one goes in the `Authorization` header?
- Why are tokens stored in React state (memory) and not localStorage? (AD-10)
- What does `initiateAuth(email, password)` return from the Cognito SDK?
- What happens when the access token expires in this MVP?

#### Predict before implementation
1. After a page refresh, is the user still logged in?
2. The access token expires. The user makes an API call. What happens?
3. The frontend stores the email from the ID token. Can a malicious user forge a different email?

#### Implementation scope
- `frontend/src/auth/AuthContext.tsx`: React context with `user: UserInfo | null`, `accessToken: string | null`, `idToken: string | null`, `login(email, password)`, `logout()`
- `frontend/src/auth/useAuth.ts`: hook
- `frontend/src/pages/Login.tsx`: MUI form; calls `login()`; on success decode ID token locally for `user_email`; redirect to `/upload`
- `frontend/src/components/ProtectedRoute.tsx`: redirect to `/login` if `accessToken` is null

#### Tests
- Login form renders email + password fields
- `ProtectedRoute` redirects to `/login` when unauthenticated
- `ProtectedRoute` renders children when authenticated

#### Failure exercise
Store the access token in `localStorage` instead of state. Open browser DevTools → Application → LocalStorage. Show you can read the token. Explain what XSS attack this enables.

#### Commit checkpoint
`feat: React auth context, Cognito login form (AD-10, AD-23)`

---

# DAY 7 — Full Upload UI + Admin + Integration Tests + Defense

**Learning goal:** By end of Day 7 the full user journey works end-to-end, integration tests prove the key behaviors, and you can defend every architectural decision without notes.

---

### T-22 — Upload flow UI + status polling
**Day:** 7 | **CORE** | **Estimated time:** 2 hours  
**Dependencies:** T-14, T-21

#### Why this task exists
The user-facing flow connects the browser to every backend component. It is the visible proof that the system works.

#### Implementation scope
`frontend/src/pages/Upload.tsx`:
- File picker: client-side validation (`.csv` extension, ≤ 50 MiB) — reject before any API call
- `POST /api/v1/uploads/intent` with `{filename, file_size_bytes, user_email}` (email from ID token)
- `PUT presigned_url` directly to S3 with `Content-Type: text/csv`
- `POST /api/v1/uploads/{job_id}/complete`
- Poll `GET /api/v1/jobs/{job_id}` every 3 seconds; display `StatusBadge`; stop when terminal state
- Show `failure_reason` on `failed`

`frontend/src/api/client.ts`: typed API client; auto-injects `Authorization: Bearer {token}`
`frontend/src/components/StatusBadge.tsx`: `queued`/`processing`/`completed`/`failed` with MUI color

#### Tests
- File picker rejects `.txt` (no API call made)
- File picker rejects > 50 MiB (no API call made)
- `StatusBadge` renders correct label for each status

#### Commit checkpoint
`feat: Upload flow UI with client-side validation and status polling`

---

### T-23 — Download UI
**Day:** 7 | **CORE** | **Estimated time:** 45 minutes  
**Dependencies:** T-15, T-22

#### Implementation scope
`Download Processed File` button rendered only when `status === 'completed'`; calls `GET /jobs/{id}/download`; redirects browser to presigned URL.

#### Tests
- Button absent for `processing`; present for `completed`

#### Commit checkpoint
`feat: Download button — rendered only for completed jobs`

---

### T-24 — React Admin admin surface
**Day:** 7 | **CORE** | **Estimated time:** 1.5 hours  
**Dependencies:** T-16, T-21

#### Why this task exists
The admin surface is where React Admin's conventions (Content-Range, _sort, _end params) must match the API exactly. It's also where the `require_admin` FastAPI dependency proves its value — non-admins are blocked at the API level, not just the UI level.

#### Implementation scope
- `frontend/src/admin/AdminApp.tsx`: `<Admin dataProvider={...}>` with `<Resource name="admin/jobs" list={JobsList} />`; data provider configured with auth token injection
- `frontend/src/admin/JobsList.tsx`: `<List>` with `<Datagrid>` showing `id`, `user_email`, `original_filename`, `status`, `created_at`, `failure_reason`; `<SelectInput>` status filter
- `/admin` route protected by `ProtectedRoute`

#### Tests
- `JobsList` renders with mock data provider
- Non-admin cannot access admin API (test via FastAPI TestClient)

#### Commit checkpoint
`feat: React Admin admin surface — jobs list with status filter`

---

### T-25 — End-to-end Cognito auth flow verification
**Day:** 7 | **CORE** | **Estimated time:** 1 hour  
**Dependencies:** T-20, T-21

#### Why this task exists
This is where all the JWT theory becomes real. You verify that API Gateway rejects missing tokens, FastAPI enforces ownership, and admin-only endpoints block regular users — all against the actually deployed system.

#### Implementation scope
- Create test user in Cognito console (`aws cognito-idp admin-create-user`)
- Create admin user + add to `csvforge-admins` group
- Configure `frontend/.env` with real `VITE_COGNITO_USER_POOL_ID`, `VITE_COGNITO_APP_CLIENT_ID`

#### Verification scenarios
1. `GET /api/v1/jobs` without Authorization → 401 (rejected at API Gateway)
2. `GET /api/v1/jobs` with valid access token → 200
3. `GET /api/v1/admin/jobs` with regular user token → 403 (rejected in FastAPI)
4. `GET /api/v1/admin/jobs` with admin token → 200
5. User A's `job_id` queried with User B's token → 404
6. Expired token → 401

#### Commit checkpoint
`docs: Cognito auth flow verified; results in PROGRESS.md`

---

### T-26 — Backend integration tests
**Day:** 7 | **CORE** | **Estimated time:** 2 hours  
**Dependencies:** T-09, T-15

#### Why this task exists
Unit tests with mocks prove logic. Integration tests with real PostgreSQL and real S3 (or moto) prove the components work together. The worker idempotency integration test is particularly important — it's the only way to confirm the conditional UPDATE actually works.

#### Implementation scope
`tests/integration/test_upload_flow.py`:
- `POST /intent` → `PUT presigned_url` (moto) → `POST /complete` → verify job is `queued`

`tests/integration/test_worker.py`:
- Upload real test CSV to moto S3; invoke `worker.handler.lambda_handler` directly; verify `completed`, metadata written, processed file exists in S3
- Invoke handler twice: second invocation returns without processing (idempotency)

`tests/integration/test_auth.py`:
- Unauthenticated request → 401 (local bypass disabled)
- Wrong user → 404

#### Tests
- Complete upload flow → job transitions to `queued`
- Worker idempotency: second invocation on completed job → status stays `completed`, no additional S3 writes
- Permanent failure: binary file → `status=failed`, `failure_reason` non-empty

#### Commit checkpoint
`test: backend integration tests — upload flow, worker idempotency, auth`

---

### T-27 — End-to-end smoke test (10 scenarios)
**Day:** 7 | **CORE** | **Estimated time:** 2 hours  
**Dependencies:** All prior tasks

#### Why this task exists
This is the final proof that the deployed system works end-to-end. Running 10 specific scenarios gives you confidence before any interview or demo. It also forces you to trace each scenario through CloudWatch logs.

#### 10 verification scenarios
1. **Auth:** `GET /api/v1/jobs` without token → 401
2. **Authorization:** User A token + User B job → 404
3. **Admin auth:** regular user → 403; admin → 200
4. **Happy path:** login → upload clean CSV → `queued → processing → completed`
5. **Metadata:** `GET /jobs/{id}` after completion shows correct `row_count`, `column_count`, `column_names`
6. **Whitespace:** upload CSV with padded cells; verify processed file has no trailing whitespace
7. **Deduplication:** upload CSV with duplicate rows; `duplicate_rows_removed > 0`
8. **Download:** `GET /jobs/{id}/download` → presigned URL → file downloads
9. **Permanent failure:** upload binary `.csv` → `status=failed`; `failure_reason` is human-readable
10. **DLQ:** send malformed SQS message; confirm it reaches DLQ; CloudWatch alarm enters ALARM state

#### CloudWatch verification
- Lambda logs show `job_id` at each lifecycle step
- Logs contain NO CSV cell content
- Structured JSON format confirmed

#### Commit checkpoint
`docs: smoke test complete; all 10 scenarios verified; PROGRESS.md updated`

---

# STRETCH TASKS (after all CORE complete)

---

### T-28 — Failure + retry + DLQ integration tests *(STRETCH)*
**Dependencies:** T-26

Write tests for the full retry cycle: force permanent failure, verify no retry; force transient failure, verify retry; send DLQ-destined message, verify DLQ receipt and CloudWatch alarm.

---

### T-29 — Frontend Vitest suite *(STRETCH)*
**Dependencies:** T-22, T-23, T-24

Expand frontend tests: Upload.test.tsx (API call sequence), StatusBadge (100% line coverage), DownloadButton (rendered only for completed).

---

### T-30 — buildspec.yml — CodeBuild *(STRETCH)*
**Dependencies:** T-26

Write the CodeBuild buildspec that runs tests, builds Docker image, pushes to ECR, builds frontend. Build artifacts only — no deployment actions in buildspec.yml (AD-30).

```yaml
phases:
  install:
    commands:
      - pip install -r backend/requirements.txt
      - cd frontend && npm ci
  pre_build:
    commands:
      - docker compose up -d postgres
      - until pg_isready -h localhost -p 5432; do sleep 1; done
  build:
    commands:
      - pytest tests/unit/
      - pytest tests/integration/
      - cd frontend && npm run test && cd ..
      - docker build -f worker/Dockerfile -t csvforge-worker .
      - aws ecr get-login-password | docker login --username AWS --password-stdin $ECR_REGISTRY
      - docker tag csvforge-worker $ECR_URI:$CODEBUILD_BUILD_ID
      - docker push $ECR_URI:$CODEBUILD_BUILD_ID
      - cd frontend && npm run build
  post_build:
    commands:
      - docker compose down
```

---

### T-31 — pipeline.yaml — CodePipeline *(STRETCH)*
**Dependencies:** T-30

Provision CodePipeline: Source (GitHub via CodeConnection) → Build (CodeBuild) → Deploy stage (CloudFormation api.yaml then worker.yaml; S3 sync for frontend). AD-30 separation enforced.

---

### T-32 — CloudWatch alarms + operational runbook *(STRETCH)*
**Dependencies:** T-27

Add CloudWatch alarms for: Lambda error rate > 0 (both API and Worker), jobs stuck in `queued` for > 10 minutes (AD-29 DB-before-SQS failure surface), DLQ depth > 0. Write a brief operational runbook for each alarm: what it means, how to investigate, how to recover.

---

## 7-Day Execution Summary

| Day | Tasks | Focus | Expected hours |
|-----|-------|-------|----------------|
| 1 | T-01 to T-04 | Python, FastAPI, PostgreSQL, SQLAlchemy | 8–9h |
| 2 | T-05 to T-08 | CSV processing, S3, SQS | 7–8h |
| 3 | T-09 to T-11 | Worker Lambda, Docker, Frontend scaffold | 7–8h |
| 4 | T-12 to T-15 | FastAPI API layer complete | 7–8h |
| 5 | T-16 to T-19 | Admin endpoints, VPC, RDS, S3, SQS, Cognito | 8–9h |
| 6 | T-20 to T-21 | Application CloudFormation, React auth | 7–8h |
| 7 | T-22 to T-27 | Upload UI, admin, integration tests, smoke test | 8–10h |

**Total CORE tasks:** 27  
**Total STRETCH tasks:** 5

---

## Final MVP Definition

The MVP is complete when:
1. A user can log in with Cognito credentials
2. Upload a CSV file (≤ 50 MiB) via the browser
3. See the job progress through `queued → processing → completed`
4. Download the processed (trimmed + deduplicated) file
5. See a human-readable error if the file is invalid
6. An admin can see all jobs with status filtering
7. Unauthenticated requests are rejected at API Gateway (401)
8. Cross-user access is rejected in FastAPI (404)

---

## Interview Defense Topics

After Day 7 you should be able to answer:

**Architecture**
- Why does the file never pass through API Gateway or Lambda?
- Why is SQS between the API and worker instead of direct Lambda invocation?
- Why PostgreSQL over DynamoDB for this workload?
- Why Lambda for the worker over ECS/Fargate?

**Security**
- What does API Gateway do before FastAPI runs?
- Why is `user_id` from `sub` claim and not from the request body?
- What is the `content-length-range` presigned URL condition and why is it needed?
- Why are tokens in memory and not localStorage?

**Reliability**
- What is idempotency and how is it implemented in the worker?
- What is at-least-once delivery and what is its consequence?
- What is the visibility timeout and what happens if it's too short?
- What happens if PostgreSQL is unavailable when the worker runs?

**IAM**
- What is the principle of least privilege and how is it enforced here?
- Why does the worker have `s3:GetObject` on `uploads/*` but not `processed/*`?
- How does Lambda access Secrets Manager without hardcoded credentials?

**Networking**
- Why does Lambda need a VPC?
- What is the difference between a Gateway endpoint and an Interface endpoint?
- Why is no NAT Gateway provisioned?
- How does Lambda reach SQS from a private subnet?

**Database**
- What does `pool_pre_ping=True` do for Lambda?
- Why is `pool_size=1` appropriate for Lambda?
- What is the purpose of the conditional UPDATE `WHERE status IN ('queued','processing')`?
- What happens if two workers process the same job concurrently?

**CSV**
- Why are duplicate headers a permanent validation failure?
- Why must trim run before deduplication?
- What is a BOM and how is it handled?

---

## Architecture Issues Resolved in This Plan

| Issue | Resolution | Decision |
|-------|-----------|----------|
| Duplicate CSV headers: was "valid" | Changed to permanent `InvalidCSVError` | AD-21 revised |
| Worker memory 512 MB (undersized) | Increased to 1024 MB | AD-01 |
| Worker Dockerfile missing `backend/` | Added `COPY backend/` to Dockerfile | AD-28 |
| No `content-length-range` on presigned URL | Added to `generate_presigned_upload_url` | AD-27 |
| No `ContentLength` check in complete endpoint | Added `get_object_size` + 422 if exceeded | AD-27 |
| `user_id` parsed from S3 key (untrusted source) | Use `job.user_id` from PostgreSQL record | AD-08 |
| `updated_at` has no `onupdate` | Added `onupdate=func.now()` in Job model | AD-24 |
| SQS visibility timeout 360s (stale) | Corrected to 1800s (6× Lambda timeout) | AD-17 |
| `maxReceiveCount: 3` (below AWS recommendation) | Changed to 5 | AD-17 |
| DB-before-SQS failure window undocumented | Documented as known limitation (AD-29) | AD-29 |
| `--query-stack-outputs` fictional CLI flag | Replaced with real `describe-stacks` pattern | — |
| CI/CD: build and deploy mixed in buildspec | Separated: CodeBuild builds, Deploy stage deploys | AD-30 |
| S3 CORS missing | Added to `data.yaml` `CorsConfiguration` | T-18 |
| API CORS missing `expose_headers: Content-Range` | Added to `CORSMiddleware` and API Gateway | T-01 |
| React Router §3.13 said v6 | Corrected to v8 | — |
| T-33 claimed worker depends on API Gateway | Corrected: worker is sibling (AD-26) | AD-26 |
| Cognito `AllowedOAuthFlows: implicit` unnecessary | Removed; direct SDK auth documented | — |
| R-AUTH-04 contradicted AD-10 | Clarified: user re-authenticates on expiry | AD-10 |
| Docker test proved only container starts | Changed to RIE handler invocation test | T-10 |
| FastAPI 0.140.0 (stale) | Updated to 0.141.1 | AD-22 |

