# CSVForge — Revision Notes & Interview Prep

> Read top-to-bottom once. After that, use section headers as a checklist.
> Every concept below is grounded in the actual code in this repository.

---

## Learning Order — Why This Sequence

```
Python fundamentals → SQLAlchemy ORM → PostgreSQL → FastAPI → AWS (S3, SQS, Lambda) → CI/CD → Frontend (React/Angular)
```

Each layer builds on the previous one. You cannot explain SQLAlchemy sessions without
understanding Python generators. You cannot explain FastAPI dependencies without
understanding Python functions. Start from the bottom, work up.

---

## 1. Python Fundamentals

These are the Python mechanics you must be able to explain verbatim because they appear
everywhere in this codebase — and are common interview questions.

### 1.1 Mutable vs Immutable Objects

**Immutable** — the value cannot change after creation. Python creates a new object
when you "modify" it.

| Immutable | Mutable |
|-----------|---------|
| `int`, `float`, `bool` | `list` |
| `str` | `dict` |
| `tuple` | `set` |
| `frozenset` | Custom class instances (usually) |

```python
# String is immutable — a new string is created
s = "hello"
s = s + " world"  # 'hello' still exists in memory; s now points to a new object

# List is mutable — the same object is modified in place
nums = [1, 2, 3]
nums.append(4)    # the same list object at the same memory address now has 4
```

**Why it matters in interviews:**
- Mutable default arguments are a classic Python gotcha:
  ```python
  # BAD — the list is shared across all calls
  def add(item, collection=[]):
      collection.append(item)
      return collection

  # GOOD
  def add(item, collection=None):
      if collection is None:
          collection = []
      collection.append(item)
      return collection
  ```
- Strings passed as function arguments cannot be modified by the function (immutable).
- Dicts and lists passed as function arguments CAN be modified in place (mutable).

---

### 1.2 Generators and `yield`

A generator is a function that produces values lazily, one at a time, instead of
computing all values at once. The `yield` keyword is what makes it a generator.

```python
def count_up(n):
    i = 0
    while i < n:
        yield i   # pauses here, returns i to the caller
        i += 1    # resumes here on the next next() call

gen = count_up(3)
next(gen)  # → 0
next(gen)  # → 1
next(gen)  # → 2
next(gen)  # → StopIteration
```

**Why `yield` matters for FastAPI (this codebase):**

`get_db()` in `connection.py` uses `yield`:

```python
def get_db():
    db = SessionLocal()
    try:
        yield db      # FastAPI pauses here, injects db into the route handler
        db.commit()   # runs AFTER the route handler returns
    except Exception:
        db.rollback() # runs if the route handler raises
        raise
    finally:
        db.close()    # ALWAYS runs, even on exception
```

A regular `return` could not run cleanup after the caller is done. `yield` suspends
execution at that line, lets the route handler run completely, then resumes from
after the `yield` for cleanup. This is Python's standard pattern for resource
management when `with` statements aren't available.

---

### 1.3 List Comprehensions

```python
# Traditional loop
squares = []
for n in range(5):
    squares.append(n ** 2)

# List comprehension — same result, one line
squares = [n ** 2 for n in range(5)]

# With condition
evens = [n for n in range(10) if n % 2 == 0]

# Nested (flattening a matrix)
matrix = [[1, 2], [3, 4], [5, 6]]
flat = [x for row in matrix for x in row]  # → [1, 2, 3, 4, 5, 6]
```

---

### 1.4 `finally` Block

The `finally` block **always** executes — whether the `try` block succeeds, raises
an exception, or even calls `return`. This is why `get_db()` puts `db.close()` in
`finally`.

```python
def risky():
    try:
        return "success"      # finally STILL runs before actually returning
    except ValueError:
        return "error"
    finally:
        print("always runs")  # printed even when return is called in try

risky()  # prints "always runs", then returns "success"
```

---

### 1.5 Enums

```python
import enum

class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    COMPLETED = "completed"
```

The `str` mixin (inheriting from both `str` and `enum.Enum`) means each enum
member IS a string:

```python
JobStatus.QUEUED == "queued"       # True  — with str mixin
JobStatus.QUEUED.value == "queued" # True  — always works
```

Without the `str` mixin, `JobStatus.QUEUED == "queued"` would be `False`, causing
silent comparison bugs when comparing an enum to a string read from the database.

---

### 1.6 String Splitting — the `split()` / `yield` Interview Question

The live coding question from the interview: *implement Python's `split()` using `yield`*.

```python
def my_split(text, delimiter=" "):
    start = 0
    for i, char in enumerate(text):
        if char == delimiter:
            yield text[start:i]
            start = i + 1
    yield text[start:]  # last segment after the final delimiter

list(my_split("hello world foo"))  # → ['hello', 'world', 'foo']
```

---

### 1.7 Grouping by First Letter — the dict comprehension problem

```python
words = ["apple", "banana", "avocado", "berry", "carrot", "apple", "berry"]

# Group by first letter, deduplicated
result = {}
for word in words:
    key = word[0]
    if key not in result:
        result[key] = []
    if word not in result[key]:
        result[key].append(word)

# → {'a': ['apple', 'avocado'], 'b': ['banana', 'berry'], 'c': ['carrot']}
```

---

### 1.8 Flatten a List

```python
nested = [[1, 2, [3, 4]], [5, 6], [7, [8, [9]]]]

# Using a recursive generator
def flatten(lst):
    for item in lst:
        if isinstance(item, list):
            yield from flatten(item)
        else:
            yield item

list(flatten(nested))  # → [1, 2, 3, 4, 5, 6, 7, 8, 9]
```

