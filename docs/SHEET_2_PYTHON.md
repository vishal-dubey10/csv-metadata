# Sheet 2 — Python Concepts in CSVForge

> Every concept shown with the exact code from this codebase.
> File paths are real. Line references are approximate.

---

## 1. Type Annotations — Python Knows What Your Variables Are

Python is dynamically typed by default. Type annotations add static
information that IDEs and tools like `mypy` use to catch errors before
runtime. They are **not enforced at runtime** — they are documentation
that tools can verify.

```python
# File: backend/db/job_repository.py

# Without annotations — IDE has no idea what db or job_id are
def get_job_by_id(db, job_id):
    ...

# With annotations — IDE knows db is a Session, job_id is a str,
# return value is either a Job object or None
def get_job_by_id(db: Session, job_id: str) -> Optional[Job]:
    stmt = select(Job).where(Job.id == job_id)
    return db.execute(stmt).scalar_one_or_none()
```

```python
# File: backend/db/connection.py

# Mapped[str] tells SQLAlchemy AND mypy that job.id is always a string
id: Mapped[str] = mapped_column(VARCHAR(36), primary_key=True)

# Mapped[str | None] means the value is either a string or None (nullable column)
processed_s3_key: Mapped[str | None] = mapped_column(VARCHAR(512), nullable=True)
```

---

## 2. Dataclass — A Class That Is Just Data

`@dataclass` auto-generates `__init__`, `__repr__`, and `__eq__` from the
class annotations. Use it when you just need a typed container for values —
no methods, no logic.

```python
# File: backend/auth.py

from dataclasses import dataclass
from typing import Optional

@dataclass
class CurrentUser:
    user_id: str               # required field
    is_admin: bool = False     # optional field with default
    email: Optional[str] = None

# Using it:
user = CurrentUser(user_id="abc-123", is_admin=True)
print(user.user_id)    # "abc-123"
print(user.is_admin)   # True
print(user.email)      # None

# @dataclass generates this automatically:
# def __init__(self, user_id: str, is_admin: bool = False, email: Optional[str] = None):
#     self.user_id = user_id
#     self.is_admin = is_admin
#     self.email = email
```

---

## 3. Enum — A Fixed Set of Named Values

An `Enum` replaces magic strings like `"pending_upload"` with named
constants. The `str` mixin makes each member equal to its string value —
so `JobStatus.QUEUED == "queued"` is `True`.

```python
# File: backend/models/job.py

import enum

class JobStatus(str, enum.Enum):
    PENDING_UPLOAD = "pending_upload"
    QUEUED         = "queued"
    PROCESSING     = "processing"
    COMPLETED      = "completed"
    FAILED         = "failed"

# Without the str mixin:
JobStatus.QUEUED == "queued"         # False — different types
JobStatus.QUEUED.value == "queued"   # True — must use .value

# WITH the str mixin:
JobStatus.QUEUED == "queued"         # True — works directly
str(JobStatus.QUEUED)                # "queued"

# Used in job_repository.py:
.values(status=JobStatus.QUEUED.value)
# or simply:
.values(status=JobStatus.QUEUED)   # also works because of str mixin
```

---

## 4. Generator Function (yield) — Pause and Resume

A function with `yield` is a generator. It pauses at `yield`, returns a
value, and resumes when the caller asks for the next value. FastAPI uses
this for `Depends()` to run setup and cleanup around a request.

```python
# File: backend/db/connection.py

from collections.abc import Generator

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()         # 1. Open a DB session
    try:
        yield db                # 2. PAUSE here — hand the session to the route
                                #    The route handler runs completely
        db.commit()             # 3. Route returned normally: make writes durable
    except Exception:
        db.rollback()           # 4. Route raised an exception: undo all writes
        raise                   #    re-raise so FastAPI returns a 500 error
    finally:
        db.close()              # 5. ALWAYS runs: return connection to pool
```

**How FastAPI uses it:**

```python
# FastAPI calls next() on the generator:
gen = get_db()
db = next(gen)              # runs up to yield, returns db
# ... route handler uses db ...
try:
    next(gen)               # resumes after yield, runs commit + finally
except StopIteration:
    pass
```

**Why `finally` matters:**

```python
# If you wrote it without finally:
def get_db_broken():
    db = SessionLocal()
    yield db
    db.close()   # NEVER RUNS if the route raised an exception!

# With finally:
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()   # ALWAYS runs, even if the route raised an exception
```

---

## 5. Decorator — Wrapping Functions

A decorator is a function that takes a function and returns a modified
version. `@app.get("/health")` registers the function as a FastAPI route
handler. When a GET /health request arrives, FastAPI calls `health_check()`.

```python
# File: backend/main.py

@app.get("/health")                 # decorator: registers this function as a route
def health_check() -> dict:
    return {"status": "ok", "environment": os.getenv("ENVIRONMENT", "production")}

# Without decorator syntax (equivalent but verbose):
def health_check() -> dict:
    return {"status": "ok"}
app.get("/health")(health_check)   # manually register
```

