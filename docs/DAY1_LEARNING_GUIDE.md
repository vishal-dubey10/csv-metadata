# CSVForge — Day 1 Learning Guide

This guide walks you through everything built on Day 1: what it is, why it was designed
this way, and how the pieces connect. It is written for someone who knows Python but
is learning SQLAlchemy, FastAPI, PostgreSQL patterns, and AWS serverless architecture.

---

## Section 1: What Day 1 Built

Day 1 laid the entire foundation that Days 2 and 3 build on:

- **The `jobs` table** — a 15-column PostgreSQL table that tracks every CSV upload
  from initial intent through worker completion
- **The repository layer** — `job_repository.py`, a set of plain Python functions that
  own all SQL for the `jobs` table
- **The API foundation** — a FastAPI application with CORS middleware and a health check
  endpoint, ready to accept real routes in Day 2
- **The test infrastructure** — 53 unit tests running against SQLite in-memory (no
  Docker needed), plus integration tests that verify PostgreSQL-specific behavior

### Full Architecture Diagram

```
┌───────────────────────────────────────────────────────────────────────────┐
│                        Browser (Day 3+)                                    │
│                React + React Admin frontend                                │
└────────────────────────────┬──────────────────────────────────────────────┘
                             │ HTTPS
                  ┌──────────▼──────────┐
                  │   API Gateway       │  (production path)
                  │   (AWS)             │
                  └──────────┬──────────┘
                             │ Lambda invocation
                  ┌──────────▼──────────────────────────────────────────────┐
                  │  FastAPI (Python)                                        │
                  │  backend/main.py                                         │
                  │                                                          │
                  │  Local dev:  uvicorn backend.main:app --reload           │
                  │  Production: Mangum wraps FastAPI → Lambda handler       │
                  └──────┬─────────────────────┬───────────────────────────┘
                         │                     │ boto3 (Day 2+)
                         │ SQLAlchemy ORM       │
              ┌──────────▼──────────┐  ┌───────▼─────────────────────────┐
              │  PostgreSQL 16      │  │  AWS S3                         │
              │  (RDS in prod,      │  │  uploads/{user}/{job}/          │
              │  Docker in dev)     │  │  processed/{user}/{job}/        │
              │                     │  └──────────────────────────────────┘
              │  jobs table          │           │ SQS message (Day 2+)
              │  (15 columns)        │  ┌────────▼──────────────────────┐
              └────────────────────┘  │  Worker Lambda (Day 2+)       │
                                       │  • Reads CSV from S3          │
                                       │  • Cleans / deduplicates      │
                                       │  • Writes result to S3        │
                                       │  • Calls job_repository.py    │
                                       └───────────────────────────────┘
```

### How to Navigate the Codebase

The codebase has a deliberate layered structure. Each layer only knows about the
layers below it:

```
main.py                   ← HTTP layer (knows about routes, FastAPI, CORS)
    └── job_repository.py ← Data access layer (knows about SQL, sessions)
            └── models/job.py    ← Data model (knows about column types)
                    └── db/connection.py ← Connection layer (knows about the DB URL)
```

The tests mirror this:
```
tests/unit/test_health.py           ← tests main.py routes
tests/unit/test_job_repository_unit.py ← tests job_repository.py functions
tests/unit/test_job_model.py        ← tests the Job ORM model itself
tests/integration/                  ← tests that need real PostgreSQL
```

---

## Section 2: Recommended Reading Order

Read the files in this order. Each file introduces concepts the next one depends on.

1. **`docs/DECISIONS.md`** — Start here. Read the "why" before the "how". These
   Architecture Decision Records explain every significant choice in the codebase.
   Understanding AD-02 (presigned uploads), AD-08 (conditional UPDATE), and AD-13
   (hiding pending_upload jobs) will make the code make sense immediately.

2. **`backend/requirements.txt`** — A five-minute scan tells you every tool in play:
   FastAPI, SQLAlchemy, psycopg, Alembic, Mangum. Each package has a comment
   explaining its role. Read this so later files don't introduce surprises.

3. **`backend/db/connection.py`** — The database plumbing. This module creates the
   SQLAlchemy Engine, connection pool, Session factory, and the `get_db()` FastAPI
   dependency. Read this before anything else in `db/` — every other module imports
   from it.

4. **`backend/models/job.py`** — The data model. Defines the `Job` ORM class (the 15
   columns of the `jobs` table) and `JobStatus` (the 5 states). Read this after
   `connection.py` because it imports `Base` from there.

5. **`backend/db/job_repository.py`** — The database operations. Every SELECT, INSERT,
   and UPDATE for the `jobs` table lives here. Read this after `models/job.py` —
   it imports and manipulates `Job` objects.

6. **`backend/main.py`** — The API entry point. Creates the FastAPI app, adds CORS
   middleware, registers the health check route. Read this last among the backend
   files — it imports everything else.

7. **`alembic.ini` + `backend/alembic/env.py`** — Migration tooling. `alembic.ini`
   is the configuration; `env.py` is the runtime script that connects Alembic to
   your SQLAlchemy models and PostgreSQL database. Read these together.

8. **`backend/alembic/versions/0001_initial_jobs_table.py`** — The actual migration
   that creates the `jobs` table. Read this after `models/job.py` so you can see
   the relationship between the ORM model definition and the DDL it generates.

9. **`tests/conftest.py`** — The test fixtures. Creates the SQLite in-memory engine,
   the test HTTP client, and the database session for unit tests. Read this before
   the test files so the fixtures make sense.