---

## 2. SQLAlchemy — ORM and Database Patterns

This is the heaviest technical area. Know it cold.

### 2.1 The Engine / Pool / Session Mental Model

Think of a restaurant:

| SQLAlchemy Concept | Restaurant Analogy |
|--------------------|-------------------|
| **Engine** | The restaurant manager — knows where the kitchen is, manages all the tables |
| **Connection Pool** | The set of tables — pre-set, ready for customers |
| **Connection** | One occupied table — one open TCP socket to PostgreSQL |
| **Session** | One customer's full order — tracks everything ordered, ends with a bill (commit) or walking out (rollback) |

**Engine** is created once per process (or Lambda container). Creating a new Engine
per request would create a new connection pool per request — defeating the entire
point of pooling.

**Session** is created once per request. A long-lived Session accumulates stale data
from earlier requests and causes consistency problems.

---

### 2.2 Connection Pooling — Why It Matters

Opening a PostgreSQL connection involves:
1. TCP three-way handshake
2. TLS/SSL negotiation (required on RDS)
3. PostgreSQL authentication (username, password exchange)

This costs 10–50 ms per connection. For an API handling hundreds of requests per
second, recreating the connection on every request adds serious latency. A pool
pre-creates N connections and reuses them — each request borrows one, runs queries,
and returns it.

**Why `pool_size=1` for Lambda:**
Lambda handles exactly one request per container. At 500 concurrent requests you
have 500 containers × 1 connection = 500 total connections. With `pool_size=5` you'd
have 2500 connections — `db.t3.micro` only handles ~100.

**`pool_pre_ping=True`** solves stale connections. After a Lambda container is frozen
for 5+ minutes, the TCP connection to RDS may have been dropped by a NAT gateway.
Without `pool_pre_ping`, the first query fails with a cryptic "broken pipe" error.
With it, SQLAlchemy sends a lightweight `SELECT 1` probe before handing out the
connection. If the probe fails, it opens a fresh connection transparently.

---

### 2.3 `flush()` vs `commit()`

| Operation | What it does | Analogy |
|-----------|-------------|---------|
| `flush()` | Sends SQL to the DB within the current transaction. Visible to queries within the same transaction. Not permanent yet. | Writing in pencil |
| `commit()` | Makes the transaction permanent. Visible to ALL other connections. | Signing in ink |
| `rollback()` | Discards everything since the last commit. | Crumpling up the paper |

In `create_job()`:
1. `db.add(job)` — stages the INSERT in memory
2. `db.flush()` — sends the INSERT within the transaction (so `created_at` is generated by the DB)
3. `db.refresh(job)` — re-reads the row from the DB to get the server-generated `created_at` timestamp
4. `db.commit()` — happens later in `get_db()` after the route returns

---

### 2.4 `expire_on_commit=False`

After `db.commit()`, SQLAlchemy normally marks all ORM object attributes as "expired."
The next access to `job.status` would trigger a `SELECT` to reload from the DB. But in
FastAPI, the response serialization happens AFTER the commit — if the session is
already closing, this triggers `DetachedInstanceError`.

Setting `expire_on_commit=False` keeps in-memory values readable after commit. Safe
for a one-session-per-request pattern because there is no "next request" that could
see stale data from this session.

---

### 2.5 `expire_all()` — When Bulk Updates Break the Identity Map

SQLAlchemy's Session keeps an **identity map**: a dictionary of `(class, primary_key) → Python object`. After an ORM-level UPDATE to individual objects, the in-memory object is updated. But after a bulk UPDATE (Core SQL, `update(Job).values(...)`), the identity map still shows the OLD values.

`db.expire_all()` forces every loaded ORM object's attributes to be re-read from the
database on next access. Used after `claim_job_for_processing()` to prevent reading
stale status.

---

### 2.6 Conditional UPDATE — Idempotency Without Locks

The worker uses this pattern to claim a job:

```sql
UPDATE jobs
SET status = 'processing'
WHERE id = :job_id
AND status IN ('queued', 'processing')
```

This checks the condition AND updates in one atomic SQL statement. No explicit row
lock (SELECT FOR UPDATE) needed.

- If two workers race for the same job, the database row lock ensures only one UPDATE
  executes at a time.
- The first worker gets `rowcount=1` → proceeds.
- The second worker's UPDATE hits the row but status is now `'processing'` — it's still
  IN the allowed list, so it also gets `rowcount=1`. But S3 overwrites are idempotent,
  and `complete_job` just overwrites the result columns. End state is correct.
- If the job was already `completed` or `failed`, `rowcount=0` and the worker exits.

---

### 2.7 ORM Model Syntax (SQLAlchemy 2.0)

Old style (1.x):
```python
class Job(Base):
    id = Column(String(36), primary_key=True)
    status = Column(String(32), nullable=False)
```

New style (2.0) — used in this codebase:
```python
class Job(Base):
    id: Mapped[str] = mapped_column(VARCHAR(36), primary_key=True)
    status: Mapped[str] = mapped_column(VARCHAR(32), nullable=False)
    processed_s3_key: Mapped[str | None] = mapped_column(VARCHAR(512), nullable=True)
```

`Mapped[str]` is the Python-level type annotation — your IDE knows `job.id` is a `str`.
`mapped_column(VARCHAR(36))` is the database-level descriptor. Together they give full
type safety at both Python and SQL levels.

---

### 2.8 Repository Pattern vs Active Record

