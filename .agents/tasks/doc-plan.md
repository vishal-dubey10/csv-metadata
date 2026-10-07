# Implementation Plan — Day 1 Educational Documentation

## Critical Constraint

After every change:
```
PYTHONPATH=. DATABASE_URL=sqlite:///:memory: .venv/bin/pytest tests/unit/ -v
```
must pass all **53 tests**. All changes are documentation only — no logic, no renames, no refactors.

Note: The repo has a `.env` file (copied from `.env.example`). The simplest way to run tests
is with the explicit env var override above, or with a `.env` that contains a valid `DATABASE_URL`.

---

## File-by-File Documentation Gap Analysis

### Backend core (the gaps that matter most)

**`backend/db/connection.py`**
Already has a solid module docstring and inline comments. Gaps:
- No analogy for Engine / Pool / Session / Connection (the restaurant analogy is asked for explicitly)
- `get_db()` generator pattern (`yield` inside `try/finally`) is only partially explained — the FastAPI request lifecycle diagram is missing
- `expire_on_commit=False` is stated but not explained with consequences
- `pool_pre_ping=True` is stated but the "stale connection / Lambda warm invocation" problem needs a concrete scenario
- `pool_size=1` is stated but the Lambda connection-scaling arithmetic is missing
- `SessionLocal = sessionmaker(...)` — the "factory that makes factories" concept is not explained
- The `Base(DeclarativeBase)` pattern and why models inherit from it is not explained

**`backend/models/job.py`**
Has column-level comments. Gaps:
- No analogy for ORM mapping (Python class ↔ table row)
- `JobStatus(str, enum.Enum)` — the `str` mixin and why `.value` is often not needed is not explained
- `Mapped[str]` / `mapped_column()` — SQLAlchemy 2.0 typed-mapping syntax vs the older `Column()` style is not explained
- `ARRAY(TEXT).with_variant(TEXT, "sqlite")` — the testing compatibility decision is only one comment, needs full explanation of *why* SQLite can't use ARRAY and what with_variant does
- `server_default=text("CURRENT_TIMESTAMP")` vs `server_default=sa.func.now()` (in the migration) — the difference is not explained
- `onupdate=func.now()` — the exact ORM mechanism (when it fires, what SQL it adds) needs detail
- The `__repr__` method purpose is not explained
- Interview concepts (UUID vs sequential int PK, TIMESTAMPTZ vs TIMESTAMP, ARRAY vs junction table) are not surfaced