10. **`tests/unit/`** then **`tests/integration/`** — Tests in dependency order: model
    tests first (simplest), then repository tests, then API tests. Integration tests
    build on unit test patterns but run against real PostgreSQL.

---

## Section 3: Key Concept Summaries

### 3.1 SQLAlchemy Mental Model: Engine / Pool / Connection / Session

SQLAlchemy has four layers that are easy to confuse. The restaurant analogy makes them
concrete:

```
SQLAlchemy Layer      Restaurant Analogy
──────────────────    ──────────────────────────────────────────────────────
Engine                The restaurant management system — knows the kitchen
                      location (DB URL), manages all the wait staff (pool)
                      
Connection Pool       The set of tables on the floor, ready to seat guests.
                      Pre-created, waiting to be used.
                      
Connection            One physical table, currently occupied.
                      Maps to one actual TCP socket to PostgreSQL.
                      
Session               One customer's booking at one table.
                      Tracks their full order (identity map of loaded objects),
                      pending changes (new/modified/deleted ORM objects),
                      and ends with payment (commit) or cancellation (rollback).
```

**Key rules:**
- The **Engine** is long-lived — created once per process (or Lambda container) and
  shared across all requests.
- A **Connection** is borrowed from the pool for the duration of a transaction and
  returned afterward. The TCP socket is not closed — it stays open and ready for the
  next request.
- A **Session** is short-lived — one per request, opened in `get_db()`, closed after
  the route returns.
- One Session = one transaction = one "Unit of Work."

**Why this matters:** Creating a new Engine per request creates a new connection pool
per request, which means a new TCP connection to PostgreSQL per request. Opening a
PostgreSQL connection involves a TCP three-way handshake, TLS negotiation, and
authentication — easily 10–50 ms of overhead. A pool amortizes that cost across many
requests.

See `backend/db/connection.py` for the full implementation with all comments.

---

### 3.2 FastAPI Request Lifecycle

What actually happens when the browser makes a GET request to `/health`:

```
Browser
   │  HTTP GET /health
   ▼
Uvicorn (local) / API Gateway + Mangum (Lambda)
   │  ASGI protocol
   ▼
FastAPI framework
   │
   ├── CORS Middleware
   │     Reads Origin header from the request.
   │     On preflight (OPTIONS): returns Allow headers immediately.
   │     On real request: adds Access-Control-Allow-Origin to response.
   │
   ├── Route Matching
   │     Finds @app.get("/health") → health_check function.
   │
   ├── Dependency Resolution
   │     FastAPI sees Depends(get_db) in the function signature.
   │     Calls get_db() → runs up to yield → injects db into the function.
   │     (Health check doesn't use Depends(get_db) — this step is skipped.)
   │
   ├── Route Handler Executes
   │     health_check() runs and returns {"status": "ok", ...}
   │
   ├── Dependency Cleanup
   │     FastAPI resumes get_db() after the yield.
   │     db.commit() or db.rollback() runs, then db.close().
   │
   └── FastAPI serializes return value to JSON via Pydantic
         └── Uvicorn / Mangum sends HTTP 200 with JSON body to browser
```

**ASGI vs WSGI:** WSGI (Flask, Django) is synchronous — one thread per request.
ASGI (FastAPI, Starlette) is asynchronous — can handle many concurrent connections
with a single thread using Python's event loop. FastAPI supports both `async def`
and regular `def` route handlers.

**Mangum:** In production, there's no uvicorn. API Gateway receives the HTTP request,
converts it to a Lambda event (a JSON dict), and invokes the Lambda function. Mangum
translates that Lambda event back into an ASGI-compatible request, passes it to
FastAPI, and packages the FastAPI response back into the Lambda response format. The
FastAPI code itself doesn't change — only the transport layer differs.

---

### 3.3 `get_db()` Step by Step

`get_db()` is a FastAPI **generator dependency** — a function with `yield` that
provides a resource and then cleans it up after the route finishes.

```
Step 1: get_db() is called by FastAPI before the route handler runs.

Step 2: db = SessionLocal()
        Creates a new Session, checks out a connection from the pool.
        No SQL is sent yet.

Step 3: yield db
        Python pauses here. FastAPI injects `db` into the route handler.
        The route handler runs completely (including all repository calls).

Step 4: (if route returned normally) db.commit()
        Writes all pending INSERTs/UPDATEs/DELETEs to PostgreSQL permanently.
        This is the "happy path" — the "signing in ink" moment.

Step 4b: (if route raised an exception) db.rollback()
        Discards all pending writes. The database is left unchanged.
        The exception is re-raised so FastAPI can return an HTTP 500.

Step 5: db.close()  [always runs, success or failure, via `finally`]
        Returns the connection back to the pool.
        The TCP socket to PostgreSQL is NOT closed — it goes back to the pool,
        ready for the next request.
```

**Why `yield` instead of `return`?** A function with `return` can't run cleanup code
after the caller is done. A function with `yield` is a generator — Python pauses at
the `yield`, lets the caller run, then resumes. The `try/finally` ensures cleanup runs
even if the route raised an exception. This is a core Python pattern for
context-management without the `with` statement.

**Library analogy:** `get_db()` is like a library checkout desk. You pick up a session
(check out books). You do your work (read/modify data). On the way out, the desk either
checks the books back in (commit) or undoes your check-out (rollback). The desk is
always reset for the next patron (close).

---

### 3.4 Jobs State Machine

Every CSV upload is represented by one `Job` row. The `status` column tracks where
the job is in its lifecycle:

```
                    ┌──────────────────────┐
                    │   pending_upload     │  ← Job created in DB;
                    │   (internal only)    │    S3 upload not yet done
                    └──────────┬───────────┘
                               │
                    POST /uploads/{id}/complete
                    transition_to_queued()
                    WHERE status = 'pending_upload'
                               │
                    ┌──────────▼───────────┐
                    │       queued         │  ← First user-visible state;
                    │                      │    SQS message published
                    └──────────┬───────────┘
                               │
                    Worker Lambda receives SQS message
                    claim_job_for_processing()
                    WHERE status IN ('queued', 'processing')
                               │
                    ┌──────────▼───────────┐
                    │     processing       │  ← Worker is actively running
                    └──────────┬───────────┘
                               │
               ┌───────────────┴───────────────┐
               │                               │
    complete_job()                         fail_job()
               │                               │
    ┌──────────▼──────────┐      ┌─────────────▼────────────┐
    │      completed      │      │          failed           │
    │  (terminal state)   │      │      (terminal state)     │
    └─────────────────────┘      └──────────────────────────┘
```

**Terminal states:** `completed` and `failed` have no outgoing transitions. The worker
checks `status` before processing and does nothing if the job is already terminal.

**Why 5 states instead of a boolean?** A boolean `is_processed` can't distinguish
between "queued and waiting" and "actively being processed" — both would be `False`.
The state machine makes every condition explicit and makes invalid transitions
impossible. You can't accidentally move from `failed` to `processing` — there's no
code path for it.

**`pending_upload` is hidden from users:** `list_jobs_for_user()` excludes
`pending_upload` jobs. From the user's perspective, a job doesn't exist until the
file actually reaches S3. Showing a `pending_upload` job would present a record that
might never complete (the user closed the browser before uploading).

---

### 3.5 Conditional UPDATE Idempotency

The worker uses a conditional UPDATE to claim a job atomically. This is the core
concurrency pattern in CSVForge.

**The problem:** SQS delivers messages "at least once." The same job can arrive twice
if the Lambda timed out. Two Lambda invocations might race to process the same CSV.

**The naive (broken) approach — SELECT then UPDATE:**
```
Worker A: SELECT status FROM jobs WHERE id='X'  → 'queued'
Worker B: SELECT status FROM jobs WHERE id='X'  → 'queued'
Worker A: UPDATE jobs SET status='processing' WHERE id='X'
Worker B: UPDATE jobs SET status='processing' WHERE id='X'
```
Both workers see 'queued' before either updates. Both proceed. Data corruption.

**The correct approach — conditional UPDATE:**
```sql
UPDATE jobs
SET status = 'processing'
WHERE id = 'X'
AND status IN ('queued', 'processing')
```

The check (`status IN (...)`) and the set (`status = 'processing'`) happen in a
single SQL statement. The database's row-level locking ensures that only one UPDATE
can write the new status at a time.

**The parking spot analogy:**
Two drivers (workers) see an empty parking spot (a job in `queued` state). They both
start driving toward it. The spot has a chain (the database row lock). The first driver
to reach the chain pulls it down and parks. The second driver arrives a moment later,
sees the chain is already up (status is now `processing`), and drives away (rowcount=0,
returns False).

**`rowcount` is the proof:** `result.rowcount` tells you how many rows were actually
updated. If `rowcount == 1`, your WHERE clause matched — you own this job. If
`rowcount == 0`, another worker already changed the status — do nothing.

**Why include `processing` in the WHERE clause?** If a Lambda timed out mid-processing,
SQS re-delivers the message. The job is already in `processing`. Including `processing`
in the WHERE clause allows the retry to re-claim it. The prior Lambda is dead;
re-claiming is the correct behavior.

**PostgreSQL MVCC note:** MVCC (Multi-Version Concurrency Control) means PostgreSQL
keeps multiple versions of each row. When Worker A commits its UPDATE to
`status='processing'`, Worker B's concurrent UPDATE sees the committed version. If
Worker B's WHERE clause matches `processing` (because `processing` is in the IN list),
Worker B can still get `rowcount=1`. This is why a second layer of idempotency in the
worker (checking `processed_s3_key` before actually processing) is planned for Day 3.

See `backend/db/job_repository.py → claim_job_for_processing()` for the implementation.

---

### 3.6 `expire_on_commit=False` and `expire_all()`

This is one of the most confusing things about SQLAlchemy. Here's the full picture.

**What "expiry" means:** SQLAlchemy maintains an **identity map** — a dictionary
mapping `(Model, primary_key) → Python object`. When you load a `Job` object from the
database, it's cached in this map. If you load the same job twice, you get back the
same Python object (no extra SELECT).

"Expiring" an attribute marks it as stale. The next time you access `job.status`,
SQLAlchemy issues a fresh `SELECT` to reload it from the database.

**The default behavior (`expire_on_commit=True`):** After `db.commit()`, SQLAlchemy
expires every attribute of every loaded object. The next access will re-query the DB.
This is safe for long-lived sessions where data might have changed since you loaded it.

**The problem in Lambda + FastAPI:**
```
Step 1: route calls create_job() → returns a Job object
Step 2: get_db() calls db.commit() → all attributes expire
Step 3: FastAPI tries to serialize job.status to JSON
Step 4: job.status is expired → SQLAlchemy tries to issue a SELECT
Step 5: but the session is being closed → DetachedInstanceError
```

**The fix (`expire_on_commit=False`):** Attributes stay readable in Python memory
after commit. No re-query needed. FastAPI can serialize the object to JSON without
a database round-trip.

**The trade-off:** If you reused the same Session across multiple requests (which we
never do), attributes could show stale values. But since each request gets a fresh
Session and the Session is closed after one response, there's no "next request" that
could see stale data in this Session.