| Pattern | How it works | Example |
|---------|-------------|---------|
| **Repository** (this codebase) | All DB logic for one entity lives in one module. Routes call functions like `create_job(db, ...)`. They never write SQL. | `job_repository.py` |
| **Active Record** (Django ORM) | The model object itself has `.save()`, `.delete()`, `.filter()`. Logic and data are merged. | `Job.objects.filter(user_id=uid)` |

Repository is easier to test (swap the DB backend by changing one file) and easier to
reason about (no business logic hidden in model methods). Active Record is more
convenient for simple CRUD — fewer files to navigate.

---

## 3. PostgreSQL — Relational Database Concepts

### 3.1 The `jobs` Table Schema

15 columns. Know what each is for and which are NULL until a certain state:

| Column | Type | NULL when |
|--------|------|----------|
| `id` | VARCHAR(36) UUID | Never |
| `user_id` | VARCHAR(128) | Never |
| `status` | VARCHAR(32) | Never |
| `original_filename` | VARCHAR(255) | Never |
| `original_s3_key` | VARCHAR(512) | Never |
| `processed_s3_key` | VARCHAR(512) | NULL until `completed` |
| `file_size_bytes` | BIGINT | NULL until upload confirmed |
| `row_count` | INTEGER | NULL until `completed` |
| `column_names` | ARRAY(TEXT) | NULL until `completed` |
| `failure_reason` | TEXT | NULL unless `failed` |
| `created_at` | TIMESTAMPTZ | Never |
| `updated_at` | TIMESTAMPTZ | Never |

---

### 3.2 Why VARCHAR over PostgreSQL Native ENUM?

PostgreSQL native ENUMs require `ALTER TYPE` to add new values. `ALTER TYPE` takes an
`ACCESS EXCLUSIVE` lock on the table — no reads or writes during the migration. For a
high-traffic jobs table this is unacceptable. `VARCHAR(32)` lets you add new status
values with zero downtime (just deploy new code; no ALTER TABLE needed).

---

### 3.3 TIMESTAMPTZ vs TIMESTAMP

**TIMESTAMP** stores date+time as-is with no timezone. If the server moves timezones,
the stored value represents a different instant.

**TIMESTAMPTZ** (TIMESTAMP WITH TIME ZONE) stores as UTC internally. Always correct
regardless of server or database timezone settings. Always use TIMESTAMPTZ for
production systems.

---

### 3.4 Indexes — Why They Exist

Three indexes on the `jobs` table:

```sql
idx_jobs_user_id    ON jobs (user_id)      -- WHERE user_id = X (list jobs for a user)
idx_jobs_status     ON jobs (status)       -- WHERE status = Y (admin filter)
idx_jobs_created_at ON jobs (created_at)   -- ORDER BY created_at DESC
```

Without `idx_jobs_user_id`, every `GET /jobs` request would scan the entire table —
O(N) where N is total rows across all users. With the index, PostgreSQL jumps
directly to that user's rows — O(log N).

The trade-off: indexes add storage and slow down INSERT/UPDATE slightly (the index
B-tree must be updated on every write).

---

### 3.5 UUID vs Sequential Integer Primary Keys

| | Sequential Integer | UUID |
|--|---------------------|------|
| Storage | 4 bytes (INT) or 8 bytes (BIGINT) | 16 bytes (or 36 as VARCHAR) |
| B-tree locality | New rows append to end of index → fewer page splits | Random insertion → more page splits |
| Enumeration risk | Easy to guess (1, 2, 3...) | Not guessable |
| Generation | Requires DB round-trip | Python can generate without DB |
| Merge safety | Collisions across databases | Globally unique |

CSVForge uses UUID because: (1) you need the ID before the INSERT to build the S3 key,
and (2) user-facing IDs should not be enumerable.

---

### 3.6 ACID Transactions

**A**tomicity — all operations in a transaction succeed or all fail together.
**C**onsistency — the database moves from one valid state to another.
**I**solation — concurrent transactions don't see each other's uncommitted changes.
**D**urability — committed transactions survive crashes (written to disk).

This is why PostgreSQL was chosen over DynamoDB. Job state must never be partially
updated — a job cannot be `completed` without `processed_s3_key` being set. A
transaction guarantees both columns are written atomically or neither is.

---

## 4. FastAPI — Python Web Framework

### 4.1 WSGI vs ASGI

**WSGI** (Web Server Gateway Interface) — synchronous, one request at a time per
thread. Django and Flask use WSGI.

**ASGI** (Asynchronous Server Gateway Interface) — asynchronous, can handle many
concurrent requests on a single thread. FastAPI is ASGI.

---

### 4.2 Dependency Injection

```python
@app.get("/jobs")
def list_jobs(
    current_user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    jobs = list_jobs_for_user(db=db, user_id=current_user.user_id)
    return jobs
```

`Depends(get_current_user)` tells FastAPI: before calling `list_jobs`, call
`get_current_user` and inject its return value as `current_user`. This is dependency
injection.

**Why it matters:**
- In production, `get_current_user` reads the JWT and returns the real user.
- In tests, you can override it: `app.dependency_overrides[get_current_user] = lambda: fake_user`.
- The route function (`list_jobs`) doesn't change. This is the open/closed principle.

---

### 4.3 Request Lifecycle (Trace This End-to-End)

