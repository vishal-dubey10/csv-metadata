"""
Alembic environment configuration — the bridge between SQLAlchemy models and migrations.

WHAT:
    This file is executed by Alembic every time you run a migration command
    (alembic upgrade head, alembic downgrade -1, alembic revision --autogenerate, etc.).
    It is the glue that connects your Python models to the Alembic migration engine.

WHY:
    Alembic needs to know two things to do its job:
      1. What does the *target* schema look like? (answer: Base.metadata — the SQLAlchemy model definitions)
      2. How do I connect to the database? (answer: DATABASE_URL from the environment)
    env.py provides both.

ARCHITECTURE:

    SQLAlchemy Models (backend/models/job.py)
           ↓  define columns, types, constraints
    Base.metadata  (backend/db/connection.py)
           ↓  schema registry — the "desired state" of the DB
    env.py  ← YOU ARE HERE
           ↓  tells Alembic about the schema and the connection
    Migration files (backend/alembic/versions/)
           ↓  SQL change instructions
    PostgreSQL schema
           ↓
    alembic_version table  (tracks which migrations have been applied)

WHY NOT Base.metadata.create_all()?
    SQLAlchemy's Base.metadata.create_all(engine) creates all tables from scratch
    if they don't exist. It sounds convenient for development, but it has a critical
    flaw: it cannot ALTER an existing table. If you add a column to your model and
    call create_all(), nothing happens — the column is not added to the live database.

    Alembic's migrations solve this:
    - Each migration file records an incremental change (add column, rename index, etc.)
    - Alembic tracks which migrations have been applied in the `alembic_version` table
    - Running `alembic upgrade head` replays only the un-applied migrations
    - Running `alembic downgrade -1` reverses the most recent migration

    ANALOGY:
      SQLAlchemy models = the architect's blueprint (desired end state)
      Alembic migrations = the construction history (every change, in order, since day 0)

      You can hand a new contractor only the blueprint and they won't know
      what already exists. But hand them the complete construction history and
      they can pick up exactly where the last contractor left off.

      create_all() is the blueprint. Alembic is the paper trail.

TWO MODES:
    Alembic runs migrations in one of two modes (see functions below):

    offline mode  (`alembic upgrade head --sql`)
        Generates SQL statements as text — no database connection required.
        Useful for reviewing what will be executed, or for DBAs who apply
        SQL manually in restricted production environments.

    online mode  (default, `alembic upgrade head`)
        Connects directly to the database and executes the migration.
        This is the normal development workflow.

CONCEPTS:
    - target_metadata: the SQLAlchemy MetaData object. Alembic diffs it against
      the live database schema during `alembic revision --autogenerate` to
      produce a migration file automatically.
    - pool.NullPool: used here instead of the app's connection pool. Alembic
      is a one-shot CLI tool, not a long-lived server. NullPool ensures
      connections are closed immediately after use.
    - Side-effect imports: the `from backend.models.job import Job` line looks
      unused but is essential — it registers the Job table on Base.metadata.
      Without it, Alembic sees an empty metadata and generates empty migrations.
"""

import sys
import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# ── Make project root importable ─────────────────────────────────────────────
# alembic.ini sets `prepend_sys_path = .` which adds the project root to
# Python's sys.path before this file runs. This is what makes
# `from backend...` imports work when you run `alembic` from the project root.
#
# Without prepend_sys_path, Python would look for a `backend` package starting
# from wherever alembic was invoked, and the import would fail.

# ── Load .env for local development ──────────────────────────────────────────
# When running `alembic upgrade head` locally, DATABASE_URL must be available.
# python-dotenv reads the .env file and injects its contents into os.environ
# before the rest of this file runs.
# In CI/CD and production, the env var is injected by the deployment system —
# load_dotenv() is a no-op when the var is already set.
from dotenv import load_dotenv
load_dotenv()

# ── Import app components ─────────────────────────────────────────────────────
# Import Base BEFORE importing models so the MetaData registry exists first.
# Base.metadata is the central schema registry — it is populated as each
# model class is defined (Python metaclass magic at import time).
from backend.db.connection import Base, DATABASE_URL

# ── Side-effect import: register Job on Base.metadata ───────────────────────
# This import looks unused — no code in this file calls `Job` directly.
# The `# noqa: F401` comment suppresses the "imported but unused" warning
# that linters like flake8 and ruff would raise.
#
# WHY is this import needed?
# Python model classes register themselves on Base.metadata at the moment their
# class body is executed (i.e., at import time). If this module is never imported,
# the Job table never appears in Base.metadata, and Alembic generates migrations
# that don't create any tables.
#
# RULE: Every model you want Alembic to track must be imported somewhere before
# `target_metadata = Base.metadata` is evaluated.
from backend.models.job import Job  # noqa: F401 — import for side effect

# ── Alembic Config ────────────────────────────────────────────────────────────
# context.config is Alembic's representation of alembic.ini.
# It exposes the settings from alembic.ini and can be modified at runtime
# (which is what we do below with sqlalchemy.url).
config = context.config