**When `expire_all()` IS needed:**
In `claim_job_for_processing()` and other state-transition functions, we use a bulk
UPDATE (`update(Job).where(...).values(...)`). This is a Core-level statement, not
an ORM object mutation. SQLAlchemy does not automatically update Python objects when
you do a bulk UPDATE (especially with `synchronize_session=False`).

So after the UPDATE, any `Job` objects already loaded in this Session still show the
old `status` in memory. `db.expire_all()` marks all cached objects as stale — the
next attribute access will re-query the database and get the current value.

```
Without expire_all():
  job = get_job_by_id(db, job_id)  → job.status == 'queued' (loaded into identity map)
  claim_job_for_processing(db, job_id)  → DB now has status='processing'
  job.status  → still 'queued'  ← STALE! expire_on_commit=False keeps old value

With expire_all():
  job = get_job_by_id(db, job_id)  → job.status == 'queued'
  claim_job_for_processing(db, job_id)  → DB now has status='processing'
  expire_all()  → job attributes marked stale
  job.status  → triggers fresh SELECT → returns 'processing'  ← CORRECT
```

See `backend/db/connection.py` for the `expire_on_commit=False` setting and
`backend/db/job_repository.py` for the `expire_all()` calls.

---

### 3.7 Alembic's Role

There's a gap between what your Python code thinks the database schema is and what
actually exists in PostgreSQL. Alembic bridges that gap.

```
Your SQLAlchemy models          Alembic migration files         Real PostgreSQL schema
(what the CODE thinks           (the bridge — describes         (what actually exists
the schema is)                  the changes needed)             in the database)
                          
class Job(Base):                def upgrade():                  jobs table:
  id: Mapped[str]          →      op.create_table('jobs',  →    id VARCHAR(36) PK
  status: Mapped[str]               sa.Column('id', ...),       status VARCHAR(32)
  created_at: Mapped[...]           sa.Column('status', ...)    created_at TIMESTAMPTZ
                                  )                             ...
```

**The `alembic_version` table:** Alembic creates one table in your database:
```sql
alembic_version
───────────────
version_num VARCHAR(32)  -- stores the latest applied revision ID
```

After running `alembic upgrade head`, `version_num = '0001'`. This is how Alembic
knows which migrations have been applied and which need to run.

**`alembic upgrade head`** does this:
1. Connects to PostgreSQL using the DATABASE_URL from `.env` (or Secrets Manager).
2. Reads `alembic_version` to find the current revision (or None if first run).
3. Walks the revision chain from current to `head` (latest).
4. Runs each `upgrade()` function in sequence.
5. Updates `alembic_version` after each step.

**`alembic downgrade -1`** reverses one step: runs the `downgrade()` function of the
current revision, then updates `alembic_version`.

**Why not `Base.metadata.create_all()`?** SQLAlchemy can create all tables from your
models with one call. The problem: it can't update an existing table. If you add a
column to a model, `create_all()` does nothing to the live database — it only creates
tables that don't exist yet. Alembic generates `ALTER TABLE` statements to update
existing schemas incrementally.

See `alembic.ini`, `backend/alembic/env.py`, and
`backend/alembic/versions/0001_initial_jobs_table.py`.

---

### 3.8 Testing Strategy

**Two test layers serve different purposes:**

| Layer | Database | Why this database |
|-------|----------|-------------------|
| Unit (`tests/unit/`) | SQLite in-memory | No Docker. No network. Fast (~1 second). Tests ORM logic, business rules, API endpoint contracts. |
| Integration (`tests/integration/`) | Real PostgreSQL (Docker) | Tests PostgreSQL-specific behavior: ARRAY columns, TIMESTAMPTZ, MVCC concurrency, and Alembic migration correctness. |

**Why SQLite for unit tests?**

SQLite is a serverless, in-process database engine that lives in a single file (or
in memory). No Docker, no TCP connection, no authentication. A test that creates a
Session, inserts rows, queries, and verifies results runs in milliseconds.

The trade-off: SQLite doesn't support all PostgreSQL features. The biggest one is
`ARRAY(TEXT)` — SQLite has no array type. This is why `column_names` is defined as
`ARRAY(TEXT).with_variant(TEXT, "sqlite")` — when running on SQLite, the column
becomes plain `TEXT`. Unit tests can still test that the value is stored and retrieved
correctly; they just can't test PostgreSQL-specific ARRAY operators.

**Why PostgreSQL for integration tests?**

Some behaviors only appear with real PostgreSQL:
- `ARRAY` column behavior (real array operators, `ANY()`, indexing)
- `TIMESTAMPTZ` — SQLite's date handling is different
- MVCC concurrency behavior (the `claim_job_for_processing` race condition)
- Alembic migration correctness — verifying the `alembic_version` table, indexes,
  and constraints were actually created as expected

**The test isolation strategy:**

Unit tests use `scope="session"` for the engine (created once across all tests) and
`scope="function"` for the database session (fresh per test). Each test function gets
a clean state because the session is rolled back after each test.

Integration tests use a transaction-rollback isolation pattern: each test runs inside
a transaction that is rolled back at the end, leaving the database clean without
needing to recreate the schema. See `tests/conftest.py`.

---

## Section 4: Interview Question Bank

These are real interview questions that this codebase teaches. After reading the code,
you should be able to answer all of them from memory.

---

### Q: What is the SQLAlchemy Unit of Work pattern?

**Short answer:** A Unit of Work groups related database operations into a single
transaction — all succeed together or all fail together.

