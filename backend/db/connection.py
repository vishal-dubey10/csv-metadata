"""
CSVForge — database connection and session management.

WHAT:
    This module is the single place responsible for:
      1. Resolving the DATABASE_URL (local vs production path)
      2. Creating the SQLAlchemy Engine
      3. Providing a Session factory (SessionLocal)
      4. Exposing the declarative Base that all ORM models inherit from
      5. Providing `get_db()` — a FastAPI dependency that yields a session

WHY:
    Routes and repositories should not create database connections manually.
    Centralising everything here means:
    - There is exactly one connection pool per process.
    - Session configuration (expire_on_commit, autoflush) is set in one place.
    - Tests can swap the engine without touching any route code.

ARCHITECTURE:
    FastAPI route handler
         ↓  (FastAPI sees Depends(get_db) and calls the generator)
    get_db()  ←─ this module
         ↓  (yields)
    SQLAlchemy Session
         ↓  (issues SQL via)
    Engine  →  Connection Pool  →  actual psycopg connection  →  PostgreSQL

    In Lambda (production):
        Module is loaded once per container.  The Engine and its connection
        pool persist across warm invocations.  Only `get_db()` is called
        per-request; it checks out a connection from the existing pool
        rather than creating a new one.

ANALOGY (mental model only — not a literal mapping):
    Engine     = the restaurant management system that knows all the tables
    Pool       = the collection of tables available for seating guests
    Connection = one physical table currently occupied by a customer
    Session    = one customer's order / meal transaction at that table

    Restaurant analogy (expanded):
        The Engine is the restaurant manager who knows where the kitchen is
        and manages the wait staff.  The Pool is the set of tables on the
        floor ready to seat guests.  When a new guest (request) arrives, the
        manager (Engine) finds a free table (Connection) from the floor (Pool)
        and opens a tab (Session).

        A Session is like one table booking for one meal:
          - It holds a reference to a single Connection (one table).
          - It tracks everything ordered during the meal (the identity map —
            which Python objects have been loaded from the DB).
          - It tracks pending changes (new items ordered, items modified,
            items cancelled — new/modified/deleted ORM objects).
          - At the end of the meal, you either pay the bill (commit — make
            all changes durable) or cancel the order (rollback — discard).
        One Session per request is the correct pattern.  A long-lived Session
        (one Session reused across many requests) would accumulate stale data
        from earlier requests and eventually cause consistency problems.

        When the meal is over, the table is bussed and reset (connection
        returned to the pool), ready for the next guest.

    Remember: this is only a mental model.  A real SQLAlchemy Session can
    span multiple round-trips to the database (multiple queries), whereas a
    restaurant table analogy implies a single sitting.

IMPORTANT CONCEPTS:
    - Engine is long-lived; Sessions are short-lived (one per request).
    - Connection pooling avoids the TCP handshake + auth overhead of a
      fresh PostgreSQL connection on every request.
    - The `yield` keyword in `get_db()` is what makes it a generator
      and allows FastAPI to run cleanup code (commit/rollback/close)
      after the route finishes — including on exceptions.
    - `expire_on_commit=False` keeps ORM attributes readable after a
      commit without issuing a second SELECT (important for Lambda response).
    - The local vs production credential path keeps secrets out of
      CloudFormation/Lambda environment variables.

Interview concept:
    "What is connection pooling and why does it matter?"
    Opening a new TCP connection to PostgreSQL involves a three-way handshake,
    SSL negotiation, and PostgreSQL authentication — easily 5-20 ms of overhead.
    Connection pooling keeps a set of already-authenticated connections open
    and reuses them, reducing per-request latency significantly.
"""

import json
import logging
import os
from collections.abc import Generator

import boto3
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# load_dotenv() reads key=value pairs from a `.env` file in the current
# directory and sets them as environment variables — but ONLY if the variable
# is not already set.  In Docker Compose the env vars are injected directly,
# so this is a no-op there.  In production Lambda there is no .env file at all;
# this call is harmless (it silently does nothing if the file doesn't exist).
# Its only job is to support the `cp .env.example .env` local dev workflow.
load_dotenv()