```python
# File: backend/db/job_repository.py — not a FastAPI decorator, but same concept

# Python's built-in functools.lru_cache is a decorator:
from functools import lru_cache

@lru_cache(maxsize=128)
def expensive_function(x):   # result is cached after first call
    ...
```

---

## 6. Exception Handling — try / except / finally

```python
# File: backend/db/connection.py

def _build_database_url() -> str:
    environment = os.getenv("ENVIRONMENT", "production")

    if environment == "local":
        url = os.getenv("DATABASE_URL")
        if not url:
            # Raise with a clear message — better than AttributeError later
            raise RuntimeError(
                "ENVIRONMENT=local but DATABASE_URL is not set. "
                "Copy .env.example to .env and fill in DATABASE_URL."
            )
        return url

    secret_arn = os.environ["DB_SECRET_ARN"]   # KeyError if missing
    # ...

# File: backend/s3_client.py — catching specific exceptions

def object_exists(bucket: str, key: str) -> bool:
    client = get_s3_client()
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "404":
            return False        # object doesn't exist — not an error
        raise                   # re-raise any other error (403, 503, etc.)
```

```python
# File: backend/main.py — raising HTTP errors

from fastapi import HTTPException

def upload_complete(job_id, db, current_user):
    job = get_job_by_id(db=db, job_id=job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # FastAPI catches HTTPException and returns the right HTTP response
```

---

## 7. f-strings — String Interpolation

f-strings (`f"..."`) embed Python expressions directly in strings. They
are evaluated at runtime and are faster than `%` formatting or `.format()`.

```python
# File: backend/main.py

user_id = "aabb-ccdd"
job_id  = "550e8400-e29b-41d4-a716-446655440000"

# Simple f-string
s3_key = f"uploads/{user_id}/{job_id}/original.csv"
# → "uploads/aabb-ccdd/550e8400-.../original.csv"

# File: backend/db/connection.py
return f"postgresql+psycopg://{username}:{password}@{host}:{port}/{dbname}"
# → "postgresql+psycopg://csvforge:secret@rds-host:5432/csvforge"

# Expression inside f-string
size_mb = f"{file.size / (1024 * 1024):.1f} MB"
# → "23.4 MB"  (:.1f = one decimal place)

# Multi-line f-string
raise RuntimeError(
    f"ENVIRONMENT=local but DATABASE_URL is not set. "
    f"Copy .env.example to .env and fill in DATABASE_URL."
)
```

---

## 8. List Comprehension — Concise List Transformation

A compact way to build a list from another iterable. Equivalent to a for
loop that appends to a list.

```python
# File: backend/main.py  (_get_cors_origins)

raw = "http://localhost:5173, https://example.com , "

# List comprehension with filter:
origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
# → ["http://localhost:5173", "https://example.com"]

# Equivalent for loop:
origins = []
for origin in raw.split(","):
    if origin.strip():
        origins.append(origin.strip())
```

```python
# File: backend/main.py  (admin_list_jobs)

return [_job_to_admin_response(j) for j in jobs]
# Transforms every Job ORM object in the list to an AdminJobResponse Pydantic model
```

---

## 9. Dictionary Unpacking (`**`) — Merging Dictionaries

The `**` operator unpacks a dictionary into keyword arguments or merges
dictionaries.

```python
# File: backend/main.py

# Using **kwargs — not directly in this file, but the pattern is used
# by SQLAlchemy's .values(**{...}) pattern and os.environ

# In Python 3.9+, merge two dicts:
base_headers = {"Content-Type": "application/json"}
auth_headers = {"Authorization": "Bearer abc123"}
merged = {**base_headers, **auth_headers}
# → {"Content-Type": "application/json", "Authorization": "Bearer abc123"}

# File: backend/db/connection.py
# Demonstrates conditional keyword arguments (same pattern used in create_engine)
engine_kwargs = {}
if _is_postgres:
    engine_kwargs["pool_size"] = 1
    engine_kwargs["pool_pre_ping"] = True
engine = create_engine(DATABASE_URL, **engine_kwargs)
```

---

## 10. `Optional` and `None` Handling

`Optional[X]` means the value is either type `X` or `None`. Always check
for `None` before using the value. The `?.` safe-navigation pattern from
other languages is not in Python — use `if x is not None` or `x or default`.

```python
# File: backend/db/job_repository.py

from typing import Optional

def get_job_by_id(db: Session, job_id: str) -> Optional[Job]:
    return db.execute(select(Job).where(Job.id == job_id)).scalar_one_or_none()
    # Returns Job if found, None if not found

# Caller handles None explicitly:
# File: backend/main.py
job = get_job_by_id(db=db, job_id=job_id)
if job is None:
    raise HTTPException(status_code=404, detail="Job not found")
# After this check, job is guaranteed to be a Job object
```

```python
# Walrus operator — assign and check in one expression (Python 3.8+)
if url := os.getenv("DATABASE_URL"):
    return url
# Same as:
url = os.getenv("DATABASE_URL")
if url:
    return url
```

---

## 11. `json` Module — Serialising and Deserialising