**`backend/db/job_repository.py`**
Has good docstrings. Gaps:
- No module-level architecture diagram (who calls these functions, in what sequence)
- `db.flush()` vs `db.commit()` distinction in `create_job` — flush is mentioned but the concept ("writes to DB within the transaction but does not commit") needs more depth
- `db.refresh(job)` — why we need this after flush (server defaults aren't automatically reflected in Python object memory) is not explained
- `synchronize_session="evaluate"` in `transition_to_queued` vs `synchronize_session=False` in `claim_job_for_processing` — the difference and why each choice was made is not explained
- `db.expire_all()` in the state-transition functions — the identity map / session cache problem is mentioned but not given the full treatment it deserves
- `select().scalars().all()` — the SQLAlchemy 2.0 execution model (Result → ScalarResult → list) is not explained
- The conditional UPDATE race condition (two workers on the same job) analysis in `claim_job_for_processing` contains a deliberate admission of complexity ("both can theoretically get rowcount=1") but no PostgreSQL MVCC explanation
- The `func as sa_func` local import pattern in `count_all_jobs` is unexplained
- Interview concepts (conditional UPDATE vs SELECT FOR UPDATE, Unit of Work, repository pattern) are not surfaced

**`backend/main.py`**
Has a module docstring and CORS comments. Gaps:
- No ASGI/WSGI concept explanation (what Mangum does, why uvicorn is needed locally)
- The full request lifecycle diagram (Browser → Uvicorn → FastAPI → Middleware → Route → Dependency → Repository) is not present
- `FastAPI(title=..., version=..., description=...)` — how these fields appear in /docs is not explained
- `allow_credentials=True` — what this enables (cookies, Authorization headers) is not explained
- `expose_headers=["Content-Range"]` — why this specific header needs to be exposed is only half-explained (React Admin context needed)
- The `@app.get("/health")` decorator — how FastAPI registers routes and generates OpenAPI docs from them is not explained
- The `-> dict` return type annotation and Pydantic automatic serialization is not mentioned

**`backend/__init__.py`**, **`backend/db/__init__.py`**, **`backend/models/__init__.py`**
All are empty. Need package docstrings explaining:
- What the package contains
- Why `__init__.py` makes a directory a Python package
- Import conventions for this package

### Alembic

**`backend/alembic/env.py`**
Has a good module docstring and comments. Gaps:
- The `import sys / prepend_sys_path` mechanism is not explained (it's actually handled by alembic.ini `prepend_sys_path = .`)
- The `# noqa: F401` comment on the Job import needs the side-effect import concept explained (why flake8/ruff would flag it, why it's correct anyway)
- `pool.NullPool` in `run_migrations_online` — the distinction from the app's pooled engine is explained but the why (CLI tool vs long-lived server) needs more
- The offline vs online mode mental model (SQL script generation vs live connection) needs an analogy
- `target_metadata = Base.metadata` — what metadata actually is (the schema registry) is not explained

**`backend/alembic/versions/0001_initial_jobs_table.py`**
Has solid inline comments. Gaps:
- `revision`, `down_revision`, `branch_labels` variables — the Alembic DAG (directed acyclic graph) / chain concept is not explained
- `gen_random_uuid()::text` — the `::text` PostgreSQL cast syntax is not explained
- Why status uses `VARCHAR(32)` instead of a PostgreSQL native ENUM (ALTER TYPE locks the table) is mentioned in the upgrade docstring but needs a concrete explanation of what ALTER TYPE does
- The `onupdate` comment in the `updated_at` column correctly states it's handled by the ORM, but doesn't explain why the migration can't set this (DDL vs ORM layers)
- The index explanation is good but the B-tree index concept (how Postgres uses it for WHERE and ORDER BY) is missing

**`alembic.ini`**
Has comments on each section. Gaps:
- `file_template` — the `%(rev)s_%(slug)s` format tokens are explained but not the advantage (sort order, discoverability)
- `prepend_sys_path = .` — explained by a comment but the Python `sys.path` concept needs a sentence
- The logging section (`[loggers]`, `[handlers]`, `[formatters]`) — no explanation of why Alembic has its own logger or how to enable `sqlalchemy.engine` DEBUG logging

### Tests

**`tests/conftest.py`**
Has solid docstrings. Gaps:
- Why `os.environ.setdefault(...)` must happen BEFORE the app imports (module-level code in `connection.py` runs at import time) — this ordering constraint is critical and only partially explained
- `check_same_thread=False` — the SQLite thread restriction is mentioned but not explained with a concrete scenario
- `scope="session"` vs default `scope="function"` — the trade-off is stated but the mental model (one engine vs one engine per test) is not shown
- The transaction rollback isolation pattern in `pg_db` — the savepoint/rollback mechanism is described but a diagram of what's happening at the DB level would help
- Why `TestClient` is used as a context manager (`with TestClient(app) as c:`) instead of just `TestClient(app)` — ASGI lifespan events

**`tests/unit/test_health.py`**
Minimal gaps — it has good docstrings. Needs:
- A brief explanation of how TestClient works (in-process ASGI, no network)
- What `response.json()` does (parses the response body as JSON, no HTTP involved)
- The `client` fixture dependency — where it comes from (conftest.py, pytest fixture injection)

**`tests/unit/test_job_model.py`**
Has good per-class docstrings. Gaps:
- `sa_inspect` is imported but the SQLAlchemy inspection API concept is not explained
- `Job.__table__.columns` / `Job.__table__.c` introspection — how SQLAlchemy exposes the table definition programmatically
- `col.onupdate is not None` — how this introspects the Python-side `onupdate` hook, vs what's in the DB

**`tests/unit/test_job_repository_unit.py`**
Well-documented. Gaps:
- The `_make_job` helper pattern (test factory function) — why it exists, what it encapsulates
- `db.commit()` after `_make_job` — why the commit is needed (in SQLite tests, session isn't auto-committed)
- The `test_second_claim_returns_false` test name is misleading — the test name says "returns False" but the assertion says `second is True`. The test doc explains this, but the test name should ideally be noted as intentionally documenting the surprising behavior

### Infrastructure files

**`Dockerfile`**
Has comments. Gaps:
- `FROM python:3.14-slim` — what "slim" means (minimal OS, no dev tools) and why it's used
- Layer caching order (requirements.txt before source code) — the full explanation of why order matters for Docker layer cache
- `ENV PYTHONPATH=/app` — why this is needed (allows `from backend.xxx import` to work)
- `--no-cache-dir` — disk space in the image
- `--reload` — the Watchfiles mechanism that detects file changes via the volume mount
- `CMD` vs `ENTRYPOINT` distinction
- The production difference (Mangum + Lambda zip, no uvicorn) is referenced but not explained

**`docker-compose.yml`**
Has comments. Gaps:
- `restart: unless-stopped` — the restart policy and when each applies
- `healthcheck` — what `pg_isready` actually does, and how the `depends_on.condition: service_healthy` prevents the race condition
- Named volume vs bind mount — the postgres_data volume persists data, the `./backend` bind mount enables hot reload
- `POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB` — how these initialize the database on first start
- The `depends_on.condition: service_healthy` mechanism and what happens without it

**`.env.example`**
Has comments. Gaps:
- The security warning about never committing `.env` — link to what `.gitignore` entry prevents this
- The two-path architecture (local vs production) for database credentials is described but not mapped to the code that reads it
- `postgresql+psycopg://` vs `postgresql+psycopg2://` — why the driver prefix matters

**`backend/requirements.txt`**
No explanations at all. Every package needs:
- What it is (one line)
- Why it's pinned to an exact version (not `>=`)
- How it fits into the CSVForge architecture

**`backend/requirements-dev.txt`**
Same gaps, plus:
- Why dev deps are separate from prod deps (don't ship test tools to Lambda)
- `moto` — what it is and how it works conceptually
- `httpx2` — why TestClient requires it (starlette dependency)

**`worker/requirements.txt`**
No explanations. Needs the same treatment as `backend/requirements.txt` plus:
- Why `fastapi` and `uvicorn` are absent (the worker has no HTTP server)
- Why `mangum` is absent (mangum is only needed when wrapping FastAPI for Lambda)
- Why `alembic` is absent (the worker doesn't run migrations)

**`worker/__init__.py`**
Empty. Needs a package docstring explaining what the worker package is and what it will contain.

---

## Key Concepts and Which Files Must Explain Them

| Concept | Primary file | Supporting files |
|---------|-------------|-----------------|
| SQLAlchemy Engine | `connection.py` | `requirements.txt` |
| Connection Pool / pool_size / pool_pre_ping | `connection.py` | `docker-compose.yml` |
| Session / Unit of Work | `connection.py` | `job_repository.py` |
| expire_on_commit / expire_all() / identity map | `connection.py` | `job_repository.py` |
| FastAPI dependency injection / Depends / yield | `main.py`, `connection.py` | `conftest.py` |
| ASGI / Mangum / uvicorn | `main.py` | `Dockerfile` |
| CORS / Same-Origin Policy | `main.py` | |
| ORM model mapping / Mapped / mapped_column | `models/job.py` | |
| SQLAlchemy 2.0 select() / execute() / scalars() | `job_repository.py` | |
| Conditional UPDATE / rowcount / atomicity | `job_repository.py` | `0001_initial_jobs_table.py` |
| PostgreSQL MVCC / concurrent UPDATE race | `job_repository.py` | |
| flush() vs commit() vs rollback() | `job_repository.py` | `connection.py` |
| synchronize_session | `job_repository.py` | |
| UUID primary key vs sequential int | `models/job.py` | `0001_initial_jobs_table.py` |
| TIMESTAMPTZ vs TIMESTAMP | `models/job.py` | `0001_initial_jobs_table.py`, `test_migration.py` |
| ARRAY(TEXT).with_variant() | `models/job.py` | `conftest.py` |
| server_default vs Python default vs onupdate | `models/job.py` | `0001_initial_jobs_table.py` |
| Alembic revision chain / down_revision | `env.py`, `0001_initial_jobs_table.py` | `alembic.ini` |
| offline vs online migration mode | `env.py` | |
| Base.metadata.create_all() vs migrations | `env.py` | `conftest.py` |
| pytest fixtures / conftest.py / scopes | `conftest.py` | all test files |
| SQLite in-memory vs PostgreSQL test isolation | `conftest.py` | `test_job_model.py` |
| Transaction rollback isolation pattern | `conftest.py` | `test_job_repository_integration.py` |
| Docker layer caching | `Dockerfile` | |
| docker-compose healthcheck / depends_on | `docker-compose.yml` | |
| Repository pattern | `job_repository.py` | |
| State machine / terminal states | `models/job.py` | `job_repository.py` |
| `str` enum mixin | `models/job.py` | `test_job_model.py` |

---

## Logical Batches for Documentation Work

The implementer must do these sequentially (later batches reference concepts from earlier ones):

### Batch 1 — Foundation layer (no dependencies)
Files that define core concepts others depend on. Document these first.

1. `backend/__init__.py` — package docstring
2. `backend/db/__init__.py` — package docstring
3. `backend/models/__init__.py` — package docstring
4. `worker/__init__.py` — package docstring
5. `backend/requirements.txt` — per-package explanations
6. `backend/requirements-dev.txt` — per-package explanations
7. `worker/requirements.txt` — per-package explanations, explain absences
8. `.env.example` — improve credential architecture explanation

### Batch 2 — Database foundation
Files that define the ORM and connection layer. `models/job.py` depends on `connection.py`.

9. `backend/db/connection.py` — add restaurant analogy, request lifecycle diagram, explain `yield` in `get_db()`, expand `expire_on_commit`, `pool_pre_ping`, `pool_size`, `SessionLocal` factory concept, `Base(DeclarativeBase)` concept
10. `backend/models/job.py` — add ORM-mapping analogy, explain `Mapped`/`mapped_column` vs old `Column()`, full `ARRAY.with_variant()` explanation, UUID/TIMESTAMPTZ interview concepts, `str` enum mixin, `__repr__` purpose

### Batch 3 — Repository and API
Files that build on the database foundation.

11. `backend/db/job_repository.py` — add module architecture diagram, expand `flush()` vs `commit()`, explain `refresh()`, explain `synchronize_session` choices, expand `expire_all()`, explain SQLAlchemy 2.0 result chain, full MVCC race condition analysis with analogy, interview concepts (conditional UPDATE vs SELECT FOR UPDATE, repository pattern)
12. `backend/main.py` — add full ASGI request lifecycle diagram, CORS deep-dive, `expose_headers` React Admin context, route registration and OpenAPI generation, Mangum/Lambda vs uvicorn/local distinction

### Batch 4 — Alembic
Alembic files reference models and connection, so they come after Batch 2.

13. `alembic.ini` — expand file_template tokens, sys.path concept, logging section explanation
14. `backend/alembic/env.py` — explain `# noqa: F401` side-effect import, `pool.NullPool` rationale, `target_metadata`, offline vs online analogy, full architecture diagram
15. `backend/alembic/versions/0001_initial_jobs_table.py` — explain `revision`/`down_revision` chain, `gen_random_uuid()::text` cast, ALTER TYPE locking problem, B-tree index concept, DDL vs ORM layers for `onupdate`

### Batch 5 — Tests
Tests reference all prior concepts; document them last to be able to refer back.

16. `tests/conftest.py` — expand module-import-time ordering constraint, `check_same_thread`, scope trade-offs, `pg_db` transaction isolation diagram, `TestClient` lifespan context manager
17. `tests/unit/test_health.py` — add TestClient in-process explanation, fixture injection explanation
18. `tests/unit/test_job_model.py` — explain `sa_inspect`, `Job.__table__` introspection, `col.onupdate` Python-layer introspection
19. `tests/unit/test_job_repository_unit.py` — explain `_make_job` helper pattern, commit necessity, clarify `test_second_claim_returns_false` naming
20. `tests/integration/test_db_connection.py` — explain `information_schema`, `pg_isready`, when these tests run/skip
21. `tests/integration/test_job_repository_integration.py` — explain why these tests duplicate unit test scenarios (ARRAY, TIMESTAMPTZ), `time.sleep()` rationale for `updated_at` test
22. `tests/integration/test_migration.py` — explain `information_schema.tables`, `pg_indexes`, `alembic_version` table, how this mirrors production verification

### Batch 6 — Infrastructure
Infrastructure files are independent of Python concepts.

23. `Dockerfile` — explain slim image, layer cache order, `ENV PYTHONPATH`, `--no-cache-dir`, `CMD` vs `ENTRYPOINT`, production vs dev distinction
24. `docker-compose.yml` — explain restart policies, healthcheck mechanism, named volume vs bind mount, POSTGRES_* env vars, `depends_on.condition`

---

## New File: `docs/DAY1_LEARNING_GUIDE.md`

Create this file at `/Users/vishal/csv-metadata/docs/DAY1_LEARNING_GUIDE.md`.

### Required contents

**1. Overview section**
- What Day 1 built (the jobs table, the repository layer, the API foundation)
- The architecture diagram showing all components and their relationships
- How to navigate the codebase file-by-file

**2. Reading order**
Recommended reading order for a learner approaching the codebase fresh:
1. `docs/DECISIONS.md` — understand the "why" before the "how"
2. `backend/requirements.txt` — what tools are in play
3. `backend/db/connection.py` — the database plumbing
4. `backend/models/job.py` — the data model
5. `backend/db/job_repository.py` — database operations
6. `backend/main.py` — the API entry point
7. `alembic.ini` + `backend/alembic/env.py` — migration tooling
8. `backend/alembic/versions/0001_initial_jobs_table.py` — the actual migration
9. `tests/conftest.py` — how tests are set up
10. Unit tests → integration tests

**3. Key concept summaries** (each as a short section)
- SQLAlchemy mental model (Engine / Pool / Connection / Session analogy)
- FastAPI request lifecycle (full diagram: Browser → Uvicorn → ASGI → FastAPI → Middleware → Route → Depends → Repository)
- The `get_db()` generator dependency explained step by step
- The jobs state machine (diagram with all 5 states and transitions)
- The conditional UPDATE idempotency pattern explained with the parking spot analogy
- Why `expire_on_commit=False` and when `expire_all()` is needed
- Alembic's role: the difference between SQLAlchemy models and the real database schema
- Testing strategy: why SQLite for unit tests, why PostgreSQL for integration tests

**4. Interview question bank**
A section called "Interview questions this codebase teaches" with 15-20 questions and brief answers, covering:
- What is the SQLAlchemy Unit of Work?
- What is the N+1 query problem? (reference the identity map)
- What does `expire_on_commit=False` do and when would you set it to `True`?
- What is the difference between `flush()` and `commit()`?
- Why use `pool_size=1` in Lambda?
- What problem does `pool_pre_ping=True` solve?
- What is MVCC and how does it affect the conditional UPDATE race condition?
- Why is `TIMESTAMPTZ` preferred over `TIMESTAMP`?
- Why use a UUID primary key instead of a sequential integer?
- What does Alembic's `down_revision` field do?
- What is the difference between `server_default` and `default` in SQLAlchemy?
- What is `synchronize_session` and why does it matter after a bulk UPDATE?
- Why does the worker's `claim_job_for_processing` include `processing` in the WHERE clause?
- What is CORS and why does the browser enforce it?
- What is ASGI and how does FastAPI use it?
- What is the repository pattern and why is it used here?
- Why are `pending_upload` jobs excluded from the user-facing job list?

**5. How to run everything**
- Unit tests (with explanation)
- Integration tests (with prerequisites)
- Docker Compose setup
- Running Alembic migrations
- Starting the API locally

**6. Day 1 → Day 2 bridge**
- What Day 1 built (foundation)
- What Day 2 will add (S3 presigned URLs, SQS, upload endpoints)
- How the existing `job_repository.py` functions will be called in Day 2

---

## New/Updated Steering Files

### `.kiro/steering/documentation-style.md` (create)

Create at `/Users/vishal/csv-metadata/.kiro/steering/documentation-style.md`.

Contents:
- This is a learning codebase — every AI-generated change must include educational comments
- Comment philosophy: explain WHY, not WHAT (avoid restating what the code says)
- Required module docstring structure (WHAT / WHY / ARCHITECTURE / ANALOGY / IMPORTANT)
- Required function docstring structure (What / Why / How / Inputs / Output / Side effects / Edge cases / Analogy where helpful / Interview connection where relevant)
- Comment style: use `#` for inline blocks, `"""` for module/function docstrings
- Forbidden comments: trivial comments like `# Create engine`, `# Return result`, `# Import library`
- Concept linking: when a concept is explained in one file, later files can reference it (e.g., "See `connection.py` for the Engine/Session explanation")
- Do NOT add documentation inside test assertions — assertions are self-documenting code

### `.kiro/steering/no-behavior-changes.md` (create)

Create at `/Users/vishal/csv-metadata/.kiro/steering/no-behavior-changes.md`.

Contents:
- This is an active learning codebase where tests are the source of truth
- Do NOT change: APIs, database schema, SQL queries, transaction behavior, dependency versions, test behavior
- Do NOT: refactor working code, rename functions/classes/files, introduce new abstractions, "clean up" working implementation
- After any change: `PYTHONPATH=. DATABASE_URL=sqlite:///:memory: .venv/bin/pytest tests/unit/ -v` must pass 53 tests
- When in doubt: add a comment, don't change the code

### `.kiro/steering/architecture.md` (create)

Create at `/Users/vishal/csv-metadata/.kiro/steering/architecture.md`.

Contents:
- Project name: CSVForge
- Architecture summary: FastAPI (Lambda via Mangum) + PostgreSQL (RDS) + S3 + SQS + React frontend
- Key design decisions reference: `docs/DECISIONS.md`
- Python package layout: `backend/` (API + shared DB), `worker/` (Lambda worker), `tests/`
- Database layer: SQLAlchemy 2.0 ORM, psycopg 3, PostgreSQL 16
- Test strategy: SQLite in-memory for unit tests, real PostgreSQL for integration tests
- Environment detection: `ENVIRONMENT=local` → `DATABASE_URL` env var; `ENVIRONMENT=production` → Secrets Manager
- Current status: Day 1 complete (53/53 unit tests passing)
- AD references: AD-01 through AD-30 in `docs/DECISIONS.md`

---

## Implementation Notes for the Coding Agent

1. **Do not change any executable code.** Only add/modify strings (docstrings, comments). The only exception is the three empty `__init__.py` files and `worker/__init__.py`, where a module docstring is the entire content.

2. **Verify after each batch.** Run `PYTHONPATH=. DATABASE_URL=sqlite:///:memory: .venv/bin/pytest tests/unit/ -v` after completing each batch before moving to the next.

3. **For `.py` files**: add module docstrings as the very first string in the file (before imports if there isn't one already, or replace/extend the existing one). Add function docstrings immediately after the `def` line. Add inline `#` comments immediately before the relevant line or block.

4. **For `.txt`, `.ini`, `.yml`, `Dockerfile` files**: add comments using the file's native comment syntax (`#` for all of these).

5. **For `requirements.txt` files**: add a `#` comment above each package explaining what it does and why it's here.

6. **For `docs/DAY1_LEARNING_GUIDE.md` and steering files**: create these as new files. They do not affect the Python import system or test behavior.

7. **The `alembic.ini` comment style**: uses `#` — do not use `;` (which is also valid INI comment syntax but inconsistent with the existing file).

8. **`worker/__init__.py` is currently an empty file** (not missing — it exists on disk but has no content). Same for `backend/__init__.py`, `backend/db/__init__.py`, `backend/models/__init__.py`. Adding a docstring to an empty `__init__.py` does not change behavior.

9. **Preserve existing good documentation.** Files like `job_repository.py` and `connection.py` already have solid docstrings. The task is to augment them with the specific missing concepts listed above, not to rewrite what is already good.

10. **Test name note for `test_second_claim_returns_false`** in `test_job_repository_unit.py`: Do NOT rename this test. The test name is misleading (the second claim actually returns True), but renaming would change test output and potentially confuse the learner who is already used to the name. Instead, add a comment explaining the intentional naming oddity.

---

## Verification Command

```bash
PYTHONPATH=. DATABASE_URL=sqlite:///:memory: .venv/bin/pytest tests/unit/ -v
```

Expected output: `53 passed`

For integration tests (requires Docker):
```bash
docker compose up -d
alembic upgrade head
PYTHONPATH=. .venv/bin/pytest tests/integration/ -v
```
