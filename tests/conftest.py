"""
Shared pytest fixtures for CSVForge tests.

Fixtures defined here are available to all test modules automatically —
pytest discovers conftest.py files up the directory tree.

Fixture scopes:
  function (default): fresh instance per test — safest, slightly slower
  session: one instance for the entire test run — faster but shared state risk

Integration tests require Docker PostgreSQL to be running.
Unit tests use SQLite in-memory and have no external dependencies.

──────────────────────────────────────────────────────────────────────────────
MODULE-IMPORT-TIME ORDERING CONSTRAINT
──────────────────────────────────────────────────────────────────────────────
The `os.environ.setdefault("DATABASE_URL", ...)` call MUST happen before ANY
import from `backend.*`. Here is why:

Python executes module-level code when it FIRST imports a module. When you
write `from backend.db.connection import engine`, Python runs connection.py
top-to-bottom, which includes:

    engine = create_engine(os.environ['DATABASE_URL'])   ← module-level code

If DATABASE_URL is not set before this import, `create_engine` will pick up
whatever is in the real environment — which might be the production database.
The `os.environ.setdefault` call at the top of conftest.py guarantees that by
the time `connection.py` runs, DATABASE_URL is already set to the SQLite test
value.

Rule: Never re-order these lines. Environment variables first, imports second.

──────────────────────────────────────────────────────────────────────────────
FIXTURE SCOPE: "session" VS "function"
──────────────────────────────────────────────────────────────────────────────
Cost model for scope choices:

  scope="function" (default):
    - A new fixture instance is created for EVERY test function.
    - For the SQLite engine: object creation + create_all() runs per test.
    - Safest: complete isolation, no shared state between tests.
    - Slightly slower for large test suites (hundreds of tests).

  scope="session":
    - One fixture instance is created for the ENTIRE pytest run.
    - For the pg_engine fixture: one connection pool for all integration tests.
    - Much faster for large test suites.
    - Trade-off: tests must not leave the engine in a broken state. If one
      test corrupts shared state, later tests may fail unexpectedly.

For SQLite unit tests, the engine is session-scoped but each test's changes
are isolated via the `sqlite_db` fixture (which calls create_all/drop_all per
test). For PostgreSQL integration tests, isolation is achieved via per-test
transaction rollback (see pg_db fixture below).
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ── Set environment before importing app modules ──────────────────────────────
# connection.py calls _build_database_url() at import time.
# Setting ENVIRONMENT=local here ensures it reads DATABASE_URL, not Secrets Manager.
# The DATABASE_URL for unit tests points to SQLite in-memory.
# Integration tests override this with the real PostgreSQL URL.
#
# CRITICAL: This line must appear before any `from backend.*` import.
# See the module docstring for the full explanation of why import order matters.
os.environ.setdefault("ENVIRONMENT", "local")


# ── SQLite in-memory engine for unit tests ────────────────────────────────────
# SQLite is not PostgreSQL — it doesn't support ARRAY, TIMESTAMPTZ, or
# gen_random_uuid(). Unit tests that exercise PostgreSQL-specific features
# (column_names array, exact timestamp behaviour) belong in integration tests.
#
# check_same_thread=False — WHY THIS IS NEEDED:
#   SQLite's default behavior is to reject connections used across threads
#   (it has limited concurrency support). In a test environment, pytest may
#   run fixtures in a different thread than the tests themselves, or the ASGI
#   TestClient may use a separate thread for the ASGI event loop.
#   Setting check_same_thread=False disables this check.
#
#   This is safe in the test environment because SQLite is used only for
#   in-memory testing — no concurrent writes could corrupt data. In production,
#   we use PostgreSQL which handles concurrency correctly.
SQLITE_URL = "sqlite:///:memory:"

sqlite_engine = create_engine(
    SQLITE_URL,
    connect_args={"check_same_thread": False},
)

SqliteSession = sessionmaker(bind=sqlite_engine, expire_on_commit=False)


@pytest.fixture()
def sqlite_db():
    """
    SQLite in-memory database session for unit tests.

    Creates all tables before each test, yields the session,
    then drops all tables after the test completes.

    Each test gets a completely fresh, empty database.
    No cleanup needed in the test itself.

    Note: SQLite does not support PostgreSQL-specific types (ARRAY, TIMESTAMPTZ).
    Tests that require those types must use the `pg_db` integration fixture instead.
    """
    from backend.db.connection import Base
    # Import models so they register on Base.metadata before create_all()
    from backend.models.job import Job  # noqa: F401

    Base.metadata.create_all(bind=sqlite_engine)
    db = SqliteSession()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=sqlite_engine)


# ── PostgreSQL integration fixture ────────────────────────────────────────────
# Requires Docker PostgreSQL to be running.
# Skipped automatically if TEST_DATABASE_URL is not set.
#
# TRANSACTION ROLLBACK ISOLATION PATTERN:
# We use per-test transaction rollback to isolate tests. Here is what happens
# at the database level for each test:
#
#   Test start:
#     PostgreSQL: BEGIN TRANSACTION  (outer savepoint)
#                   │
#     db fixture yields the session
#                   │
#     Test function runs:
#       job_repository.create_job(db, ...)  → INSERT within transaction
#       job_repository.list_jobs(db, ...)   → SELECT within same transaction
#       assertions pass
#                   │
#     Test function ends
#                   │
#     db fixture ROLLBACK                  ← undoes all INSERTs/UPDATEs
#                   │
#   Next test starts with a clean database
#
# This means each integration test sees a clean database regardless of
# insertion order, without needing to DELETE rows between tests. The rollback
# never reaches the actual PostgreSQL storage — it's all in-memory within
# the transaction.

TEST_PG_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://csvforge:password@localhost:5432/csvforge",
)


@pytest.fixture(scope="session")
def pg_engine():
    """
    PostgreSQL engine shared across the entire integration test session.

    scope="session" means one engine for all integration tests — avoids
    the overhead of creating a new engine (and pool) per test.

    Skips all integration tests if PostgreSQL is not reachable.
    """
    from sqlalchemy import create_engine as _ce
    engine = _ce(TEST_PG_URL, pool_pre_ping=True, pool_size=1, max_overflow=0)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"PostgreSQL not available — skipping integration tests: {e}")
    yield engine
    engine.dispose()


@pytest.fixture()
def pg_db(pg_engine):
    """
    PostgreSQL session for integration tests, with per-test transaction rollback.

    How it works:
    1. Begin a transaction (SAVEPOINT).
    2. Yield the session to the test.
    3. Rollback the transaction — every INSERT/UPDATE from the test is undone.

    The database is always left in a clean state. No manual truncation needed.
    Each test sees the same initial state (empty jobs table after migration).

    See the module-level comment for a full diagram of what happens at the
    PostgreSQL level during each test.

    Requires `alembic upgrade head` to have been run before the test session.
    """
    from sqlalchemy.orm import Session
    connection = pg_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# ── FastAPI TestClient fixture ─────────────────────────────────────────────────

@pytest.fixture()
def client():
    """
    FastAPI TestClient for HTTP endpoint tests.

    TestClient runs the ASGI app in-process — no real HTTP server needed.

    WHY USE AS CONTEXT MANAGER:
    `with TestClient(app) as client:` triggers FastAPI's ASGI lifespan events.
    FastAPI's lifespan protocol fires a `startup` event before requests and a
    `shutdown` event after. Some apps set up database connections or background
    tasks in `startup`. Using TestClient as a context manager ensures these
    startup/shutdown hooks run — this is the correct pattern for testing real
    FastAPI apps. Without the `with` statement, any `@app.on_event("startup")`
    code would not run during tests.

    Used for: health endpoint tests, and eventually route tests.
    """
    from backend.main import app
    with TestClient(app) as c:
        yield c