```python
# File: backend/sqs_client.py

import json

# Python dict → JSON string (serialise)
body = json.dumps({"job_id": job_id, "s3_key": s3_key})
# → '{"job_id": "550e8400-...", "s3_key": "uploads/..."}'

# JSON string → Python dict (deserialise)
message = json.loads(body)
message["job_id"]   # "550e8400-..."

# File: backend/db/connection.py
# Secrets Manager returns JSON: {"username": "...", "password": "..."}
response = client.get_secret_value(SecretId=secret_arn)
secret = json.loads(response["SecretString"])
username = secret["username"]
password = secret["password"]
```

---

## 12. `os` Module — Environment Variables and File Paths

```python
# File: backend/db/connection.py

import os

# os.getenv("KEY") — returns None if not set (safe)
environment = os.getenv("ENVIRONMENT", "production")
#                                       ^^^^^^^^^^ default value

# os.environ["KEY"] — raises KeyError if not set (intentional crash)
secret_arn = os.environ["DB_SECRET_ARN"]
# If DB_SECRET_ARN is missing, KeyError tells you immediately
# (better than a cryptic error later)

# File: backend/s3_client.py
from pathlib import Path

dest = Path(dest_path)
dest.parent.mkdir(parents=True, exist_ok=True)   # create parent dirs if needed
# parents=True  → create intermediate directories
# exist_ok=True → don't error if the directory already exists
```

---

## 13. Async Functions — `async def` and `await`

Python's `async/await` syntax allows a function to pause while waiting for
I/O (network, disk) without blocking the entire process. FastAPI routes
can be sync or async.

```python
# File: backend/auth.py

# async def — the function can be paused at `await` points
async def get_current_user(
    request: Request,
    x_dev_user_id: str = Header(default=""),
) -> CurrentUser:
    environment = os.getenv("ENVIRONMENT", "production")

    if environment == "local":
        return CurrentUser(user_id=x_dev_user_id or "dev-user-001")

    # In production, this reads from request.scope (no I/O needed here)
    aws_event = request.scope.get("aws.event", {})
    claims = aws_event.get("requestContext", {}).get("authorizer", {}) \
                      .get("jwt", {}).get("claims", {})
    user_id = claims.get("sub", "")
    return CurrentUser(user_id=user_id)
```

```python
# Most route handlers in backend/main.py are synchronous (def not async def)
# because SQLAlchemy's sync driver (psycopg 3 sync mode) doesn't need awaiting.

@app.get("/jobs")
def list_jobs(db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    jobs = list_jobs_for_user(db=db, user_id=current_user.user_id)
    return JobListResponse(jobs=[_job_to_response(j) for j in jobs], total=len(jobs))
```

---

## 14. `uuid` Module — Generating Unique IDs

```python
# File: backend/db/job_repository.py

import uuid

job = Job(
    id=str(uuid.uuid4()),   # generates a random UUID like "550e8400-e29b-41d4-a716-446655440000"
    ...
)
```

`uuid4()` generates a random 128-bit number formatted as a UUID string.
The probability of a collision is astronomically small (about 1 in 10^38).

**Why UUID instead of sequential integer?**
- Sequential IDs leak count information: job `1001` tells an attacker
  that job `1000` probably exists for another user.
- UUID is non-enumerable: knowing your job UUID gives zero information
  about other jobs.
- UUID can be generated in Python without a database round-trip.

---

## 15. `logging` Module — Structured Application Logs

```python
# File: backend/db/connection.py

import logging

# Create a logger named after this module
logger = logging.getLogger(__name__)
# __name__ is the module's dotted path: "backend.db.connection"

# Log levels (from least to most severe):
logger.debug("Detailed information for debugging")
logger.info("Normal operation: job created")
logger.warning("Something unexpected but not fatal")
logger.error("An error occurred")
logger.exception("An error occurred", exc_info=True)   # includes traceback
```

In Lambda, logs automatically go to CloudWatch Logs. Each log level maps
to a CloudWatch log entry that you can filter and alert on.

---

## Concept Quick Reference

| Concept | Syntax | Where in CSVForge |
|---|---|---|
| **Type annotations** | `x: str`, `-> Optional[Job]` | Every function signature |
| **Dataclass** | `@dataclass` | `CurrentUser` in `auth.py` |
| **Enum** | `class X(str, enum.Enum)` | `JobStatus` in `models/job.py` |
| **Generator / yield** | `yield db` | `get_db()` in `connection.py` |
| **Decorator** | `@app.get("/path")` | Every route in `main.py` |
| **Exception handling** | `try/except/finally` | `get_db()`, `object_exists()` |
| **f-string** | `f"uploads/{user_id}/{job_id}"` | S3 key construction |
| **List comprehension** | `[f(x) for x in xs if cond]` | CORS origins, response serialisation |
| **Optional** | `Optional[Job]` | Repository return types |
| **json** | `json.dumps()`, `json.loads()` | SQS messages, Secrets Manager |
| **os.getenv** | `os.getenv("KEY", default)` | Every env var read |
| **async/await** | `async def`, `await` | `get_current_user()` |
| **uuid** | `str(uuid.uuid4())` | Job ID generation |
| **Path** | `Path(dest).parent.mkdir(...)` | S3 download in `s3_client.py` |
