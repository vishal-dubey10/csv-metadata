"""
CSVForge — backend package root.

WHAT:
    This file marks the `backend/` directory as a Python package.
    Without it, Python would treat `backend/` as a plain folder and
    `from backend.db.connection import get_db` would fail with a
    ModuleNotFoundError.

WHY:
    Python's import system requires a directory to contain `__init__.py`
    before it can be used as a package namespace.  Adding this file is
    effectively a declaration: "this directory is importable as `backend`."

ARCHITECTURE:
    The `backend` package contains three sub-packages:

        backend/
        ├── __init__.py          ← you are here
        ├── main.py              ← FastAPI app instance and routes
        ├── db/
        │   ├── __init__.py      ← db sub-package marker
        │   ├── connection.py    ← Engine, SessionLocal, get_db()
        │   └── job_repository.py  ← all SQL operations on the jobs table
        └── models/
            ├── __init__.py      ← models sub-package marker
            └── job.py           ← Job ORM model and JobStatus enum

IMPORTANT CONCEPTS:
    - A Python package is just a directory with __init__.py.
    - The `PYTHONPATH=.` prefix in test commands ensures the repo root is
      on Python's search path so `import backend` resolves correctly.
    - This file is intentionally empty of executable code.  `__init__.py`
      files can run arbitrary code at import time, but doing so in a
      production package is generally avoided because it runs every time
      any sub-module is imported — surprising and hard to debug.
"""