```
Browser: GET /jobs HTTP/1.1
         Authorization: Bearer eyJ...

1. API Gateway validates the JWT against Cognito's public keys.
   Invalid → 401, never reaches FastAPI.

2. Mangum converts the Lambda JSON event into an ASGI request.

3. FastAPI router matches GET /jobs → list_jobs() function.

4. FastAPI resolves dependencies BEFORE calling list_jobs():
   - get_current_user() → reads JWT claims → returns CurrentUser(user_id="abc...")
   - get_db() → opens Session → yields db

5. list_jobs() runs:
   - calls list_jobs_for_user(db, user_id=current_user.user_id)
   - SQL: SELECT * FROM jobs WHERE user_id='abc...' AND status != 'pending_upload'
          ORDER BY created_at DESC
   - returns list of Job ORM objects

6. FastAPI serializes Job objects to JSON via Pydantic (JobResponse model).
   Returns HTTP 200 with JSON body.

7. get_db() cleanup:
   - db.commit() (no writes in this request → no-op)
   - db.close() (returns connection to pool)
```

---

### 4.4 HTTP Methods and Status Codes

**Methods used in this codebase:**

| Method | Purpose | Has Request Body? |
|--------|---------|-------------------|
| GET | Retrieve data, no side effects | No |
| POST | Create a resource or trigger an action | Yes |

**Status codes — the full picture:**

| Range | Meaning | Examples |
|-------|---------|---------|
| 1xx | Informational — request received, continuing | 100 Continue |
| 2xx | Success | 200 OK, 201 Created, 204 No Content |
| 3xx | Redirect | 301 Moved Permanently, 302 Found |
| 4xx | Client error — the request was wrong | 400 Bad Request, 401 Unauthorized, 403 Forbidden, 404 Not Found, 409 Conflict, 422 Unprocessable Entity |
| 5xx | Server error — the server failed | 500 Internal Server Error, 502 Bad Gateway, 503 Service Unavailable |

**Why POST for login?**
Login sends credentials (username + password) in the request body. GET requests have
no body and are logged by servers, proxies, and browser history — credentials in a GET
URL would leak. POST sends credentials in the body over HTTPS, which is encrypted.

**Can you send a JSON body in a GET request?**
Technically yes — the HTTP spec does not forbid it. But most HTTP clients, proxies, and
servers do not support it. Servers like API Gateway may strip the body from GET requests.
In practice: no. Use POST if you need to send a body.

---

### 4.5 HTTP Headers

Request headers the browser sends:
```
Authorization: Bearer <JWT>    — identifies the user
Content-Type: application/json — tells the server the body format
Origin: http://localhost:5173  — CORS: where the request came from
```

Response headers the server sends:
```
Content-Type: application/json
Access-Control-Allow-Origin: http://localhost:5173  — CORS permission
Content-Range: jobs 0-24/150                        — pagination for React Admin
```

**How to set headers in FastAPI:**
```python
@app.get("/admin/jobs")
def admin_list_jobs(response: Response):
    response.headers["Content-Range"] = f"jobs 0-9/{total}"
```

---

### 4.6 CORS (Cross-Origin Resource Sharing)

Browsers block JavaScript from reading responses that come from a different origin
(different domain, port, or scheme) — this is the Same-Origin Policy.

A React app at `http://localhost:5173` calling an API at `http://localhost:8000` is
a cross-origin request. Without CORS headers, the browser blocks it.

The API must respond with:
```
Access-Control-Allow-Origin: http://localhost:5173
Access-Control-Allow-Credentials: true
```

In this codebase: `CORSMiddleware` in `main.py` adds these headers automatically.

---

### 4.7 Pydantic — Request/Response Validation

Pydantic validates request bodies and serializes response data. FastAPI uses it
automatically when you annotate route parameters with Pydantic models.

```python
class UploadIntentRequest(BaseModel):
    original_filename: str
    user_email: str

@app.post("/uploads/intent")
def upload_intent(body: UploadIntentRequest):
    # FastAPI has already validated body — no need to check manually
    # If filename is missing, FastAPI returns 422 Unprocessable Entity
    ...
```

---

### 4.8 404 vs 403 for Ownership Failures

When a user requests a job that belongs to another user, CSVForge returns **404**, not 403.

**Why not 403?**
403 (Forbidden) says "I know this resource exists, you just can't access it." This
leaks information — an attacker can enumerate job IDs and discover which ones exist.

**404** for both "doesn't exist" and "belongs to someone else" tells the attacker
nothing. This is a standard defence-in-depth pattern for user-facing resources.
Trade-off: you can't distinguish between a job that doesn't exist and one you don't own.
That's acceptable because legitimate callers always know their own job IDs.

---

### 4.9 CSRF Tokens

CSRF (Cross-Site Request Forgery) tricks a logged-in user's browser into making an
unwanted request to a site they're already authenticated to (e.g., a malicious page
that submits a hidden form to your bank).