logger = logging.getLogger(__name__)


# ── URL resolution ────────────────────────────────────────────────────────────

def _build_database_url() -> str:
    """
    Resolve the PostgreSQL connection URL for the current environment.

    WHAT:
        Reads environment variables to construct a full SQLAlchemy database URL.
        There are two paths:
          - Local (ENVIRONMENT=local): DATABASE_URL is read directly.
          - Production (ENVIRONMENT=production): credentials are fetched from
            AWS Secrets Manager; host/port/name from separate env vars.

    WHY:
        Two separate paths exist to avoid storing the database password in:
        - CloudFormation templates (visible in the AWS Console)
        - Lambda environment variables (visible in the AWS Console)
        Secrets Manager provides encrypted storage with fine-grained IAM access.
        AD-16 and AD-24 document this decision.

    HOW (execution order):
        1. Read ENVIRONMENT env var (default: "production").
        2. If local: read DATABASE_URL directly and return it.
        3. If production: read DB_SECRET_ARN, DB_HOST, DB_PORT, DB_NAME.
        4. Call Secrets Manager GetSecretValue to get username + password.
        5. Assemble and return the full URL.

    OUTPUTS:
        A psycopg 3 dialect URL in the form:
            postgresql+psycopg://<user>:<password>@<host>:<port>/<dbname>

        The `postgresql+psycopg` prefix tells SQLAlchemy which driver to use:
        - `psycopg`  → psycopg 3 (this project — async-capable, faster)
        - `psycopg2` → the older psycopg 2 driver (still widely used)
        SQLAlchemy uses the prefix to select the correct DBAPI at import time.

    SIDE EFFECTS:
        In production, makes one HTTPS call to AWS Secrets Manager.
        This call happens at module import time (see `DATABASE_URL = ...`
        at the bottom of this section) — i.e., once per Lambda cold start.
        Subsequent warm invocations reuse the already-imported module.

    EDGE CASES:
        - ENVIRONMENT=local but DATABASE_URL not set → RuntimeError with a
          clear message (better than an obscure AttributeError later).
        - Missing DB_SECRET_ARN in production → KeyError from os.environ[].
          This is intentional: the application should crash loudly if
          required secrets are absent rather than silently continue.
    """
    environment = os.getenv("ENVIRONMENT", "production")

    if environment == "local":
        url = os.getenv("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "ENVIRONMENT=local but DATABASE_URL is not set. "
                "Copy .env.example to .env and fill in DATABASE_URL."
            )
        return url

    # ── Production path (Lambda + Secrets Manager) ────────────────────────────
    # The secret JSON contains only {"username": "...", "password": "..."}.
    # Host, port, and dbname come from separate Lambda env vars to avoid a
    # circular CloudFormation dependency: the RDS endpoint isn't known until
    # after the RDS instance is created, but the secret is created alongside
    # the stack.  Splitting them avoids the chicken-and-egg problem.  AD-24.
    secret_arn = os.environ["DB_SECRET_ARN"]
    host = os.environ["DB_HOST"]
    port = os.getenv("DB_PORT", "5432")
    dbname = os.getenv("DB_NAME", "csvforge")

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(response["SecretString"])

    username = secret["username"]
    password = secret["password"]

    return f"postgresql+psycopg://{username}:{password}@{host}:{port}/{dbname}"