**Deep answer:** SQLAlchemy's `Session` IS the Unit of Work. When you `db.add(job)`,
`db.flush()`, and later `db.commit()`, the Session tracks every object you've added,
modified, or deleted. At commit time, it writes them all to the database in the correct
dependency order (no foreign key violations). If any step fails, `db.rollback()` undoes
everything. This prevents the database from ending up in a partially-updated state —
a critical guarantee for a processing pipeline where a job record must always match
the actual state of the S3 file.

**Where it appears in this codebase:** `backend/db/connection.py` → `get_db()`,
`backend/db/job_repository.py` → `create_job()`

---

### Q: What is the N+1 query problem?

**Short answer:** Loading N parent objects and then issuing one query per object to
load their children — N+1 total queries instead of 1 or 2.

**Deep answer:** The identity map helps prevent N+1 in SQLAlchemy. If you load a `Job`
by primary key and then load it again in the same Session, SQLAlchemy returns the
cached object from the identity map instead of issuing a second SELECT. However, if
you load a list of Jobs and then access a relationship on each one (e.g.,
`job.processing_logs`), SQLAlchemy would issue one SELECT per job unless you use
eager loading (`joinedload`, `selectinload`). CSVForge doesn't have relationships in
Day 1, so N+1 is not yet relevant — but it's the first thing to audit when adding
relationships in Day 3.

**Where it appears in this codebase:** `backend/db/connection.py` → `SessionLocal`
(the identity map is configured here via `expire_on_commit=False`)

---

### Q: What does `expire_on_commit=False` do and when would you set it to `True`?

**Short answer:** Keeps ORM object attributes readable in Python memory after a commit,
preventing `DetachedInstanceError` when FastAPI serializes the response.

**Deep answer:** SQLAlchemy's default (`expire_on_commit=True`) marks all loaded
object attributes as stale after every commit. The next attribute access issues a fresh
SELECT. In a web framework like FastAPI, the response serialization happens after
`db.commit()` and before `db.close()`. With the default setting, the serialization
triggers a SELECT on an already-closing session — causing `DetachedInstanceError`.
Setting `expire_on_commit=False` keeps in-memory values valid. You would set it back
to `True` in a long-lived session that spans multiple operations where you need
guaranteed-fresh data on every attribute access — for example, a background worker
that processes items in a loop and needs to see the latest DB state between iterations.

**Where it appears in this codebase:** `backend/db/connection.py` → `SessionLocal`

---

### Q: What is the difference between `flush()` and `commit()`?

**Short answer:** `flush()` writes to the database within the current transaction (not
yet visible to other connections); `commit()` makes the transaction permanent and
visible to everyone.

**Deep answer:** Think of a transaction like writing in pencil vs. signing in ink.
`flush()` is writing in pencil — the rows exist in the database (PostgreSQL has
executed the SQL), other queries within the same transaction can see them, but no other
database connection can see them and the changes can still be rolled back.
`commit()` is signing in ink — the transaction is committed, changes are durable and
visible to all. In `create_job()`, we call `flush()` before `refresh()` because we
need the row to exist in the database before we can SELECT it back to get
server-generated values (like `created_at`). The final `commit()` happens in
`get_db()`'s cleanup code, not inside the repository function.

**Where it appears in this codebase:** `backend/db/job_repository.py` → `create_job()`

---

### Q: Why use `pool_size=1` in Lambda?

**Short answer:** Each Lambda container handles exactly one request at a time — more
connections would be wasted and would hit RDS connection limits at scale.

**Deep answer:** Lambda scales by spinning up more containers, not by using threads
inside one container. One container = one concurrent request = one needed connection.
If `pool_size=5`, then 1000 concurrent Lambda invocations would try to hold 5000 open
connections simultaneously — hitting RDS's `max_connections` limit (typically 100–400
for `db.t3.micro`) and causing new requests to fail with "too many clients."
`pool_size=1` means 1000 concurrent invocations = 1000 open connections — predictable,
proportional to concurrency. For a long-running server with multiple workers (uvicorn
with 4 workers), `pool_size=5` would be appropriate.

**Where it appears in this codebase:** `backend/db/connection.py` → `engine`
(the `create_engine` call)

---

### Q: What problem does `pool_pre_ping=True` solve?

**Short answer:** Prevents "stale connection" errors in Lambda warm invocations where
the pooled TCP connection was dropped by a network appliance during the Lambda freeze.

**Deep answer:** A Lambda container can be frozen (no invocations) for minutes or hours.
During that freeze, AWS NAT gateways drop idle TCP connections after ~350 seconds, and
PostgreSQL may also close idle connections. The Python-side pool doesn't know this —
it still believes the connection is alive. On the next warm invocation, the pool hands
out the stale connection, the first SQL statement fails with a cryptic "broken pipe"
error, and the user sees a 500. `pool_pre_ping=True` sends a lightweight `SELECT 1`
probe before handing the connection to the request. If the probe fails, SQLAlchemy
discards the stale connection and opens a fresh one transparently. Cost: ~1 ms per
request for the probe — negligible.

**Where it appears in this codebase:** `backend/db/connection.py` → `engine`

---

### Q: What is MVCC and how does it affect the conditional UPDATE race condition?

**Short answer:** MVCC (Multi-Version Concurrency Control) lets PostgreSQL serve reads
without blocking writes by keeping multiple versions of each row. In a race between
two workers, both may match the conditional UPDATE's WHERE clause and both get
`rowcount=1`.

