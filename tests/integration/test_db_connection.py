"""
Integration test: database connectivity.

Requires Docker PostgreSQL to be running.
Skipped automatically by the pg_engine fixture if the DB is unreachable.

What this tests:
- The engine can open a connection to PostgreSQL
- A simple query executes and returns the expected result
- The connection URL format (postgresql+psycopg) works with psycopg 3

SKIP LOGIC:
These tests require a real PostgreSQL connection. The `pg_engine` fixture in
conftest.py checks whether the database is reachable. If DATABASE_URL is not
set or points to the SQLite fallback, pytest skips them with a clear message.
This means the unit test suite (SQLite) runs anywhere, while integration tests
require Docker.

INFORMATION_SCHEMA:
`information_schema` is a PostgreSQL standard schema that contains metadata
about the database: tables, columns, constraints, and indexes.
`information_schema.tables` lists every table in the database. Querying it is
how tests verify that migrations created the expected tables without
hardcoding SQL — it's the standard, cross-database-compatible way to inspect
schema structure.

`pg_isready` is a PostgreSQL command-line utility that checks whether the
server is accepting connections. It is used in `docker-compose.yml` as the
database healthcheck — the `csvforge_api` service won't start until
`pg_isready` returns success. Tests in this file verify similar connectivity
at the Python level.
"""

from sqlalchemy import text


def test_select_one(pg_db):
    """
    Basic connectivity check: SELECT 1 returns the integer 1.

    This is the canonical database health check. If this fails:
    - PostgreSQL is not running
    - The connection URL is wrong
    - The psycopg driver is not installed
    - Network/firewall issues (in production)
    """
    result = pg_db.execute(text("SELECT 1")).fetchone()
    assert result is not None
    assert result[0] == 1


def test_postgresql_version(pg_db):
    """
    Verify we're connected to PostgreSQL 16 (as specified in docker-compose.yml
    and VERSIONS.md).

    The version string looks like: "PostgreSQL 16.x on ..."
    """
    result = pg_db.execute(text("SELECT version()")).fetchone()
    version_string = result[0]
    assert "PostgreSQL 16" in version_string, (
        f"Expected PostgreSQL 16, got: {version_string}"
    )


def test_connection_uses_correct_database(pg_db):
    """
    Verify we're connected to the 'csvforge' database, not 'postgres' default.
    Using the wrong database means migrations and tables are in the wrong place.
    """
    result = pg_db.execute(text("SELECT current_database()")).fetchone()
    assert result[0] == "csvforge"


def test_connection_uses_correct_user(pg_db):
    """
    Verify the connection is authenticated as the 'csvforge' user.
    If this returns 'postgres', we're using the superuser — a security concern.
    """
    result = pg_db.execute(text("SELECT current_user")).fetchone()
    assert result[0] == "csvforge"