# ── Engine ────────────────────────────────────────────────────────────────────
# The Engine is SQLAlchemy's long-lived database coordinator.
# Think of it as the restaurant management system in the analogy:
# it knows every detail about how to connect to PostgreSQL and manages
# a pool of reusable connections.
#
# Restaurant analogy (expanded):
#   The Engine is like a restaurant manager who knows where the kitchen is
#   and manages the wait staff (connections).  When a waiter (request) needs
#   food (data), they don't hire a new cook each time — they ask the manager
#   for an available cook from the existing pool.  The manager keeps a list
#   of available cooks (idle connections) and busy cooks (in-use connections),
#   hands one out, and takes one back when the meal is served.
#
# We call _build_database_url() here, at module load time, so the URL is
# resolved exactly ONCE per process (or Lambda container).  In a Lambda warm
# invocation, the module is already in memory; _build_database_url() and
# create_engine() do NOT run again — the existing engine is reused.
#
# Creating a new Engine per request would create a NEW connection pool per
# request, defeating the entire point of pooling.
#
# Connection Pool — why pre-creating connections matters:
#   Opening a new TCP connection to PostgreSQL involves multiple round-trips:
#     1. TCP three-way handshake (SYN / SYN-ACK / ACK)
#     2. TLS/SSL negotiation (if using SSL — required on RDS)
#     3. PostgreSQL authentication (username, password, startup message)
#   This overhead can be 10–50 ms per connection.  For an API that handles
#   hundreds of requests per second, recreating the connection on every
#   request would add measurable latency and waste database resources.
#   A connection pool pre-creates N connections at startup and reuses them —
#   a request borrows a connection, runs its queries, and returns it to the
#   pool (no teardown, no reconnect overhead).
#
# Why pool_size=1 for Lambda specifically:
#   Each Lambda *container* handles exactly ONE request at a time.  There
#   is no parallelism inside a single Lambda container, so one connection
#   per container is sufficient.  AWS Lambda scales by spinning up MORE
#   containers, not by using threads inside one container.
#   Math: at 500 concurrent Lambda invocations, you have:
#     500 containers × pool_size=1 = 500 open PostgreSQL connections
#   PostgreSQL RDS typically has max_connections=100–400.  Keeping pool_size=1
#   makes the connection count predictable and proportional to concurrency.
#   If pool_size were 5, 500 concurrent invocations would open 2500 connections
#   — easily hitting the RDS connection limit and causing new requests to fail.

DATABASE_URL: str = _build_database_url()

# Pool configuration varies by database dialect.
# SQLite (used in unit tests) does not support pool_size or max_overflow;
# passing those kwargs raises an error.  We detect the dialect from the URL.
_is_postgres = DATABASE_URL.startswith("postgresql")

if _is_postgres:
    engine = create_engine(
        DATABASE_URL,

        # pool_pre_ping=True — solving the stale connection / Lambda warm
        # invocation problem:
        #
        # What is a stale connection?
        #   A stale connection is a TCP socket that appears open on the Python
        #   (client) side but has actually been closed on the server side or
        #   by a network appliance in between.  Neither party sent a FIN packet
        #   to notify the other — the socket just silently died.
        #
        # How this happens in Lambda:
        #   1. Lambda container is first created (cold start).
        #      The connection pool opens one connection to PostgreSQL (pool_size=1).
        #      The connection is established: TCP handshake + TLS + auth complete.
        #   2. The container handles a request and is then FROZEN (idle).
        #      Python code is paused; the container is not terminated.
        #   3. AWS NAT gateways drop TCP connections that are idle for ~350 seconds.
        #      PostgreSQL itself may also close idle connections after its own
        #      `idle_in_transaction_session_timeout` or `tcp_keepalives_idle` fires.
        #   4. The container gets a new request (warm invocation, 5+ minutes later).
        #      The pool still believes it has a valid connection from step 1.
        #   5. Without pool_pre_ping: SQLAlchemy hands out the stale connection.
        #      The first SQL statement fails with a cryptic "connection closed"
        #      or "broken pipe" error.  The user sees a 500 Internal Server Error.
        #   6. With pool_pre_ping=True: before handing the connection to the
        #      request, SQLAlchemy sends a lightweight `SELECT 1` probe.
        #      - If PostgreSQL responds: the connection is alive, proceed normally.
        #      - If the probe fails: SQLAlchemy discards the stale connection,
        #        opens a fresh one transparently, and the request succeeds.
        #
        # Cost: one extra round-trip (`SELECT 1`) per request.  This is ~1 ms on a
        # VPC-internal connection — negligible compared to actual query time.
        #
        # Interview concept:
        #   "Stale connection" — a socket that appears open on the client side
        #   but has been closed on the server or by a network appliance.
        pool_pre_ping=True,

        # pool_size=1: how many connections to keep open in the pool at once.
        #
        # Lambda runs ONE request at a time per container.  There is no benefit
        # to holding more than one connection per container.  More importantly,
        # AWS Lambda scales horizontally: at 1000 concurrent invocations you'd
        # have 1000 containers × pool_size connections open simultaneously.
        # PostgreSQL has a finite max_connections (typically 100–200 on RDS).
        # pool_size=1 ensures the total open connections equals the number of
        # warm Lambda containers, which is bounded and predictable.
        #
        # For a long-running server (e.g., uvicorn with 4 workers), a larger
        # pool (e.g., pool_size=5) would be appropriate.
        pool_size=1,

        # max_overflow=0: disallow creating extra connections beyond pool_size
        # even under a burst of concurrent requests to the same container.
        # (In Lambda, there is at most one concurrent request per container,
        # so max_overflow=0 is safe and keeps the connection count deterministic.)
        max_overflow=0,
    )
