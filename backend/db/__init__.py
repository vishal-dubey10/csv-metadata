"""
CSVForge — database sub-package.

WHAT:
    This file marks the `backend/db/` directory as a Python package so that
    its modules can be imported as `backend.db.connection` and
    `backend.db.job_repository`.

WHY:
    The `db/` sub-package groups every module that touches the database layer:
    the connection / session plumbing (`connection.py`) and the data-access
    operations (`job_repository.py`).  Keeping these in a dedicated sub-package
    makes the architectural boundaries visible at a glance.

ARCHITECTURE:
    Consumers of this sub-package:

        backend/main.py
            imports get_db from backend.db.connection   (FastAPI dependency)

        worker/ (Day 3+)
            imports SessionLocal from backend.db.connection
            imports claim_job_for_processing, complete_job, fail_job
                from backend.db.job_repository

        tests/conftest.py
            imports get_db, SessionLocal, Base, engine
                from backend.db.connection

IMPORTANT CONCEPTS:
    - Keeping database access objects (session, engine) separate from models
      (ORM classes) prevents circular imports: `connection.py` defines `Base`,
      `models/job.py` imports `Base` from `connection.py`, and `job_repository.py`
      imports both — a clean one-way dependency graph.
    - This file is intentionally empty of executable code.
"""
