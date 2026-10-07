"""
CSVForge — models sub-package.

WHAT:
    This file marks the `backend/models/` directory as a Python package so
    that its modules can be imported as `backend.models.job`.

WHY:
    The `models/` sub-package contains the SQLAlchemy ORM class definitions —
    the Python representation of every database table.  Grouping them here
    separates "what the data looks like" (models) from "how we talk to the
    database" (db/) and "how we expose the API" (main.py).

ARCHITECTURE:
    Models sit at the base of the dependency graph:

        backend/db/connection.py  defines `Base`
               ↓ (imported by)
        backend/models/job.py     defines `Job(Base)`, `JobStatus`
               ↓ (imported by)
        backend/db/job_repository.py  uses `Job`, `JobStatus`
               ↓ (imported by)
        backend/main.py / worker  call repository functions

    Alembic's `env.py` also imports from this sub-package (via
    `from backend.models.job import Job`) so Alembic knows the full
    schema when generating or running migrations.

IMPORTANT CONCEPTS:
    - ORM models define the *Python-layer* schema.  The *database-layer*
      schema is managed separately by Alembic migration files.  These two
      must stay in sync; they diverge whenever a developer adds a column to
      the model without creating a corresponding migration.
    - This file is intentionally empty of executable code.
"""