else:
    # SQLite is used in unit tests (ENVIRONMENT=local with a sqlite:// URL,
    # or when pytest sets DATABASE_URL=sqlite:///:memory:).
    # SQLite does not support pool_size/max_overflow — use default settings.
    engine = create_engine(DATABASE_URL)


# ── Session factory ───────────────────────────────────────────────────────────
# `sessionmaker` is a "factory factory" — it returns a *class* (SessionLocal)
# that you call with no arguments to get a *Session instance*.
#
# Factory pattern breakdown:
#   `sessionmaker(bind=engine, expire_on_commit=False)`
#       → This is the "factory configurator" (configures the factory)
#   `SessionLocal`
#       → This is the "factory" (a callable class that creates Sessions)
#   `SessionLocal()`
#       → This is one "product" (one Session instance, ready to use)
#
# Think of it like a coffee machine factory:
#   `sessionmaker(...)` is the machine blueprint configured with your
#   preferences (which database, which settings).
#   `SessionLocal` is the configured machine sitting on the counter.
#   `SessionLocal()` is one cup of coffee — a fresh session for this request.
#
# Why not just call `Session(bind=engine, ...)` directly each time?
#   You could, but you'd have to repeat all the configuration arguments every
#   time you create a session.  `sessionmaker` lets you configure once and
#   produce consistently-configured Session instances on demand.
#
# Think of it like a pre-configured mold for stamping out Session objects:
# every Session produced by SessionLocal() has the same bind, expire_on_commit,
# autoflush, and autocommit settings baked in.
#
# Usage:
#   db = SessionLocal()   # creates one Session bound to `engine`
#   # ... use db ...
#   db.close()
#
# In practice, `get_db()` below wraps this in a try/finally so you don't
# call SessionLocal() directly in route handlers.
SessionLocal = sessionmaker(
    bind=engine,  # which Engine (and therefore which database) to use

    # expire_on_commit=False:
    #   SQLAlchemy's default behaviour after db.commit() is to "expire"
    #   every attribute of every ORM object that was in the session.
    #   "Expiring" means SQLAlchemy marks the attribute as stale.  The next
    #   time you access `job.status`, SQLAlchemy issues a SELECT to reload it
    #   from the database (the "lazy load on attribute access" pattern).
    #
    #   WHY this default exists (expire_on_commit=True by default):
    #     In a long-lived session that spans multiple operations, you WANT
    #     fresh data after a commit.  If another process updated the row
    #     between your commit and the next read, expiry ensures you get the
    #     latest value from the DB.  This is the safe default.
    #
    #   WHY we set expire_on_commit=False:
    #     In Lambda + FastAPI, the sequence after a commit is:
    #       1. Route handler commits: db.commit()
    #       2. FastAPI serializes the return value (e.g., job.status → JSON)
    #       3. get_db() finally block runs: db.close()
    #
    #     If expire_on_commit=True (the default):
    #       After step 1, every attribute on `job` is marked stale.
    #       Step 2 accesses `job.status` → SQLAlchemy tries to issue a SELECT
    #       → but the session may already be closed or mid-close → raises
    #       DetachedInstanceError: "Instance <Job> is not bound to a Session".
    #
    #     With expire_on_commit=False:
    #       After commit, ORM objects retain their in-memory values.
    #       Step 2 reads `job.status` from Python memory — no DB round-trip.
    #       No DetachedInstanceError.
    #
    #   The trade-off we accept:
    #     If the same Session were reused across multiple operations (which we
    #     never do — one Session per request), the objects could be stale.
    #     But since each request gets a fresh Session and closes it after one
    #     response, there is no "next access that could see stale data."
    #
    #   See `expire_all()` in `job_repository.py` for how we handle state
    #   transitions where we DO need fresh data within the same session.
    #
    #   Interview concept:
    #     "Identity map" — SQLAlchemy's Session keeps a dictionary of
    #     (class, primary_key) → object.  Expiry marks entries as stale;
    #     expire_on_commit=False keeps them fresh after commit.
    expire_on_commit=False,

    # autoflush=True (default):
    #   Before executing any query, SQLAlchemy automatically flushes pending
    #   ORM changes to the database (within the current transaction).
    #   This ensures that if you add an object and then immediately query for
    #   it, the query sees the newly added row (because the INSERT was flushed
    #   before the SELECT, even without an explicit db.flush() call).
    #
    # autocommit=False (default):
    #   Every session is wrapped in a transaction.  Nothing is written to the
    #   database permanently until db.commit() is called.  If the session is
    #   closed without committing, the transaction is rolled back automatically.
)