**Deep answer:** PostgreSQL never overwrites a row in place. Instead, it creates a new
version of the row with the new data (and marks the old version as superseded). Readers
see a consistent snapshot of the database as of the start of their transaction —
they don't see uncommitted writes from other transactions. When Worker A and Worker B
simultaneously execute `UPDATE jobs SET status='processing' WHERE status IN
('queued','processing')`, PostgreSQL serializes the two UPDATEs at the row level.
Worker A's UPDATE commits first. Worker B's UPDATE sees Worker A's committed version —
`status='processing'` — which still matches the WHERE clause. Worker B's UPDATE
succeeds too (rowcount=1). Both workers believe they own the job. This is why a second
idempotency guard (checking `processed_s3_key` before doing actual work) is planned for
Day 3.

**Where it appears in this codebase:** `backend/db/job_repository.py` →
`claim_job_for_processing()`

---

### Q: Why is `TIMESTAMPTZ` preferred over `TIMESTAMP`?

**Short answer:** `TIMESTAMPTZ` stores the instant in time as UTC internally and is
unambiguous regardless of server timezone; `TIMESTAMP` stores raw numbers with no
timezone context.

**Deep answer:** `TIMESTAMP` (without time zone) stores the literal date and time
numbers. If your server is in UTC and you store "2024-01-15 10:00:00", that record is
meaningless without knowing the timezone. If you move the database server to EST, all
your stored times now represent a different instant in real time. `TIMESTAMPTZ`
(TIMESTAMP WITH TIME ZONE) stores the moment as UTC internally and converts to the
session's timezone on read. Since CSVForge always uses UTC, `TIMESTAMPTZ` is always
correct regardless of where the server runs. This matters for multi-region deployments
where compute and data may be in different AWS regions.

**Where it appears in this codebase:** `backend/models/job.py` → `created_at`,
`updated_at`; `backend/alembic/versions/0001_initial_jobs_table.py`

---

### Q: Why use a UUID primary key instead of a sequential integer?

**Short answer:** UUIDs are non-enumerable (preventing ID guessing attacks),
globally unique (safe to merge across databases), and can be generated in Python
without a database round-trip.

**Deep answer:** Sequential integers (SERIAL, BIGSERIAL) expose information: if a user
finds job ID `1042`, they know jobs `1`, `2`, ... `1041` all exist and belong to
someone. They can enumerate resources. UUID v4 is randomly distributed —
knowing one UUID gives no information about others. The second benefit is
generation: we need the job ID immediately to construct the S3 key
(`uploads/{user_id}/{job_id}/original.csv`) in the same request, before the INSERT
happens. UUID can be generated in Python with `uuid.uuid4()` — no DB round-trip.
The downside: UUIDs are 36 bytes (text) vs. 4–8 bytes (integer), and random UUIDs
cause B-tree index page splits (new values scatter across the index rather than always
appending to the end).

**Where it appears in this codebase:** `backend/models/job.py` → `id` column;
`backend/db/job_repository.py` → `create_job()`

---

### Q: What does Alembic's `down_revision` field do?

**Short answer:** It forms the migration chain — each migration knows which migration
must have been applied before it, allowing Alembic to run them in the correct order.

**Deep answer:** Alembic migration files form a directed acyclic graph (DAG) — in
most projects, a simple linked list. Each file has a `revision` (its own ID) and a
`down_revision` (the ID of the migration that must precede it). `alembic upgrade head`
walks this chain from the current applied revision (stored in `alembic_version`) to
the latest, executing each `upgrade()` in sequence. `down_revision = None` means "I
am the first migration." When you add a second migration, you set its
`down_revision = "0001"` — Alembic then knows `0001` must be applied before `0002`.
This chain structure guarantees migrations are always applied in the same order across
all environments (local, staging, production).

**Where it appears in this codebase:**
`backend/alembic/versions/0001_initial_jobs_table.py`

---

### Q: What is the difference between `server_default` and `default` in SQLAlchemy?

**Short answer:** `server_default` is evaluated by the database at INSERT time;
`default` is evaluated by Python at Python object creation time.

**Deep answer:** `server_default=text("CURRENT_TIMESTAMP")` means SQLAlchemy does NOT
include the column in the INSERT statement — the database fills it in. The Python
object has `None` for that attribute until after `db.flush()` + `db.refresh()`, when
the DB-generated value is retrieved. `default=lambda: datetime.utcnow()` means Python
computes the value before sending SQL — the Python object has the value immediately,
before any DB round-trip. For timestamps that should reflect the database's system
clock exactly (especially across different servers in different timezones),
`server_default` is correct. For values like UUIDs that you want available in Python
immediately (before INSERT), `default` or explicit assignment in Python is correct.
CSVForge uses both: UUID is generated in Python (explicit assignment), timestamps use
`server_default`.

**Where it appears in this codebase:** `backend/models/job.py` → `created_at`,
`updated_at`, `id`

---

### Q: What is `synchronize_session` and why does it matter after a bulk UPDATE?

**Short answer:** It controls whether SQLAlchemy updates Python objects in memory
after a bulk UPDATE statement — since the bulk UPDATE doesn't go through the ORM object
mutation path.

**Deep answer:** When you call `update(Job).where(...).values(status='processing')`,
SQLAlchemy sends one SQL UPDATE statement that touches N rows. It does NOT iterate over
individual Python objects and mutate their attributes. The `synchronize_session`
option tells SQLAlchemy what to do with any matching objects already loaded in the
Session's identity map: `"evaluate"` — re-evaluate the WHERE clause in Python and
update matching objects (safe for simple equality conditions); `False` — don't update
Python objects at all (faster, but objects become stale). When using `False`,
you must call `db.expire_all()` afterward to ensure stale objects get refreshed on
next access. CSVForge uses `"evaluate"` for `transition_to_queued()` (simple equality
WHERE) and `False` for `claim_job_for_processing()` (complex IN clause).

