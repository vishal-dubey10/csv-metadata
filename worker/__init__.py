"""
CSVForge — worker package.

WHAT:
    This file marks the `worker/` directory as a Python package.
    The worker package will contain the AWS Lambda function that picks
    CSV processing jobs off the SQS queue and executes them.

WHY THIS IS ITS OWN PACKAGE:
    The worker is deployed separately from the API.  It is a distinct
    Lambda function with its own entry point, its own IAM execution role,
    and its own deployment artifact (a zip file).  Keeping it in a separate
    top-level package (`worker/`) rather than inside `backend/` makes the
    deployment boundary visible in the file system.

    A concrete consequence: `worker/requirements.txt` is a separate file
    from `backend/requirements.txt`.  The worker's Lambda zip includes only
    what the worker needs — it has no FastAPI, no uvicorn, no Alembic —
    making the artifact smaller and the container cold start faster.

ARCHITECTURE:
    Day 2+ structure (when implemented):

        worker/
        ├── __init__.py          ← you are here
        ├── handler.py           ← Lambda entry point (handler function)
        ├── csv_processor.py     ← CSV parsing and validation logic
        └── requirements.txt     ← worker-only dependencies

    The Lambda handler will:
      1. Receive an SQS event (a batch of messages, each containing a job_id).
      2. Call `claim_job_for_processing(db, job_id)` from `backend.db.job_repository`.
      3. Download the CSV from S3.
      4. Parse and validate the CSV.
      5. Call `complete_job(db, job_id, ...)` or `fail_job(db, job_id, ...)`.

    The worker imports from `backend.db` (the shared database layer) because
    both the API and the worker operate on the same PostgreSQL database.
    The shared code lives in `backend/` because that was the initial package;
    the worker depends on it but is not part of it.

DAY 1 STATUS:
    This package is a placeholder.  Day 1 builds the database layer and
    the API; the worker logic is implemented in Day 2.  The package exists
    now so that `worker/requirements.txt` has a home and so that the
    deployment structure is visible from the start.

IMPORTANT:
    This file is intentionally empty of executable code.
"""