A CSRF token is a random, session-specific value included in forms. The server
validates it on every state-changing request. Because the malicious page can't read
the token (it's not on that origin), it can't forge the request.

Django provides CSRF protection built-in (`{% csrf_token %}` in templates).
FastAPI with JWT Bearer tokens is naturally resistant to CSRF because the browser
doesn't automatically send the `Authorization: Bearer` header to cross-origin requests —
unlike cookies, which are sent automatically.

---

### 4.10 Mangum — Running FastAPI on Lambda

```python
from mangum import Mangum
handler = Mangum(app, lifespan="off")
```

This one line is the entire production adapter. When Lambda receives an API Gateway
event (a JSON dict), Mangum translates it into an ASGI request, passes it to FastAPI,
and packages the response back. The same FastAPI code runs locally (via uvicorn) and
in Lambda (via Mangum) with no changes.

---

## 5. AWS Services

### 5.1 S3 — Object Storage

S3 is an infinite key-value store for files. Keys are path-like strings; values are
bytes.

**How CSVForge uses it:**
- `uploads/{user_id}/{job_id}/original.csv` — raw user upload
- `processed/{user_id}/{job_id}/result.csv` — cleaned output

**Presigned URL — the core concept:**

A presigned URL is an S3 URL with an embedded AWS signature computed using the API
server's IAM credentials. The browser uploads directly to S3 using this URL — no
API Gateway involvement.

```
API → generates signed URL (valid 15 min) → returns to browser
Browser → PUT file bytes to S3 directly using signed URL
S3 → validates the embedded signature → accepts the upload
```

**Why presigned URLs?**
- API Gateway has a 10 MB hard request size limit. CSV files can be 50 MB.
- The API server never touches the file bytes — no memory overhead.
- S3 handles bandwidth and storage, not Lambda.

**HeadObject** — when the browser calls `POST /uploads/{id}/complete`, the API calls
`HeadObject` to verify the file landed in S3. This returns only metadata (size, type)
without downloading the bytes.

---

### 5.2 SQS — Message Queue

SQS is a managed message queue. One service puts messages in; another reads them.
They never talk directly — the queue is the middleman.

**Upload flow:**
1. API: `send_job_message(queue_url, job_id, s3_key)` → puts `{job_id, s3_key}` into SQS.
2. SQS → triggers Worker Lambda (event source mapping — AWS connects them automatically).
3. Worker processes the job, returns. SQS deletes the message.

**At-least-once delivery** — SQS guarantees every message is delivered at least once.
In rare cases it delivers the same message twice (Lambda crash before SQS deletion).
This is why the worker uses a conditional UPDATE — only one worker can win.

**Visibility timeout (1800 seconds)** — When a worker picks up a message, SQS hides it
from others for 30 minutes. If the worker crashes, after 30 minutes SQS makes it
visible again for retry.

**Dead Letter Queue (DLQ)** — After 5 failed delivery attempts, the message moves to
the DLQ. This prevents a "poison pill" message (one that always causes the worker to
crash) from retrying forever.

**Claim-check pattern** — The SQS message contains only `{job_id, s3_key}` — not the
CSV bytes. SQS has a 256 KB message limit. The worker fetches the actual file from S3
separately.

---

### 5.3 Lambda — Serverless Function Execution

You write a function; AWS runs it in response to events. No server to manage.

**CSVForge Lambdas:**
- `csvforge-api-dev` — the FastAPI app. API Gateway triggers it on every HTTP request.
- `csvforge-worker-dev` — the CSV processor. SQS triggers it when a new job is queued.

**Cold start** — first invocation (or after ~15 min idle): AWS starts a new container,
downloads code, runs module-level Python. Takes 1–3 seconds. This includes
`create_engine(...)` and the Secrets Manager call.

**Warm invocation** — subsequent calls on the same container skip initialization. Only
the handler function runs. Fast.

**Container reuse** — AWS keeps containers warm for 5–15 minutes. The connection pool
stays open. `pool_pre_ping=True` handles connections that dropped during the freeze.

---

### 5.4 API Gateway

The front door for HTTP traffic. It:
1. Accepts HTTPS connections from browsers.
2. Validates JWTs (using Cognito as authority) — invalid token → 401, never reaches Lambda.
3. Converts HTTP requests → Lambda events (JSON).
4. Converts Lambda responses → HTTP responses.

**JWT Authorizer** — API Gateway downloads Cognito's public keys (JWKS endpoint) and
verifies the JWT signature on every request. Claims from the JWT are injected into the
request context. FastAPI reads them via `get_current_user()`.

---

### 5.5 Cognito — User Authentication

Cognito handles sign-up, sign-in, password reset, MFA, and token issuance.

**What CSVForge uses:**
- Cognito issues JWTs when users log in.
- API Gateway validates these tokens.
- The `sub` claim is the user's permanent, immutable ID.

**Why `sub` not `email`?**
Email can change. The Cognito `sub` is a UUID assigned at account creation and never
changes. All `user_id` comparisons use `sub`.

---

### 5.6 CloudFormation — Infrastructure as Code

Instead of clicking in the AWS console, you write YAML templates describing what
resources you want. CloudFormation creates, updates, or deletes them to match.

**Stack** — a named group of AWS resources created from one template. Delete the
stack and all resources are deleted together.

**Change set** — a preview of what will be created, updated, or deleted before
applying. The CI/CD pipeline creates a change set, reviews it, then applies it.

**Why IaC?** Reproducibility and version control. The same infrastructure can be
recreated from scratch in minutes. Git history shows exactly what changed and when.

---

### 5.7 RDS — Managed PostgreSQL

AWS manages OS patching, backups, failover, and hardware. You connect to an endpoint
and use it like a regular PostgreSQL instance.

CSVForge uses `db.t3.micro` — the smallest PostgreSQL instance. Single-AZ (no
automatic failover). Enough for learning; not production-grade.

---

## 6. The Django Comparison (For Your Interview)

The job description asks for Django experience. This codebase uses FastAPI, but the
concepts map directly. Know these mappings.

### 6.1 Key Concepts Side by Side

| Concept | Django | FastAPI (this codebase) |
|---------|--------|------------------------|
| URL routing | `urls.py` with `path()` | `@app.get("/path")` decorators |
| ORM | Django ORM (Active Record — `Job.objects.filter(...)`) | SQLAlchemy 2.0 (Repository pattern) |
| Middleware | List in `settings.py` → `MIDDLEWARE = [...]` | `app.add_middleware(CORSMiddleware, ...)` |
| Request/Response | `HttpRequest`, `HttpResponse` | FastAPI route functions with Pydantic models |
| Dependency injection | Not built-in (manual or third-party) | `Depends()` — first-class feature |
| Auth | `django.contrib.auth`, session-based by default | JWT-based via `get_current_user()` |
| Migrations | `python manage.py makemigrations` / `migrate` | Alembic (`alembic revision --autogenerate`) |
| Admin | Built-in `/admin` with model registration | React Admin (custom frontend) |
| Settings | `settings.py` | Environment variables + `.env` file |
| CSRF | Built-in `CsrfViewMiddleware` | Not needed with JWT Bearer tokens |

---

### 6.2 Django Default Middleware

The default Django `MIDDLEWARE` list (from a fresh project):

```python
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',       # CSRF protection
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
```

Key ones to know:
- **CsrfViewMiddleware** — protects against CSRF attacks on POST/PUT/DELETE.
- **AuthenticationMiddleware** — attaches `request.user` to every request.
- **SessionMiddleware** — enables session-based authentication.
- **SecurityMiddleware** — adds security headers (HSTS, etc.).

---

### 6.3 Django Request Lifecycle

```
Browser → Django development server (runserver) or Gunicorn (production)
    ↓
WSGIHandler (entry point)
    ↓
Middleware chain (top-down: SecurityMiddleware → SessionMiddleware → ... → route)
    ↓
URL resolver (urls.py) → matches path → calls view function or class-based view
    ↓
View function: access request.user, request.POST, request.GET, request.FILES
    ↓
Response object constructed
    ↓
Middleware chain (bottom-up: same middleware list, reverse order on response)
    ↓
HTTP Response → browser
```

---

## 7. The Complete Upload Flow (Tell This as a Story)

This is the most common deep-dive question. Know every step.

### Step 1 — Intent (POST /jobs)
- Browser calls `POST /jobs` with `{original_filename, user_email}`.
- API creates a Job in `pending_upload` status (hidden from user list).
- API calls S3 to generate a presigned PUT URL (valid 15 min, max 50 MiB).
- API returns `{job_id, upload_url}`.

### Step 2 — Upload (Browser → S3 directly)
- Browser uses the presigned URL to `PUT` the raw CSV bytes directly to S3.
- API Gateway / Lambda are not involved — no 10 MB limit applies.
- S3 validates the embedded signature and accepts the upload.

### Step 3 — Confirm (POST /uploads/{id}/complete)
- Browser calls this endpoint after the PUT succeeds.
- API calls `HeadObject` on the S3 key to verify the file actually landed.
- API verifies file size ≤ 50 MiB (using the authoritative S3 size, not the client-reported size).
- Conditional UPDATE: `pending_upload → queued` (also enforces ownership).
- API publishes `{job_id, s3_key}` to SQS.

### Step 4 — Worker Processing (SQS → Lambda)
- SQS triggers the Worker Lambda via event source mapping.
- Worker calls `claim_job_for_processing()` — atomic conditional UPDATE to `processing`.
- Worker downloads the CSV from S3 to `/tmp`.
- Worker parses, deduplicates, validates the CSV.
- Worker uploads the cleaned file to `processed/{user_id}/{job_id}/result.csv`.
- Worker calls `complete_job()` — UPDATE to `completed` with `processed_s3_key`, `row_count`, `column_names`.
- SQS deletes the message (because Lambda returned without error).

### Step 5 — Download (GET /jobs/{id}/download)
- Frontend polls `GET /jobs/{id}` until `status == "completed"`.
- Frontend calls `GET /jobs/{id}/download`.
- API generates a presigned GET URL for the processed file.
- Browser uses the URL to download directly from S3.

---

## 8. Interview Q&A — Self Test

Cover the answer, say it out loud, then check.

---

**Q: What is the default middleware in Django?**
Seven pieces: SecurityMiddleware, SessionMiddleware, CommonMiddleware, CsrfViewMiddleware,
AuthenticationMiddleware, MessageMiddleware, XFrameOptionsMiddleware. The most important
to know: Csrf (protects forms), Authentication (attaches `request.user`), Session
(cookie-based sessions).

---

**Q: What is the difference between mutable and immutable objects in Python?**
Immutable objects (int, str, tuple) cannot be changed after creation — Python creates
a new object when you "modify" one. Mutable objects (list, dict, set) can be changed
in place. Mutable default arguments in functions are a classic gotcha — use `None` as
the default and initialize inside the function.

---

**Q: What are the different HTTP methods?**
GET (read, no side effects), POST (create or trigger), PUT (replace), PATCH (partial
update), DELETE (remove). OPTIONS is used for CORS preflight. HEAD returns headers only.

---

**Q: HTTP status code categories?**
1xx: informational. 2xx: success (200 OK, 201 Created). 3xx: redirect (301 permanent,
302 temporary). 4xx: client error (400 bad request, 401 unauthorized, 403 forbidden,
404 not found, 409 conflict, 422 unprocessable). 5xx: server error (500 internal,
502 bad gateway, 503 unavailable).

---

**Q: What are HTTP headers? How do you set them?**
Key-value pairs sent with requests and responses that communicate metadata: content
type, auth tokens, CORS permissions, cache instructions. In FastAPI: `response.headers["Content-Range"] = "..."`.
In a browser fetch: `headers: { 'Authorization': 'Bearer ...' }`.

---

**Q: What is the request-response cycle?**
Browser sends HTTP request (method + URL + headers + optional body) → server processes
(routing, middleware, business logic, DB) → server sends HTTP response (status code +
headers + body) → browser renders or uses the data.

---

**Q: What is the difference between == and === (JavaScript)?**
`==` does type coercion — `"5" == 5` is `true`. `===` checks value AND type —
`"5" === 5` is `false`. Always use `===` unless you explicitly need coercion.
Python only has `==`; type coercion is not built in the same way.

---

**Q: Why do we use POST for login?**
Credentials (username/password) must be sent in the request body, not the URL. GET
has no body and would put credentials in the URL — logged by servers, stored in
browser history, leaked via Referer header. POST sends the body over HTTPS, which
encrypts it end-to-end.

---

**Q: Explain generators in Python.**
A generator is a function with `yield`. It produces values lazily, one at a time.
Calling the function returns a generator object; Python only runs the body when you
call `next()` on it. Pauses at each `yield`, resumes from that exact point on the
next `next()`. Used for: memory-efficient iteration over large datasets, resource
management (FastAPI's `get_db()`), co-routines.

---

**Q: What is list comprehension?**
A concise way to create a list by transforming or filtering an iterable:
`[expression for item in iterable if condition]`. More readable and usually faster
than a for loop with `.append()`. Same idea works for dict (`{k: v for ...}`) and
set (`{expr for ...}`) comprehensions.

---

**Q: What is optional chaining (`?.`) in JavaScript/TypeScript?**
Safely access nested properties without throwing if an intermediate value is `null`
or `undefined`. `user?.address?.city` returns `undefined` instead of throwing
`TypeError: Cannot read properties of null`. Equivalent to: `user && user.address && user.address.city`.

---

**Q: What is the spread operator?**
`...` spreads an iterable's elements in place:
```js
const a = [1, 2];
const b = [...a, 3, 4];     // [1, 2, 3, 4]  — array spread
const c = { ...obj, key: "v" }; // object spread — shallow merge
```
In Python, same concept: `[*list1, *list2]` or `{**dict1, **dict2}`.

---

**Q: What is the spread operator in Python?**
`*` for lists/tuples, `**` for dicts:
```python
a = [1, 2, 3]
b = [*a, 4, 5]     # [1, 2, 3, 4, 5]

d1 = {"a": 1}
d2 = {**d1, "b": 2}  # {"a": 1, "b": 2}
```

---

**Q: Implement Python's `split()` using `yield`.**
```python
def my_split(text, delimiter=" "):
    start = 0
    for i, char in enumerate(text):
        if char == delimiter:
            yield text[start:i]
            start = i + 1
    yield text[start:]
```

---

**Q: Group words by first letter.**
```python
words = ["apple", "banana", "avocado", "berry", "carrot", "apple", "berry"]
result = {}
for word in words:
    key = word[0]
    if key not in result:
        result[key] = []
    if word not in result[key]:
        result[key].append(word)
# → {'a': ['apple', 'avocado'], 'b': ['banana', 'berry'], 'c': ['carrot']}
```

---

**Q: Flatten a list in Python.**
```python
def flatten(lst):
    for item in lst:
        if isinstance(item, list):
            yield from flatten(item)
        else:
            yield item

list(flatten([1, [2, [3, 4]], 5]))  # → [1, 2, 3, 4, 5]
```

---

**Q: What is list concatenation in Python?**
```python
a = [1, 2, 3]
b = [4, 5]
c = a + b        # → [1, 2, 3, 4, 5]  (new list)
a += b           # a is now [1, 2, 3, 4, 5]  (in-place)
a.extend(b)      # same as += for lists
```

---

**Q: Can we improve API performance? How?**

1. **Connection pooling** — reuse DB connections instead of opening new ones per request.
2. **Caching** — Redis/Memcached for frequently read, rarely changed data.
3. **Indexes** — ensure database queries hit indexes, not full table scans.
4. **Pagination** — never return all rows; use LIMIT/OFFSET or cursor-based pagination.
5. **Async** — non-blocking I/O (async/await) for I/O-bound operations.
6. **Compression** — gzip/brotli response bodies to reduce bandwidth.
7. **CDN** — static assets from CloudFront instead of the API server.
8. **N+1 prevention** — use JOINs or eager loading instead of querying in a loop.
9. **Presigned URLs** — offload file uploads/downloads to S3 (like CSVForge does).
10. **Profiling** — measure first; optimize what's actually slow.

---

## 9. Frontend Concepts (Angular Focus for the Interview)

### 9.1 var, let, const

| | `var` | `let` | `const` |
|-|-------|-------|---------|
| Scope | Function-scoped | Block-scoped | Block-scoped |
| Hoisting | Hoisted (initialized to `undefined`) | Hoisted but not initialized (TDZ) | Hoisted but not initialized (TDZ) |
| Reassignable | Yes | Yes | No |
| Redeclarable | Yes | No | No |

Always prefer `const`. Use `let` when reassignment is needed. Never use `var` in
modern code.

---

### 9.2 Array and Object Methods

```js
// Arrays
arr.splice(1, 2)          // remove 2 elements at index 1 (mutates original)
arr.slice(1, 3)           // return elements at index 1, 2 (new array, no mutation)
arr.reduce((acc, x) => acc + x, 0)  // fold array to single value
arr.sort((a, b) => a - b) // sort in-place (ascending numbers)

// Objects
Object.keys(obj)          // array of keys
Object.values(obj)        // array of values
Object.hasOwn(obj, key)   // true if obj has own (not inherited) property key
```

---

### 9.3 Angular Core Concepts

**Angular folder structure (after `ng new`):**
```
src/
├── main.ts         ← bootstraps the app (AppComponent + AppConfig)
├── app.config.ts   ← providers: router, HttpClient, etc.
├── app.component.ts  ← root component (selector: app-root)
├── app.component.html
├── app.component.scss
└── angular.json    ← CLI configuration (build, serve, test targets)
```

**Standalone components** (Angular 17+): Each component declares its own imports.
No NgModule needed. To add a new page:
1. `ng generate component pages/my-page --standalone`
2. Add a route in `app.routes.ts`.

---

### 9.4 Data Binding

```html
<!-- Interpolation: display a property value -->
<h1>{{ title }}</h1>

<!-- Property binding: set a DOM property -->
<img [src]="imageUrl">

<!-- Event binding: respond to DOM events -->
<button (click)="onSubmit()">Submit</button>

<!-- Two-way binding: sync input and property -->
<input [(ngModel)]="username">
```

---

### 9.5 `input()` and `output()` for Component Communication

```typescript
// Child component
@Component({ selector: 'app-child' })
export class ChildComponent {
    name = input<string>('');         // receives from parent
    nameChange = output<string>();    // emits to parent

    update(value: string) {
        this.nameChange.emit(value);
    }
}

// Parent template
<app-child [name]="parentName" (nameChange)="parentName = $event" />
```

**Grandchild communication:** Use a shared service (inject the same singleton service
in both components). The service holds a `signal` or `BehaviorSubject` that both
components read and write.

---

### 9.6 Routing — Lazy Loading and Guards

```typescript
// app.routes.ts
const routes: Routes = [
    {
        path: 'dashboard',
        loadComponent: () => import('./dashboard/dashboard.component').then(m => m.DashboardComponent),
        canActivate: [AuthGuard],
    },
    { path: '**', redirectTo: 'login' }  // wildcard for unmatched routes
];
```

**Lazy loading** defers loading a component's JavaScript bundle until the user navigates
to that route. Smaller initial bundle, faster startup.

**AuthGuard** prevents navigation to routes that require authentication.
**RoleGuard** checks the user's role before allowing access.
**CanDeactivate** shows a "you have unsaved changes" dialog before leaving a route.

---

### 9.7 HTTP Interceptors

An interceptor wraps every HTTP request/response:

```typescript
export const authInterceptor: HttpInterceptorFn = (req, next) => {
    const token = inject(AuthService).token();
    const authReq = req.clone({
        headers: req.headers.set('Authorization', `Bearer ${token}`)
    });
    return next(authReq);
};
```

**Common uses:**
- Inject JWT token into every request header.
- Global error handling (show toast on 500).
- Global loader spinner on API calls.

---

### 9.8 RxJS Operators for API Calls

```typescript
// switchMap: cancel previous request if a new one arrives (search inputs)
searchInput$.pipe(
    switchMap(term => this.api.search(term))
)

// exhaustMap: ignore new requests while one is in flight (submit buttons)
submitButton$.pipe(
    exhaustMap(() => this.api.submit(form))
)

// concatMap: queue requests, process one at a time (ordered operations)
actions$.pipe(
    concatMap(action => this.api.process(action))
)

// forkJoin: wait for multiple observables to complete, return all results
forkJoin([this.api.getUsers(), this.api.getJobs()])

// takeUntilDestroyed: auto-unsubscribe when component is destroyed
this.api.getJobs().pipe(takeUntilDestroyed(this.destroyRef)).subscribe(...)
```

---

### 9.9 Observable vs Promise

| | Observable | Promise |
|-|-----------|---------|
| Values | Multiple over time (stream) | Single value |
| Lazy vs Eager | Lazy — only runs when subscribed | Eager — runs immediately |
| Cancellable | Yes — unsubscribe | No (once started) |
| Operators | Rich (map, filter, switchMap...) | Limited (.then, .catch) |
| Use case | HTTP calls, events, streams | One-shot async operations |

An Observable from `HttpClient.get()` is lazy — if you don't subscribe, the request
is never sent. A Promise runs the moment it's created.

---

### 9.10 Nullish Coalescing (`??`)

```typescript
const value = null ?? "default";    // → "default"
const value2 = 0 ?? "default";     // → 0  (0 is not null/undefined)
const value3 = 0 || "default";     // → "default"  (|| treats 0 as falsy)
```

`??` only triggers for `null` or `undefined`. `||` triggers for any falsy value
(0, "", false). Use `??` when 0 or empty string are valid values.

---

## 10. Architecture Diagram (Draw From Memory)

```
Browser (React)
    │  HTTPS + Bearer Token
    ▼
API Gateway ── validates JWT (Cognito) ── 401 if invalid
    │
    │  Lambda event (JSON)
    ▼
FastAPI (Lambda via Mangum)
    │                     │
    │  SQLAlchemy          │  boto3
    ▼                     ▼
PostgreSQL           S3 (CSV files)
(jobs table)              │
                     SQS Queue
                          │
                          │  event source mapping
                          ▼
                    Worker Lambda
                    (read S3 → clean CSV → write S3 → update DB)
```

The API never touches file bytes. PostgreSQL owns job state. S3 owns file bytes.
SQS is the trigger between API and worker.

---

## 11. Quick Reference — The 5 Key Design Decisions

| Decision | What | Why |
|----------|------|-----|
| Presigned URL | Browser uploads directly to S3 | API Gateway has 10 MB limit; files up to 50 MB |
| PostgreSQL over DynamoDB | Relational DB with ACID | Need transactions; job state must never be partially updated |
| SQS for async | Queue between API and worker | Decouples them; worker retries independently; API responds fast |
| Conditional UPDATE | Atomic claim-or-skip | SQS delivers at-least-once; two workers may race for same job |
| `pending_upload` hidden | Excluded from list endpoint | A never-uploaded job would appear as a ghost record |