**Where it appears in this codebase:** `backend/db/job_repository.py` →
`transition_to_queued()`, `claim_job_for_processing()`

---

### Q: Why does `claim_job_for_processing()` include `'processing'` in the WHERE clause?

**Short answer:** To allow a Lambda retry to re-claim a job that was already claimed
by a previous Lambda invocation that timed out mid-processing.

**Deep answer:** SQS delivers messages at-least-once. If a Lambda processes a job
for 14 minutes and then times out before completing, SQS re-delivers the message.
At that point, the job's status is `'processing'` (it was claimed by the dead Lambda).
If the WHERE clause only included `'queued'`, the retry Lambda would see
`status='processing'`, get `rowcount=0`, and give up — leaving the job stuck in
`'processing'` forever. By including `'processing'` in the WHERE clause, the retry
Lambda can re-claim the job and process it again. The second layer of idempotency
(ensuring the retry doesn't double-process an already-completed job) will be
implemented by checking `processed_s3_key is None` before doing actual CSV work —
that's Day 3.

**Where it appears in this codebase:** `backend/db/job_repository.py` →
`claim_job_for_processing()`

---

### Q: What is CORS and why does the browser enforce it?

**Short answer:** CORS (Cross-Origin Resource Sharing) is a browser security
mechanism that blocks JavaScript from reading responses from a different origin
unless that origin explicitly permits it.

**Deep answer:** The browser's Same-Origin Policy prevents a malicious website from
reading your bank balance by making a request to your bank's API from JavaScript —
because the response would go to the malicious website's JavaScript, not yours. CORS
is the mechanism that lets trusted origins opt in: the server adds an
`Access-Control-Allow-Origin` header to responses, and the browser allows the
JavaScript to read it. Without CORS middleware, every API call from the React frontend
(at `localhost:5173`) to the FastAPI backend (at `localhost:8000`) would be blocked by
the browser — even though the server returned a 200. The CORS error is enforced by the
browser, not the server. This is why you see CORS errors in the browser console, not
in server logs.

**Where it appears in this codebase:** `backend/main.py` → `CORSMiddleware`

---

### Q: What is ASGI and how does FastAPI use it?

**Short answer:** ASGI (Asynchronous Server Gateway Interface) is the protocol between
a Python async web framework and an async web server. FastAPI is an ASGI app;
uvicorn is the ASGI server.

**Deep answer:** WSGI (Flask, Django's default) is synchronous — the server calls your
app with a request, your app returns a response, one at a time per thread. To handle
1000 concurrent connections with WSGI you need 1000 threads (expensive). ASGI allows
a single thread to handle many concurrent connections via Python's event loop — your
app `await`s I/O operations, yielding control back to the event loop while waiting.
FastAPI is built on Starlette, which is an ASGI framework. Uvicorn runs the event loop
and speaks the ASGI protocol. In production, Mangum translates the Lambda invocation
event format into ASGI — so the same FastAPI app code works locally (uvicorn) and in
Lambda (Mangum), with no changes.

**Where it appears in this codebase:** `backend/main.py` (ASGI/WSGI explanation in
module docstring)

---

### Q: What is the repository pattern and why is it used here?

**Short answer:** The repository pattern centralizes all data access for one entity
behind a clean API of Python functions, so the rest of the codebase never writes SQL.

**Deep answer:** Without a repository, route handlers write SQL directly. When you
need to change the database (add a column, switch from PostgreSQL to DynamoDB, add
caching), you touch every route. With a repository, all SQL lives in one file. Routes
call `create_job(db, ...)` — they don't know or care whether that's a PostgreSQL
INSERT, a DynamoDB PutItem, or a cached operation. This is the Dependency Inversion
principle: high-level modules (routes) depend on abstractions (the repository
functions), not concrete implementations (SQL). Contrast with the Active Record
pattern (Django's `Job.objects.create(...)`) where the model carries its own
persistence methods — logic and storage are merged together.

**Where it appears in this codebase:** `backend/db/job_repository.py` (all functions)

---

### Q: Why are `pending_upload` jobs excluded from the user-facing job list?

**Short answer:** A `pending_upload` job represents an upload that may never complete —
showing it to the user would display a stuck or phantom record.

**Deep answer:** The upload flow is two steps: (1) the API creates a `Job` record in
`pending_upload` status and returns a presigned S3 URL; (2) the browser uploads to S3
and then calls `POST /uploads/{job_id}/complete`. Between steps 1 and 2, the job
exists in the database but the file has not been uploaded. If the user closes the
browser after step 1 but before step 2, the `pending_upload` row exists permanently —
but there's no file in S3 to process. Showing this row would present a job that
appears to be stuck in an ambiguous state. From the user's perspective, a job starts
when the upload completes. `list_jobs_for_user()` enforces this by filtering out
`status != 'pending_upload'`. The `pending_upload` rows are still useful for the
server (to associate the S3 upload with the correct user and filename) — they're just
not user-visible until the upload confirms.

**Where it appears in this codebase:** `backend/db/job_repository.py` →
`list_jobs_for_user()`; `backend/models/job.py` → `JobStatus` docstring

---

## Section 5: How to Run Everything

### Unit tests (no Docker required)

```bash
cd /Users/vishal/csv-metadata
PYTHONPATH=. .venv/bin/pytest tests/unit/ -v
```

Expected output: `53 passed` in about 1 second.

These tests use SQLite in-memory — no PostgreSQL, no Docker, no network.
They test: the Job ORM model structure, all job_repository.py functions, and the
`/health` API endpoint.

### Integration tests (requires Docker)

```bash
# Start PostgreSQL
docker compose up -d

# Wait for PostgreSQL to be ready, then run migrations
cd /Users/vishal/csv-metadata
.venv/bin/alembic upgrade head

# Run integration tests
PYTHONPATH=. .venv/bin/pytest tests/integration/ -v
```

These tests verify: ARRAY column behavior, TIMESTAMPTZ handling, MVCC concurrency,
Alembic migration correctness (indexes, constraints, `alembic_version` table).

### Start the full stack with Docker Compose

```bash
cd /Users/vishal/csv-metadata
docker compose up
```

This starts:
- PostgreSQL on port 5432
- FastAPI (uvicorn) on port 8000 with hot reload

Verify the API is running:
```bash
curl http://localhost:8000/health
# Expected: {"status":"ok","environment":"local"}
```

Or open http://localhost:8000/docs in a browser for the Swagger UI.

### Run Alembic migrations manually

```bash
cd /Users/vishal/csv-metadata

# Apply all pending migrations
.venv/bin/alembic upgrade head

# See what migrations would run (without applying them)
.venv/bin/alembic upgrade head --sql

# Check current migration state
.venv/bin/alembic current

# See migration history
.venv/bin/alembic history

# Rollback one migration
.venv/bin/alembic downgrade -1
```

Note: Alembic reads `DATABASE_URL` from `.env`. Make sure you have a `.env` file
before running migrations:
```bash
cp .env.example .env
# Edit .env to set DATABASE_URL=postgresql+psycopg://csvforge:csvforge@localhost:5432/csvforge
```

### Start the API locally (without Docker)

If you have PostgreSQL running elsewhere and just want uvicorn:
```bash
cd /Users/vishal/csv-metadata
PYTHONPATH=. .venv/bin/uvicorn backend.main:app --reload --port 8000
```

The `--reload` flag uses watchfiles to restart uvicorn when source files change.

### Verify everything is working

```bash
# Health check
curl http://localhost:8000/health

# OpenAPI docs (open in browser)
# http://localhost:8000/docs

# OpenAPI JSON spec
curl http://localhost:8000/openapi.json
```

---

## Section 6: Day 1 → Day 2 Bridge

### What Day 1 built (the stable foundation)

Day 1 built everything below the business logic layer:

- **The database schema** — the `jobs` table is complete with all 15 columns,
  3 indexes, and the correct types (ARRAY, TIMESTAMPTZ, UUID). The migration is
  applied and tested.
- **The repository layer** — every database operation the worker and upload flow
  will need is already implemented and tested:
  - `create_job()` — creates the initial job record
  - `transition_to_queued()` — marks a job ready for processing
  - `claim_job_for_processing()` — atomically claims a job for a worker
  - `complete_job()` — writes all processing results
  - `fail_job()` — records a permanent failure
  - `list_jobs_for_user()` — user-facing job list (excludes `pending_upload`)
  - `get_job_by_id_and_user()` — ownership-verified job lookup
  - `list_all_jobs()` / `count_all_jobs()` — admin pagination
- **The API skeleton** — FastAPI app with CORS, health check, and all the
  infrastructure (Mangum for Lambda, uvicorn for local) in place

### What Day 2 will add

Day 2 adds the upload flow and the worker skeleton:

1. **Upload intent endpoint** — `POST /uploads/intent`
   - Calls `create_job()` to create the database record
   - Calls S3 `generate_presigned_url()` to create a time-limited upload URL
   - Returns `{ job_id, upload_url }` to the browser

2. **Upload complete endpoint** — `POST /uploads/{job_id}/complete`
   - Verifies the file actually exists in S3 (S3 HeadObject)
   - Calls `transition_to_queued()` to move the job from `pending_upload` → `queued`
   - Publishes a message to SQS with the `job_id`

3. **Worker Lambda skeleton** — `worker/handler.py`
   - SQS event handler that reads the `job_id` from the message
   - Calls `claim_job_for_processing()` to atomically claim the job
   - Downloads the CSV from S3, runs cleaning (deduplication)
   - Calls `complete_job()` or `fail_job()` with the results

### Existing `job_repository.py` functions Day 2 will call

| Day 2 action | Repository function |
|-------------|-------------------|
| Create initial job record | `create_job()` |
| S3 upload confirmed → SQS | `transition_to_queued()` |
| Worker claims the job | `claim_job_for_processing()` |
| Worker finishes processing | `complete_job()` |
| Worker encounters error | `fail_job()` |

### Existing fields Day 2 will populate

These columns are `NULL` in Day 1 because they're filled by the worker:
- `processed_s3_key` — set by `complete_job()` after writing the cleaned CSV to S3
- `file_size_bytes`, `row_count`, `column_count`, `column_names` — set by `complete_job()`
- `duplicate_rows_removed` — set by `complete_job()` after deduplication
- `failure_reason` — set by `fail_job()` when the worker catches a permanent error

The state transitions the worker drives through the state machine:
```
pending_upload → queued              (transition_to_queued, Day 2 upload endpoint)
queued → processing                  (claim_job_for_processing, Day 2 worker)
processing → completed               (complete_job, Day 2 worker)
processing → failed                  (fail_job, Day 2 worker)
```

All the code to execute these transitions is already written and tested. Day 2
wires the HTTP endpoints and the Lambda handler to call functions that already work.