# ── Declarative Base ──────────────────────────────────────────────────────────
# DeclarativeBase is SQLAlchemy's registry for ORM models.
# When a class inherits from Base (e.g., `class Job(Base)`), SQLAlchemy reads
# its type annotations (`Mapped[str]`, etc.) and `mapped_column()` calls to:
#   1. Construct a `Table` object describing the SQL table structure.
#   2. Register that Table in `Base.metadata`.
#   3. Wire up the Python attribute (e.g., `job.status`) to the SQL column.
#
# `Base.metadata` is the collection of ALL these Table objects — effectively
# a complete schema registry in Python memory.  Alembic's `env.py` imports
# this Base and uses `Base.metadata` as `target_metadata`, which tells Alembic:
# "this is what the schema SHOULD look like."  Alembic then compares it against
# the live database schema and generates an ALTER TABLE / CREATE TABLE migration
# to make the database match the Python models.
#
# There must be exactly one Base per application (or at least per Alembic
# configuration).  All models import THIS Base from this module so Alembic
# discovers them all in one place.
#
# Without inheriting from Base:
#   A plain Python class `class Job:` would have no table mapping.
#   `db.add(job)` would raise an error; Alembic would have no idea the
#   `jobs` table should exist.
class Base(DeclarativeBase):
    pass


# ── FastAPI dependency ────────────────────────────────────────────────────────