# Configure Python's standard logging system using the [loggers] / [handlers] /
# [formatters] sections in alembic.ini. This gives Alembic its own log output.
# To see the SQL that Alembic executes, set [logger_sqlalchemy] level = INFO.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── target_metadata: the schema Alembic diffs against ────────────────────────
# Base.metadata is the SQLAlchemy MetaData object — a registry of every Table
# object defined by every SQLAlchemy model that has been imported above.
#
# When you run `alembic revision --autogenerate -m "add column"`, Alembic:
#   1. Reads target_metadata to understand what the schema SHOULD look like
#   2. Inspects the live database to understand what it CURRENTLY looks like
#   3. Diffs the two and generates a migration file with the necessary changes
#
# When you run `alembic upgrade head`, target_metadata is not used — Alembic
# just executes the migration files. But it must still be set for the
# context.configure() call to work correctly.
target_metadata = Base.metadata

# ── Inject DATABASE_URL at runtime ───────────────────────────────────────────
# alembic.ini intentionally leaves sqlalchemy.url blank (hardcoding credentials
# in a config file would be a security risk).
# We inject the URL here from the environment variable so Alembic uses
# the same connection configuration as the rest of the application.
config.set_main_option("sqlalchemy.url", DATABASE_URL)


# ── Offline migrations ────────────────────────────────────────────────────────

def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode — generates SQL without a live DB connection.

    WHAT:
        Instead of executing SQL against a database, Alembic renders the migration
        operations as raw SQL text and writes them to stdout (or a file).

    WHY:
        Useful in two scenarios:
        1. Review: inspect exactly what SQL will be executed before applying it.
        2. Restricted environments: some DBAs will not allow application code
           to connect directly to production databases. They apply SQL manually.
           `alembic upgrade head --sql > migration.sql` generates the script.

    HOW:
        `literal_binds=True` means parameter values are embedded directly in the
        SQL text (e.g., WHERE status = 'pending_upload' instead of WHERE status = $1).
        This makes the generated SQL readable without needing to substitute params.

    ANALOGY:
        Offline mode is like printing turn-by-turn directions — you get
        the full route on paper before you start driving.
        Online mode is like using GPS navigation — it guides you step by
        step in real time, adapting as you go.

        Another way to think about it:
        Online mode = a plumber coming to your house to fix the pipes.
            They see the real situation and make the changes in real time.
        Offline mode = a plumber writing out the full work order without
            touching your pipes. The DBA (or another team) reviews the
            work order and applies it manually. Useful when the migration
            environment doesn't have direct database access, or when every
            SQL change must be reviewed before execution.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# ── Online migrations ─────────────────────────────────────────────────────────

def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode — connects to the database and applies changes.

    WHAT:
        Creates a real database connection, executes each pending migration's
        upgrade() function, and updates the alembic_version table to record
        which migrations have been applied.

    WHY pool.NullPool?
        The application (backend/db/connection.py) uses a connection pool
        (pool_size=1 for Lambda compatibility). Alembic, however, is a one-shot
        CLI tool: it runs, applies migrations, and exits. There is no benefit to
        pooling connections for a tool that runs for a few seconds.

        NullPool disables pooling entirely — each connection.connect() call
        creates a fresh connection, and it is closed immediately when the
        `with` block exits. This is the correct behavior for a CLI tool.

    HOW IT WORKS step by step:
        1. engine_from_config() reads the sqlalchemy.url we injected above
           and creates an Engine with NullPool.
        2. connectable.connect() opens a real TCP connection to PostgreSQL.
        3. context.configure() links the connection to Alembic's migration context.
        4. context.begin_transaction() starts a PostgreSQL transaction.
        5. context.run_migrations() executes each un-applied migration's upgrade().
        6. The transaction commits automatically at the end of the `with` block.
        7. If any migration raises an exception, the transaction is rolled back —
           the database is left in its pre-migration state.

    THE alembic_version TABLE:
        Alembic maintains a single-row table called `alembic_version` in your
        database. Its `version_num` column records the latest applied revision ID.
        After running the first migration, it contains "0001".
        `alembic upgrade head` checks this table to determine which migrations
        to run, running only the ones with a revision ID not yet recorded.
        `alembic downgrade -1` removes the latest revision from this table.

    INTERVIEW CONCEPT:
        Why wrap migrations in a single transaction?
        If a migration has two operations (create table, create index) and the
        second fails, the transaction rollback leaves the database in its original
        state. Without the transaction, the table would exist but the index would
        not — a partially applied migration that is difficult to recover from.
        PostgreSQL supports transactional DDL (unlike MySQL), making this safe.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # NullPool: no connection reuse — appropriate for a CLI tool, not a server.
        # See the docstring above for the full explanation.
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


# ── Entry point ───────────────────────────────────────────────────────────────
# Alembic calls this module as a script. When it does, context.is_offline_mode()
# returns True if --sql was passed on the command line, False otherwise.
# This is the final line that actually triggers migration execution.
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