def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields one database session per request.

    WHAT:
        Opens a SQLAlchemy Session, yields it to the route handler, then
        commits (on success) or rolls back (on exception), and always closes
        the session when done.

    WHY:
        Route handlers need database access.  FastAPI's dependency injection
        system (Depends) provides a clean way to supply shared resources (like
        a DB session) without routes creating them manually.  Using a generator
        dependency (`yield`) lets us run cleanup code AFTER the route returns —
        crucial for committing or rolling back the transaction.

    HOW (execution order):
        1. `db = SessionLocal()` — check out a connection from the pool and
           open a new Unit-of-Work transaction.
        2. `yield db` — pause here and pass `db` to the route handler.
           The route handler runs completely (including calling repository
           functions that may add/update/delete ORM objects).
        3. If the route returned normally: `db.commit()` — write all pending
           changes to PostgreSQL permanently.
        4. If the route raised an exception: `db.rollback()` — discard all
           pending changes.  The `raise` re-raises the exception so FastAPI
           can return a 500 (or whatever the exception maps to).
        5. `db.close()` — return the underlying connection to the pool.
           Always runs, whether commit or rollback.

    FastAPI Request Lifecycle with get_db():

        Browser ──HTTP──► Uvicorn ──ASGI──► FastAPI
                                                │
                                          Route Handler
                                                │
                                    Depends(get_db) called
                                                │
                                          get_db() starts
                                                │
                                       db = SessionLocal()   ← Session created
                                                │
                                           yield db          ← Session injected into handler
                                                │
                                       [Handler runs]        ← DB operations happen here
                                                │
                                     db.commit() or          ← Changes persisted (or discarded)
                                     db.rollback()
                                                │
                                          db.close()         ← Session returned to pool
                                       (finally block)

    The `yield` keyword (vs `return`) is what makes get_db() a generator-based
    context manager.  FastAPI calls `get_db()`, executes the code up to `yield`,
    injects the yielded value into the route handler, runs the handler completely,
    then RESUMES `get_db()` after the `yield` (in the `finally` block) to commit
    or roll back and close the session.  This guarantees the session is always
    closed — even if the handler raises an exception, the `finally` block runs.

    ANALOGY:
        Think of get_db() as a checkout desk at a library:
        - You pick up a session (check out books).
        - You do your work (read/modify data).
        - On the way out, you either check the books back in properly
          (commit) or the system undoes your check-outs (rollback).
        - The desk is always reset for the next patron (close).

    USAGE in a route:
        @app.get("/example")
        def example(db: Session = Depends(get_db)):
            return some_repository_function(db)

    IMPORTANT — the generator pattern:
        A Python function that contains `yield` is a generator function.
        Calling it returns a generator object; Python does not execute
        the body until the generator is iterated.  FastAPI calls next()
        on the generator to get the yielded value, injects it into the
        route, and then calls next() again (reaching the end of the
        generator) after the route finishes.  The try/finally ensures
        the cleanup block runs even if the route raised an exception.

    SIDE EFFECTS:
        - Opens a database connection (or reuses a pooled one).
        - Commits or rolls back a transaction.
        - Returns the connection to the pool.

    EDGE CASES:
        - If the route raises an exception, rollback() is called and the
          exception propagates.  FastAPI converts unhandled exceptions to
          HTTP 500 responses.
        - If commit() itself fails (e.g., constraint violation not caught by
          the route), rollback() is called and the error propagates.
        - If the database is unreachable: SessionLocal() will either raise
          immediately or pool_pre_ping will detect the dead connection and
          attempt a reconnect.  If reconnection fails, the error propagates
          as an HTTP 500.

    Interview concept:
        "What is the Unit of Work pattern?"
        A Unit of Work groups a set of related database operations (reads
        and writes) into a single transaction that either all succeed
        (commit) or all fail together (rollback).  SQLAlchemy's Session IS
        the Unit of Work: it tracks every object you add/modify and writes
        them all at commit time in the correct order.
    """
    db = SessionLocal()
    try:
        yield db
        # If the route returned normally (no exception), commit all pending
        # writes.  This is the "happy path" — everything worked, make it durable.
        db.commit()
    except Exception:
        # Something went wrong in the route handler or in commit() itself.
        # Roll back every write that happened during this request so the
        # database remains consistent.  Then re-raise so FastAPI can return
        # an appropriate error response.
        db.rollback()
        raise
    finally:
        # Always close the session.  This returns the underlying PostgreSQL
        # connection to the connection pool so it can be reused by the next
        # request.  "Close" does NOT close the TCP connection to PostgreSQL;
        # it just releases the Session object and marks the connection as
        # available in the pool.
        db.close()
