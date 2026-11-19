# CSVForge — Production Deployment & Happy Path Guide

> **Purpose:** Step-by-step deployment from scratch + code-level explanation of the
> happy path. Every step links to the exact file and line that implements it.
>
> **Audience:** A developer who knows Python, wants to understand SQLAlchemy +
> FastAPI + AWS from first principles, and can explain every piece in an interview.

---

## Table of Contents

1. [The Architecture in One Picture](#1-the-architecture-in-one-picture)
2. [File Map — What Every File Does](#2-file-map--what-every-file-does)
3. [Environment Variables — Every One Explained](#3-environment-variables--every-one-explained)
4. [Step 1 — Set Up Cognito](#4-step-1--set-up-cognito)
5. [Step 2 — Set Up RDS PostgreSQL](#5-step-2--set-up-rds-postgresql)
6. [Step 3 — Set Up S3](#6-step-3--set-up-s3)
7. [Step 4 — Set Up SQS](#7-step-4--set-up-sqs)
8. [Step 5 — Deploy the API Lambda](#8-step-5--deploy-the-api-lambda)
9. [Step 6 — Set Up API Gateway](#9-step-6--set-up-api-gateway)
10. [The Happy Path — Code Walkthrough](#10-the-happy-path--code-walkthrough)
11. [The Job State Machine](#11-the-job-state-machine)
12. [Connection Pool Deep Dive](#12-connection-pool-deep-dive)
13. [How Auth Works End-to-End](#13-how-auth-works-end-to-end)
14. [Smoke Test After Deployment](#14-smoke-test-after-deployment)
15. [Key Concepts Cheat Sheet](#15-key-concepts-cheat-sheet)

---

## 1. The Architecture in One Picture

```
                      Browser
                         │
                         │ HTTPS + JWT
                         ▼
                  API Gateway (HTTP API)
                  ┌──────────────────────────────┐
                  │ JWT Authoriser               │
                  │ validates Cognito token      │
                  │ BEFORE Lambda runs           │
                  └──────────────┬───────────────┘
                                 │ event (JSON)
                                 ▼
                  Lambda — API (FastAPI via Mangum)
                  ┌──────────────────────────────┐
                  │ backend/main.py              │
                  │ backend/auth.py              │
                  │ backend/schemas.py           │
                  │ backend/db/job_repository.py │
                  │ backend/s3_client.py         │
                  │ backend/sqs_client.py        │
                  └──────┬──────────┬────────────┘
                         │          │
                         │          │ boto3
              SQLAlchemy │          ├──────────────► S3 Bucket
              psycopg 3  │          │               (CSV files)
                         ▼          │
                  RDS PostgreSQL    └──────────────► SQS Queue
                  (jobs table)                      (job messages)
                                                        │
                                                        │ event source mapping
                                                        ▼
                                             Lambda — Worker
                                             ┌──────────────────┐
                                             │ worker/handler.py│
                                             │ reads S3         │
                                             │ cleans CSV       │
                                             │ writes S3        │
                                             │ updates DB       │
                                             └──────────────────┘
```

**Five AWS services. Each has ONE job:**

| Service | Responsibility |
|---|---|
| **Cognito** | "Who is this user?" — login, tokens, groups |
| **API Gateway** | Receives HTTPS traffic, validates JWT, forwards to Lambda |
| **Lambda (API)** | Runs FastAPI — creates jobs, issues presigned URLs |
| **S3** | Stores raw CSV uploads and processed output files |
| **SQS** | Queues jobs so the API can return immediately (async processing) |
| **Lambda (Worker)** | Reads SQS, downloads CSV, processes it, writes result back to S3 |
| **RDS PostgreSQL** | The job state machine — single source of truth for every job's status |

---

## 2. File Map — What Every File Does

```
csv-metadata/
├── backend/
│   ├── main.py              ← FastAPI app, all HTTP routes, Mangum handler
│   ├── auth.py              ← get_current_user(), require_admin() dependencies
│   ├── schemas.py           ← Pydantic request/response models
│   ├── s3_client.py         ← presigned URLs, HeadObject, upload/download
│   ├── sqs_client.py        ← send_job_message(), parse_job_message()
│   ├── db/
│   │   ├── connection.py    ← Engine, SessionLocal, Base, get_db()
│   │   └── job_repository.py← ALL SQL for jobs table (create, read, update)
│   ├── models/
│   │   └── job.py           ← Job ORM class (15 columns), JobStatus enum
│   └── alembic/
│       └── versions/
│           └── 0001_initial_jobs_table.py ← CREATE TABLE migration
├── worker/
│   └── handler.py           ← Worker Lambda entry point
├── docs/
│   └── DECISIONS.md         ← AD-01 to AD-30: why every decision was made
├── .env.example             ← template for all environment variables
└── alembic.ini              ← Alembic configuration
```

---

## 3. Environment Variables — Every One Explained

**File:** `.env.example`

```bash
# Controls which credential strategy runs
ENVIRONMENT=production
# "local"      → reads DATABASE_URL directly from .env (no AWS calls)
# "production" → calls Secrets Manager for DB password at startup

# --- Production DB (Lambda reads these at runtime) ---
DB_SECRET_ARN=arn:aws:secretsmanager:ap-south-1:123456789012:secret:csvforge-db-secret
# WHY an ARN and not the password itself?
# Lambda env vars are visible in the AWS Console — anyone with Console
# access can read them. Secrets Manager stores the password encrypted.
# The Lambda execution role has permission to call GetSecretValue on this ARN.

DB_HOST=your-rds.ap-south-1.rds.amazonaws.com
# WHY stored separately from the secret?
# The RDS endpoint isn't known until after the RDS instance is created.
# If it were inside the secret, you'd have a circular CloudFormation
# dependency: stack needs RDS to know the host, but secret is created
# as part of the same stack. See AD-24.

DB_PORT=5432          # PostgreSQL default port
DB_NAME=csvforge      # database name on RDS

# --- S3 ---
S3_BUCKET_NAME=csvforge-files-dev
# Key structure inside the bucket:
# uploads/{user_id}/{job_id}/original.csv    ← uploaded by browser
# processed/{user_id}/{job_id}/result.csv    ← written by worker Lambda

# --- SQS ---
SQS_QUEUE_URL=https://sqs.ap-south-1.amazonaws.com/123456789012/csvforge-jobs-dev

# --- Cognito ---
COGNITO_USER_POOL_ID=ap-south-1_AbC123DeF
COGNITO_APP_CLIENT_ID=1abc2def3ghi4jkl5mno

# --- CORS ---
ALLOWED_ORIGINS=https://your-frontend.s3-website.ap-south-1.amazonaws.com
# Comma-separated. Defaults to http://localhost:5173 if not set.
```

**Where the code reads them:**

```python
# File: backend/db/connection.py  (lines ~65–100)

def _build_database_url() -> str:
    environment = os.getenv("ENVIRONMENT", "production")

    if environment == "local":
        url = os.getenv("DATABASE_URL")       # reads DATABASE_URL directly
        return url

    # Production path — calls Secrets Manager
    secret_arn = os.environ["DB_SECRET_ARN"]  # ARN of the secret
    host       = os.environ["DB_HOST"]         # RDS endpoint
    port       = os.getenv("DB_PORT", "5432")
    dbname     = os.getenv("DB_NAME", "csvforge")

    client   = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    secret   = json.loads(response["SecretString"])
    # secret = {"username": "csvforge", "password": "..."}

    return f"postgresql+psycopg://{secret['username']}:{secret['password']}@{host}:{port}/{dbname}"
```

This function runs **once per Lambda cold start**. Warm invocations reuse the
already-imported module — zero extra Secrets Manager calls.

---

## 4. Step 1 — Set Up Cognito

Cognito handles user sign-up, login, and token issuance. You need three things:
a **User Pool**, an **App Client**, and an **Admin Group**.

### Create a User Pool

```bash
aws cognito-idp create-user-pool \
  --pool-name csvforge-users-dev \
  --region ap-south-1 \
  --auto-verified-attributes email \
  --policies '{"PasswordPolicy":{"MinimumLength":8}}'
```

**Output:** `UserPool.Id = ap-south-1_AbC123DeF`

This is your `COGNITO_USER_POOL_ID`. A User Pool is Cognito's managed
table of users. It handles password hashing, email verification, and MFA —
you get all of that without writing any auth code.

### Create an App Client

```bash
aws cognito-idp create-user-pool-client \
  --user-pool-id ap-south-1_AbC123DeF \
  --client-name csvforge-web-dev \
  --no-generate-secret \
  --region ap-south-1
```

`--no-generate-secret` is required for browser apps. A browser cannot keep
a secret confidential (the user can read the page source). The public client
flow authenticates by user password, not by a client secret.

**Output:** `UserPoolClient.ClientId = 1abc2def3ghi4jkl5mno`

This is your `COGNITO_APP_CLIENT_ID`.

### Create the Admin Group

```bash
aws cognito-idp create-group \
  --group-name csvforge-admins \
  --user-pool-id ap-south-1_AbC123DeF \
  --region ap-south-1
```

**Where the backend checks this group:**

```python
# File: backend/auth.py  (lines ~130–145)

groups_raw = claims.get("cognito:groups", "")
# "cognito:groups" is a space-delimited string in the JWT claims
# Example: "csvforge-admins other-group"

if isinstance(groups_raw, str):
    groups = groups_raw.split() if groups_raw else []

is_admin = "csvforge-admins" in groups
return CurrentUser(user_id=user_id, is_admin=is_admin)
```

**What the JWT access token contains:**

```json
{
  "sub": "aabb-ccdd-1122-3344",
  "cognito:groups": "csvforge-admins",
  "token_use": "access",
  "exp": 1700000000,
  "iss": "https://cognito-idp.ap-south-1.amazonaws.com/ap-south-1_AbC123DeF"
}
```

`sub` is the user's permanent UUID. It is immutable — it never changes even
if the user updates their email. **Every job row stores `user_id = sub`.**

### Why `sub` and not email?

```python
# File: backend/models/job.py  (lines ~190–200)

user_id: Mapped[str] = mapped_column(
    VARCHAR(128),
    nullable=False,
    comment="Cognito sub claim — authoritative user identity",
)
user_email: Mapped[str] = mapped_column(
    VARCHAR(255),
    nullable=False,
    comment="Stored at upload time for admin display only — not used for auth (AD-23)",
)
```

If a user changes their email, their `sub` stays the same. All their jobs
remain accessible. Using email as the ownership key would orphan their jobs
the moment they updated their email address.

---

## 5. Step 2 — Set Up RDS PostgreSQL

### Create the Secrets Manager secret first

```bash
aws secretsmanager create-secret \
  --name csvforge-db-secret \
  --secret-string '{"username":"csvforge","password":"choose-a-strong-password"}' \
  --region ap-south-1
```

Note the `ARN` from the output — this becomes `DB_SECRET_ARN`.

### Create the RDS instance

```bash
aws rds create-db-instance \
  --db-instance-identifier csvforge-dev \
  --db-instance-class db.t3.micro \
  --engine postgres \
  --engine-version 16 \
  --master-username csvforge \
  --master-user-password "choose-a-strong-password" \
  --allocated-storage 20 \
  --db-name csvforge \
  --region ap-south-1
```

Wait ~5 minutes, then get the endpoint:

```bash
aws rds describe-db-instances \
  --db-instance-identifier csvforge-dev \
  --query "DBInstances[0].Endpoint.Address" \
  --region ap-south-1
```

This is your `DB_HOST`.

### Run the Alembic migration

The `jobs` table doesn't exist yet. Alembic creates it:

```bash
ENVIRONMENT=local \
DATABASE_URL=postgresql+psycopg://csvforge:password@your-rds-host:5432/csvforge \
  alembic upgrade head
```

**What Alembic does:**

Alembic reads `backend/alembic/versions/0001_initial_jobs_table.py` and
runs the `CREATE TABLE jobs (...)` SQL against the database.

```python
# File: backend/alembic/versions/0001_initial_jobs_table.py

def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id",                VARCHAR(36),   primary_key=True, ...),
        sa.Column("user_id",           VARCHAR(128),  nullable=False),
        sa.Column("user_email",        VARCHAR(255),  nullable=False),
        sa.Column("original_filename", VARCHAR(255),  nullable=False),
        sa.Column("original_s3_key",   VARCHAR(512),  nullable=False),
        sa.Column("processed_s3_key",  VARCHAR(512),  nullable=True),
        sa.Column("status",            VARCHAR(32),   nullable=False, server_default="pending_upload"),
        sa.Column("failure_reason",    TEXT,          nullable=True),
        sa.Column("file_size_bytes",   BIGINT,        nullable=True),
        sa.Column("row_count",         INTEGER,       nullable=True),
        sa.Column("column_count",      INTEGER,       nullable=True),
        sa.Column("column_names",      ARRAY(TEXT),   nullable=True),
        sa.Column("duplicate_rows_removed", INTEGER,  nullable=True),
        sa.Column("created_at",        TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at",        TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_jobs_user_id",    "jobs", ["user_id"])
    op.create_index("idx_jobs_status",     "jobs", ["status"])
    op.create_index("idx_jobs_created_at", "jobs", ["created_at"])
```

After this, the database has the jobs table with 15 columns and 3 indexes.

**How Alembic knows about the model:**

```python
# File: backend/alembic/env.py

from backend.models.job import Job         # triggers Job class definition
from backend.db.connection import Base

target_metadata = Base.metadata
# Base.metadata is the registry of all ORM models.
# Alembic compares this Python-defined schema against the live DB schema
# and generates ALTER TABLE / CREATE TABLE to close the gap.
```

**Why 3 separate indexes (not one composite)?**

```python
# File: backend/models/job.py  (lines ~290–300)

__table_args__ = (
    Index("idx_jobs_user_id",    "user_id"),    # WHERE user_id = X
    Index("idx_jobs_status",     "status"),     # WHERE status = Y  (admin filter)
    Index("idx_jobs_created_at", "created_at"), # ORDER BY created_at DESC
)
```

Each query filters on exactly one column. A composite index like
`(user_id, created_at)` only helps queries that filter on BOTH simultaneously.
Three separate indexes cover three separate access patterns.

---

## 6. Step 3 — Set Up S3

```bash
# Create the bucket
aws s3api create-bucket \
  --bucket csvforge-files-dev \
  --region ap-south-1 \
  --create-bucket-configuration LocationConstraint=ap-south-1

# Block all public access
aws s3api put-public-access-block \
  --bucket csvforge-files-dev \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,\
BlockPublicPolicy=true,RestrictPublicBuckets=true
```

The bucket is **fully private**. No public URLs. Access is only via presigned
URLs or the Lambda execution role.

**Key naming scheme:**

```
uploads/{user_id}/{job_id}/original.csv    ← browser writes via presigned PUT URL
processed/{user_id}/{job_id}/result.csv    ← worker Lambda writes after processing
```

Including `user_id` in the prefix allows future IAM conditions like
`s3:prefix: uploads/${cognito-identity.amazonaws.com:sub}/` to scope each
user to only their own files.

---

## 7. Step 4 — Set Up SQS

```bash
# Create the queue
aws sqs create-queue \
  --queue-name csvforge-jobs-dev \
  --region ap-south-1

# Get the URL
aws sqs get-queue-url \
  --queue-name csvforge-jobs-dev \
  --region ap-south-1
```

**Output:** `https://sqs.ap-south-1.amazonaws.com/123456789012/csvforge-jobs-dev`

This becomes `SQS_QUEUE_URL`.

**Why SQS at all?**

The browser expects the `/uploads/complete` endpoint to respond in
milliseconds. Processing a 50 MB CSV could take 10–30 seconds. Without SQS:
- API would have to wait for the worker to finish → user waits 30 seconds
- If the network dropped mid-process, the work is lost

With SQS:
- API queues the job and returns `{"status": "queued"}` immediately
- Worker processes asynchronously in the background
- If the worker Lambda crashes, SQS re-delivers the message (at-least-once
  delivery)

---

## 8. Step 5 — Deploy the API Lambda

### Package the code

```bash
cd /Users/vishal/csv-metadata
pip install -r backend/requirements.txt -t package/
cp -r backend package/
cd package && zip -r ../api.zip . && cd ..
```

### Create the Lambda function

```bash
aws lambda create-function \
  --function-name csvforge-api-dev \
  --runtime python3.12 \
  --handler backend.main.handler \
  --zip-file fileb://api.zip \
  --role arn:aws:iam::123456789012:role/csvforge-api-role \
  --timeout 30 \
  --memory-size 512 \
  --region ap-south-1 \
  --environment Variables="{
    ENVIRONMENT=production,
    DB_SECRET_ARN=arn:aws:secretsmanager:...,
    DB_HOST=your-rds.ap-south-1.rds.amazonaws.com,
    DB_PORT=5432,
    DB_NAME=csvforge,
    S3_BUCKET_NAME=csvforge-files-dev,
    SQS_QUEUE_URL=https://sqs.ap-south-1.amazonaws.com/...,
    ALLOWED_ORIGINS=https://your-frontend.example.com
  }"
```

### Why the handler is named `backend.main.handler`

Lambda's Handler property resolves as:
- `backend.main` → import the module `backend/main.py`
- `handler` → call the attribute named `handler` in that module

```python
# File: backend/main.py  (last ~10 lines)

from mangum import Mangum
handler = Mangum(app)

# Mangum bridges FastAPI (ASGI) and Lambda (event/context dict).
# When Lambda calls handler(event, context):
#   1. Mangum translates the API Gateway JSON event into an ASGI request
#   2. Passes it to FastAPI's app(scope, receive, send)
#   3. FastAPI processes it (middleware → route → response)
#   4. Mangum translates the ASGI response back to Lambda's response dict
#   5. API Gateway sends the HTTP response to the browser
```

If you renamed `handler` to anything else, Lambda would raise
`Runtime.HandlerNotFound` and every request would return HTTP 502.

### IAM role the Lambda needs

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:ap-south-1:...:secret:csvforge-db-secret"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:HeadObject"],
      "Resource": "arn:aws:s3:::csvforge-files-dev/*"
    },
    {
      "Effect": "Allow",
      "Action": "sqs:SendMessage",
      "Resource": "arn:aws:sqs:ap-south-1:...:csvforge-jobs-dev"
    }
  ]
}
```

Least privilege: exactly the permissions the Lambda actually exercises.
No `s3:*`, no `sqs:*`, no `secretsmanager:*`.

---

## 9. Step 6 — Set Up API Gateway

```bash
# Create the HTTP API
aws apigatewayv2 create-api \
  --name csvforge-api-dev \
  --protocol-type HTTP \
  --cors-configuration \
    AllowOrigins="https://your-frontend.example.com",\
    AllowMethods="GET,POST,PUT,OPTIONS",\
    AllowHeaders="Authorization,Content-Type" \
  --region ap-south-1
# Output: ApiId = abc123def

# Create the JWT Authoriser — validates Cognito tokens
aws apigatewayv2 create-authorizer \
  --api-id abc123def \
  --authorizer-type JWT \
  --identity-source '$request.header.Authorization' \
  --jwt-configuration \
    Audience=YOUR_APP_CLIENT_ID,\
    Issuer=https://cognito-idp.ap-south-1.amazonaws.com/ap-south-1_AbC123DeF \
  --name CognitoAuthoriser \
  --region ap-south-1
# Output: AuthorizerId = xyz789

# Lambda integration (AWS_PROXY = pass everything through)
aws apigatewayv2 create-integration \
  --api-id abc123def \
  --integration-type AWS_PROXY \
  --integration-uri arn:aws:lambda:ap-south-1:...:function:csvforge-api-dev \
  --payload-format-version 2.0 \
  --region ap-south-1
# Output: IntegrationId = int456

# Route: everything goes through the JWT authoriser then to Lambda
aws apigatewayv2 create-route \
  --api-id abc123def \
  --route-key "ANY /{proxy+}" \
  --authorization-type JWT \
  --authorizer-id xyz789 \
  --target "integrations/int456" \
  --region ap-south-1

# Health check route — no auth required
aws apigatewayv2 create-route \
  --api-id abc123def \
  --route-key "GET /health" \
  --authorization-type NONE \
  --target "integrations/int456" \
  --region ap-south-1

# Deploy
aws apigatewayv2 create-stage \
  --api-id abc123def \
  --stage-name '$default' \
  --auto-deploy \
  --region ap-south-1
```

**Your API URL:** `https://abc123def.execute-api.ap-south-1.amazonaws.com`

**What the JWT Authoriser does (before FastAPI runs):**

1. Reads `Authorization: Bearer <token>` from the request header
2. Downloads Cognito's JWKS (public keys) — cached, not fetched every request
3. Verifies the token signature against those keys
4. Verifies `exp` (not expired), `iss` (matches your User Pool), `aud` (matches your App Client)
5. If anything fails → returns `401 Unauthorized` and Lambda never runs
6. If everything passes → injects decoded claims into `requestContext.authorizer.jwt.claims`

FastAPI **never validates the JWT itself** — that would require downloading
Cognito's JWKS on every cold start. API Gateway does it more efficiently at
the infrastructure layer.

---

## 10. The Happy Path — Code Walkthrough

Here is exactly what happens from browser click to processed CSV download.
Every step names the file, the function, and the key lines.

### Step A — User logs in (Cognito)

The browser calls Cognito's `InitiateAuth` API:

```javascript
// frontend/src/auth/AuthContext.tsx
const result = await cognitoClient.initiateAuth({
  AuthFlow: "USER_PASSWORD_AUTH",
  ClientId: COGNITO_APP_CLIENT_ID,
  AuthParameters: { USERNAME: email, PASSWORD: password },
})
const accessToken = result.AuthenticationResult.AccessToken
// Store in memory; send as "Authorization: Bearer <token>" on every API call
```

Cognito returns three tokens. The **access token** is the API credential.

---

### Step B — POST /jobs (create upload intent)

**Browser sends:**
```http
POST /jobs HTTP/1.1
Authorization: Bearer eyJhbGciOiJSUzI1Ni...
Content-Type: application/json

{"user_email": "alice@example.com", "original_filename": "data.csv", "file_size_bytes": 1048576}
```

**API Gateway validates the JWT, then calls Lambda.**

**FastAPI route handler:**

```python
# File: backend/main.py  (lines ~160–185)

@app.post("/jobs", response_model=UploadIntentResponse, status_code=201)
def create_upload_job(
    body: PostJobRequest,                         # Pydantic validates JSON body
    db: Session = Depends(get_db),                # opens DB session
    current_user: CurrentUser = Depends(get_current_user),  # extracts user identity
) -> UploadIntentResponse:
    return _create_upload_job_logic(
        user_id=current_user.user_id,             # always from JWT, never from body
        user_email=body.user_email,
        original_filename=body.original_filename,
        db=db,
    )
```

**`get_current_user` extracts identity:**

```python
# File: backend/auth.py  (lines ~110–145)

async def get_current_user(request: Request, ...) -> CurrentUser:
    environment = os.getenv("ENVIRONMENT", "production")

    if environment == "local":
        # Local dev bypass — trusts X-Dev-User-Id header
        user_id = x_dev_user_id if x_dev_user_id else "dev-user-001"
        return CurrentUser(user_id=user_id, ...)

    # Production — read claims that API Gateway already validated
    aws_event = request.scope.get("aws.event", {})
    claims = (
        aws_event
        .get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    user_id = claims.get("sub", "")     # the immutable Cognito user UUID
    # ...
    return CurrentUser(user_id=user_id, is_admin=is_admin)
```

**`get_db` opens the database session:**

```python
# File: backend/db/connection.py  (lines ~200–240)

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()     # check a connection out of the pool
    try:
        yield db            # hand the session to the route handler
        db.commit()         # on success: make all writes durable
    except Exception:
        db.rollback()       # on exception: undo all writes
        raise
    finally:
        db.close()          # always: return the connection to the pool
```

**`_create_upload_job_logic` creates the DB row and presigned URL:**

```python
# File: backend/main.py  (lines ~200–235)

def _create_upload_job_logic(user_id, user_email, original_filename, db):
    # 1. INSERT into jobs table (status = pending_upload)
    job = create_job(
        db=db,
        user_id=user_id,
        user_email=user_email,
        original_filename=original_filename,
        original_s3_key="__placeholder__",   # will be updated in step 2
    )

    # 2. Build the S3 key using the new job's UUID
    s3_key = f"uploads/{user_id}/{job.id}/original.csv"
    job.original_s3_key = s3_key              # update in-memory; committed by get_db()

    # 3. Generate a presigned PUT URL (valid 15 min, max 50 MiB)
    bucket = os.getenv("S3_BUCKET_NAME", "csvforge-files-dev")
    upload_url = generate_presigned_upload_url(bucket=bucket, key=s3_key)

    return UploadIntentResponse(job_id=job.id, upload_url=upload_url, s3_key=s3_key)
```

**`create_job` in the repository:**

```python
# File: backend/db/job_repository.py  (lines ~80–120)

def create_job(db, user_id, user_email, original_filename, original_s3_key) -> Job:
    job = Job(
        id=str(uuid.uuid4()),                      # UUID generated in Python
        user_id=user_id,
        user_email=user_email,
        original_filename=original_filename,
        original_s3_key=original_s3_key,
        status=JobStatus.PENDING_UPLOAD.value,     # "pending_upload"
    )
    db.add(job)      # register with session — no SQL yet
    db.flush()       # sends INSERT within the current transaction (not committed)
    db.refresh(job)  # re-reads the row to get server_default timestamps
    return job
```

**`generate_presigned_upload_url`:**

```python
# File: backend/s3_client.py  (lines ~110–145)

def generate_presigned_upload_url(bucket, key, expiry_seconds=900) -> str:
    client = get_s3_client()
    url = client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ContentType": "text/csv",
        },
        ExpiresIn=expiry_seconds,    # 900 seconds = 15 minutes
    )
    return url
    # Signing is LOCAL — computed with HMAC-SHA256 using the execution role's
    # secret key. No network call to S3. The browser can use this URL with a
    # PUT request; it does NOT need AWS credentials.
```

**Browser receives:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "upload_url": "https://csvforge-files-dev.s3.ap-south-1.amazonaws.com/uploads/aabb-ccdd/550e8400-.../original.csv?X-Amz-Signature=...",
  "s3_key": "uploads/aabb-ccdd/550e8400-.../original.csv"
}
```

---

### Step C — Browser uploads directly to S3

```javascript
// frontend/src/components/UploadForm.tsx
await fetch(uploadUrl, {
  method: "PUT",
  headers: { "Content-Type": "text/csv" },
  body: file,                            // raw file bytes
})
// No Authorization header needed — the presigned URL IS the auth
```

**FastAPI is NOT involved.** The file goes browser → S3 directly.
This avoids API Gateway's 10 MB payload limit and Lambda memory usage.

The presigned URL is a bearer token: it embeds the IAM signature, so S3
verifies the request without calling IAM. Valid for 15 minutes only.

---

### Step D — POST /uploads/{job_id}/complete

After the S3 PUT succeeds, the browser calls:

```http
POST /uploads/550e8400-e29b-41d4-a716-446655440000/complete
Authorization: Bearer eyJhbGciOiJSUzI1Ni...
```

```python
# File: backend/main.py  (lines ~240–310)

@app.post("/uploads/{job_id}/complete", response_model=UploadCompleteResponse)
def upload_complete(job_id: str, db, current_user):

    # 1. Fetch the job (admin fetch to get the S3 key)
    job = get_job_by_id(db=db, job_id=job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    bucket = os.getenv("S3_BUCKET_NAME", "csvforge-files-dev")

    # 2. Verify the file actually exists in S3
    if not object_exists(bucket=bucket, key=job.original_s3_key):
        raise HTTPException(status_code=404, detail="Uploaded file not found in S3")
    # object_exists() calls HeadObject — fetches metadata only, zero bytes downloaded
    # S3 has strong read-after-write consistency since Dec 2020:
    # a file PUT one second ago is immediately visible to HeadObject

    # 3. Verify file size ≤ 50 MiB (authoritative check from S3)
    MAX_UPLOAD_BYTES = 52_428_800  # 50 × 1024 × 1024
    actual_size = get_object_size(bucket=bucket, key=job.original_s3_key)
    if actual_size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=422, detail="File too large")
    # We use S3's ContentLength (authoritative), NOT the client-reported
    # file_size_bytes from the intent request. Clients can lie. See AD-27.

    # 4. Atomic conditional UPDATE: pending_upload → queued
    transitioned = transition_to_queued(
        db=db, job_id=job_id, user_id=current_user.user_id
    )
    if not transitioned:
        raise HTTPException(status_code=409, detail="Job not in pending_upload status")

    # 5. Publish SQS message (tells worker: "this job is ready")
    queue_url = os.getenv("SQS_QUEUE_URL", "...")
    send_job_message(queue_url=queue_url, job_id=job_id, s3_key=job.original_s3_key)

    return UploadCompleteResponse(job_id=job_id, status="queued")
```

**`transition_to_queued` — the atomic state transition:**

```python
# File: backend/db/job_repository.py  (lines ~260–295)

def transition_to_queued(db, job_id, user_id) -> bool:
    stmt = (
        update(Job)
        .where(
            Job.id == job_id,
            Job.user_id == user_id,                        # ownership check
            Job.status == JobStatus.PENDING_UPLOAD.value,  # state check
        )
        .values(status=JobStatus.QUEUED.value)
        .execution_options(synchronize_session="evaluate")
    )
    result = db.execute(stmt)
    return result.rowcount == 1
    # rowcount = 1 → all three WHERE conditions matched → success
    # rowcount = 0 → job doesn't exist, wrong owner, or already past pending_upload
    # This is ONE SQL statement. The check and the update are atomic.
    # No race condition is possible — the database row lock ensures only
    # one UPDATE can change the status at a time.
```

**`send_job_message`:**

```python
# File: backend/sqs_client.py  (lines ~100–130)

def send_job_message(queue_url, job_id, s3_key) -> str:
    client = get_sqs_client()
    body = json.dumps({"job_id": job_id, "s3_key": s3_key})
    # Only two fields — no file content. This is the "claim check" pattern:
    # the message contains only a reference (the S3 key), not the data.
    # The worker fetches the actual file from S3 using the key.
    # SQS has a 256 KB message size limit — a 50 MB CSV would never fit anyway.
    response = client.send_message(QueueUrl=queue_url, MessageBody=body)
    return response["MessageId"]
```

---

### Step E — Worker Lambda processes the job

SQS triggers the Worker Lambda with the message. The event looks like:

```json
{
  "Records": [{
    "body": "{\"job_id\": \"550e8400-...\", \"s3_key\": \"uploads/.../original.csv\"}",
    "receiptHandle": "AQEBwJnKyrHigU...",
    "messageId": "1234-5678-..."
  }]
}
```

```python
# File: worker/handler.py

def handler(event, context):
    for record in event["Records"]:
        # 1. Parse the SQS message
        message = parse_job_message(record["body"])
        job_id = message["job_id"]
        s3_key = message["s3_key"]

        # 2. Atomic conditional UPDATE: queued → processing
        claimed = claim_job_for_processing(db, job_id)
        if not claimed:
            # Job is completed or failed — SQS re-delivered a message.
            # Idempotency guard: skip safely.
            continue

        # 3. Download the raw CSV from S3
        download_file(bucket, s3_key, "/tmp/input.csv")

        # 4. Process: read CSV, deduplicate, count columns
        # (pandas / csv module work here)

        # 5. Write the result to S3
        processed_key = f"processed/{user_id}/{job_id}/result.csv"
        upload_file(bucket, processed_key, "/tmp/result.csv")

        # 6. Mark job completed in PostgreSQL
        complete_job(
            db, job_id,
            processed_s3_key=processed_key,
            row_count=row_count,
            column_names=column_names,
            ...
        )
```

**`claim_job_for_processing` — idempotency guard:**

```python
# File: backend/db/job_repository.py  (lines ~310–375)

def claim_job_for_processing(db, job_id) -> bool:
    stmt = (
        update(Job)
        .where(
            Job.id == job_id,
            Job.status.in_([JobStatus.QUEUED.value, JobStatus.PROCESSING.value]),
        )
        .values(status=JobStatus.PROCESSING.value)
        .execution_options(synchronize_session=False)
    )
    result = db.execute(stmt)
    db.expire_all()   # flush Python in-memory cache; next read hits DB
    return result.rowcount == 1
```

Why `in_(['queued', 'processing'])` instead of just `'queued'`?

SQS delivers at-least-once. If the Lambda timed out mid-processing, the job
is already in `'processing'`. On retry, we want to re-claim it. Including
`'processing'` allows the retry to take ownership and continue.

---

### Step F — Browser polls for status

```python
# File: backend/main.py  (lines ~340–370)

@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, db, current_user):
    job = get_job_by_id_and_user(
        db=db, job_id=job_id, user_id=current_user.user_id
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_response(job)
```

```python
# File: backend/db/job_repository.py  (lines ~155–165)

def get_job_by_id_and_user(db, job_id, user_id):
    stmt = select(Job).where(Job.id == job_id, Job.user_id == user_id)
    return db.execute(stmt).scalar_one_or_none()
    # Returns None for BOTH "not found" AND "wrong owner"
    # → route returns 404 in both cases
    # → prevents enumeration: attacker learns nothing from a failed request
    # This is AD-13.
```

---

### Step G — Download the processed CSV

```python
# File: backend/main.py  (lines ~375–430)

@app.get("/jobs/{job_id}/download", response_model=JobDownloadResponse)
def download_job(job_id, db, current_user):
    job = get_job_by_id_and_user(db, job_id, current_user.user_id)
    if job is None:
        raise HTTPException(status_code=404)

    if job.status != JobStatus.COMPLETED.value:
        raise HTTPException(status_code=409, detail="Job not completed yet")

    bucket = os.getenv("S3_BUCKET_NAME", "csvforge-files-dev")
    download_url = generate_presigned_download_url(
        bucket=bucket,
        key=job.processed_s3_key,
        expiry_seconds=900,                    # 15 minutes
    )

    # Derive a friendly filename: "data.csv" → "data_processed.csv"
    base, ext = job.original_filename.rsplit(".", 1)
    filename = f"{base}_processed.{ext}"

    return JobDownloadResponse(
        job_id=job_id,
        download_url=download_url,
        filename=filename,
        expires_in_seconds=900,
    )
```

The browser downloads the file directly from S3 — same pattern as the upload.
FastAPI issues the signed URL; S3 delivers the bytes. No Lambda memory used.

---

## 11. The Job State Machine

```python
# File: backend/models/job.py  (lines ~100–145)

class JobStatus(str, enum.Enum):
    PENDING_UPLOAD = "pending_upload"   # row created; S3 upload not yet confirmed
    QUEUED         = "queued"           # S3 confirmed; SQS message published
    PROCESSING     = "processing"       # worker claimed the job
    COMPLETED      = "completed"        # worker finished; results stored
    FAILED         = "failed"           # permanent error; failure_reason explains why
```

```
pending_upload
       │
       │  POST /uploads/{id}/complete
       │  transition_to_queued()
       │  WHERE status='pending_upload' AND user_id=:user
       ▼
    queued
       │
       │  Worker: claim_job_for_processing()
       │  WHERE status IN ('queued', 'processing')
       ▼
  processing
       │
  ┌────┴─────┐
  │          │
  ▼          ▼
completed  failed
```

**State stored as VARCHAR(32) not PostgreSQL ENUM. Why?**

Adding a new value to a PostgreSQL ENUM requires `ALTER TYPE` which acquires
an `ACCESS EXCLUSIVE` lock on the table — no reads or writes during migration.
`VARCHAR(32)` lets you add new values by deploying new code, zero downtime.

---

## 12. Connection Pool Deep Dive

```python
# File: backend/db/connection.py  (lines ~105–160)

engine = create_engine(
    DATABASE_URL,
    pool_size=1,         # one connection per Lambda container
    max_overflow=0,      # no extra connections under burst
    pool_pre_ping=True,  # probe with SELECT 1 before use
)
```

**Why `pool_size=1`?**

Lambda handles **one request at a time per container**. There is no parallelism
inside a container. At 500 concurrent Lambda invocations:

```
500 containers × pool_size=1 = 500 open PostgreSQL connections
500 containers × pool_size=5 = 2500 open PostgreSQL connections
```

RDS t3.micro allows ~100–200 connections. `pool_size=1` keeps the count
proportional to actual concurrency and within the RDS limit.

**Why `pool_pre_ping=True`?**

Lambda containers can be frozen for minutes (idle, no traffic). AWS NAT
gateways drop idle TCP connections after ~350 seconds. When the container
wakes up, the connection pool believes it has a valid connection — but the
TCP socket is dead.

Without `pool_pre_ping`: the stale connection is used → SQL fails → user
sees `500 Internal Server Error`.

With `pool_pre_ping=True`: before every use, SQLAlchemy sends `SELECT 1`:
- If PostgreSQL responds → connection alive → proceed
- If probe fails → discard the stale connection, open a fresh one → proceed

Cost: one extra 1 ms round-trip per request. Worth it.

**Why `expire_on_commit=False`?**

```python
# File: backend/db/connection.py  (lines ~175–215)

SessionLocal = sessionmaker(
    bind=engine,
    expire_on_commit=False,   # keep attribute values readable after commit
)
```

Default SQLAlchemy behaviour after `db.commit()`: marks every attribute of
every ORM object as "expired". The next access triggers a `SELECT` to reload.

Problem: in Lambda, the sequence after commit is:
1. Route handler commits
2. FastAPI serialises `job.status` to JSON
3. `get_db()` closes the session

If attributes are expired after step 1, step 2 tries to access `job.status`
→ SQLAlchemy issues a `SELECT` → but the session is mid-close → raises
`DetachedInstanceError`.

`expire_on_commit=False` keeps in-memory values readable after commit. Safe
because each Lambda request gets a fresh session — there is no "next request"
that could read stale data from this session.

---

## 13. How Auth Works End-to-End

```
Browser
  │  Authorization: Bearer eyJhbGciOiJSUzI1Ni...
  │
  ▼
API Gateway — JWT Authoriser
  │  1. Fetches Cognito JWKS (public keys) — cached, not fetched every request
  │  2. Verifies signature with the matching public key
  │  3. Verifies exp (not expired)
  │  4. Verifies iss == https://cognito-idp.ap-south-1.amazonaws.com/ap-south-1_AbC123DeF
  │  5. Verifies aud == YOUR_APP_CLIENT_ID
  │  On failure → 401 Unauthorized (Lambda NEVER runs)
  │  On success → adds decoded claims to requestContext
  │
  ▼
Mangum (ASGI adapter in backend/main.py)
  │  Puts the full API Gateway event at:
  │      request.scope["aws.event"]
  │
  ▼
FastAPI — Depends(get_current_user)   [backend/auth.py]
  │  claims = scope["aws.event"]["requestContext"]["authorizer"]["jwt"]["claims"]
  │  user_id = claims["sub"]            ← immutable user UUID
  │  groups  = claims["cognito:groups"] ← "csvforge-admins other-group"
  │  is_admin = "csvforge-admins" in groups
  │  returns CurrentUser(user_id=..., is_admin=...)
  │
  ▼
Route handler
  │  Uses current_user.user_id in every DB query:
  │  WHERE user_id = current_user.user_id
  │
  ▼
PostgreSQL: ownership-scoped query
```

**FastAPI never validates the JWT.** API Gateway does. If FastAPI re-validated
the JWT, it would need to download Cognito's JWKS on every cold start (~100 ms).
API Gateway caches the JWKS and validates efficiently at the infrastructure layer.

**Local dev bypass** (safe because it only runs when `ENVIRONMENT=local`):

```python
# File: backend/auth.py  (lines ~100–115)

if environment == "local":
    user_id = x_dev_user_id if x_dev_user_id else "dev-user-001"
    is_admin = x_dev_is_admin.lower() in ("true", "1", "yes")
    return CurrentUser(user_id=user_id, is_admin=is_admin)
```

In production, `ENVIRONMENT` is always set to `"production"` via Lambda env vars.
This bypass never executes in production.

---

## 14. Smoke Test After Deployment

Run these in order after completing all setup steps:

```bash
# ── 1. Health check (no auth) ─────────────────────────────────────────────────
curl https://YOUR_API_ID.execute-api.ap-south-1.amazonaws.com/health
# Expected: {"status":"ok","environment":"production"}

# ── 2. Get a Cognito access token ─────────────────────────────────────────────
TOKEN=$(aws cognito-idp initiate-auth \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=test@example.com,PASSWORD=your-password \
  --client-id YOUR_APP_CLIENT_ID \
  --region ap-south-1 \
  --query "AuthenticationResult.AccessToken" \
  --output text)

# ── 3. Create a job (get presigned upload URL) ─────────────────────────────────
RESPONSE=$(curl -s -X POST \
  https://YOUR_API_ID.execute-api.ap-south-1.amazonaws.com/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"user_email":"test@example.com","original_filename":"test.csv","file_size_bytes":100}')

JOB_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
UPLOAD_URL=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['upload_url'])")
echo "Job ID: $JOB_ID"

# ── 4. Upload a CSV directly to S3 ────────────────────────────────────────────
echo "name,age\nAlice,30\nBob,25\nAlice,30" > /tmp/test.csv
curl -X PUT "$UPLOAD_URL" \
  -H "Content-Type: text/csv" \
  --data-binary @/tmp/test.csv
# Expected: HTTP 200 (empty body from S3)

# ── 5. Confirm the upload ─────────────────────────────────────────────────────
curl -s -X POST \
  https://YOUR_API_ID.execute-api.ap-south-1.amazonaws.com/uploads/$JOB_ID/complete \
  -H "Authorization: Bearer $TOKEN"
# Expected: {"job_id":"...","status":"queued"}

# ── 6. Poll until completed ────────────────────────────────────────────────────
curl -s \
  https://YOUR_API_ID.execute-api.ap-south-1.amazonaws.com/jobs/$JOB_ID \
  -H "Authorization: Bearer $TOKEN"
# Poll until status = "completed"
# Then: row_count, column_names, duplicate_rows_removed will be populated

# ── 7. Get download URL ────────────────────────────────────────────────────────
curl -s \
  https://YOUR_API_ID.execute-api.ap-south-1.amazonaws.com/jobs/$JOB_ID/download \
  -H "Authorization: Bearer $TOKEN"
# Expected: {"download_url":"https://s3...","filename":"test_processed.csv","expires_in_seconds":900}

# ── 8. Download the processed file ────────────────────────────────────────────
# Use the download_url from step 7
curl -L "DOWNLOAD_URL_FROM_STEP_7" -o /tmp/test_processed.csv
cat /tmp/test_processed.csv
# Expected: deduplicated CSV (Alice row appears once, not twice)
```

---

## 15. Key Concepts Cheat Sheet

| Concept | File | One-line explanation |
|---|---|---|
| **Presigned URL** | `backend/s3_client.py` | Signed, time-limited S3 URL — browser uploads/downloads without AWS credentials |
| **JWT (access token)** | `backend/auth.py` | Signed JSON blob Cognito issues after login — API Gateway verifies signature |
| **`sub` claim** | `backend/auth.py`, `backend/models/job.py` | Cognito's immutable user UUID — the only authoritative identity field |
| **Connection pool** | `backend/db/connection.py` | Pre-opened DB connections reused across requests — avoids ~50ms TCP handshake |
| **`pool_pre_ping`** | `backend/db/connection.py` | Sends `SELECT 1` before each connection use — catches stale frozen-Lambda sockets |
| **`pool_size=1`** | `backend/db/connection.py` | One connection per Lambda container — keeps total connections ≤ RDS max_connections |
| **`expire_on_commit=False`** | `backend/db/connection.py` | ORM objects keep attribute values after commit — prevents `DetachedInstanceError` |
| **Conditional UPDATE** | `backend/db/job_repository.py` | `WHERE status=X` check and update atomically in ONE SQL — no race condition |
| **`rowcount`** | `backend/db/job_repository.py` | How many rows matched the WHERE clause — `0` means another worker got there first |
| **Claim check pattern** | `backend/sqs_client.py` | SQS message contains only job ID + S3 key, not file bytes — worker fetches file separately |
| **At-least-once delivery** | `backend/db/job_repository.py` | SQS may deliver same message twice — idempotent `claim_job_for_processing` makes this safe |
| **Mangum** | `backend/main.py` | Bridges FastAPI (ASGI) and Lambda (event/context) — same app runs locally and on Lambda |
| **Repository pattern** | `backend/db/job_repository.py` | All SQL for jobs table in one module — routes never write raw SQL |
| **`get_db()` generator** | `backend/db/connection.py` | FastAPI dependency that opens/commits/closes DB session per request via `yield` |
| **`flush()` vs `commit()`** | `backend/db/job_repository.py` | flush = "write in pencil within transaction"; commit = "sign in ink, visible to all" |
| **`expire_all()`** | `backend/db/job_repository.py` | Clears SQLAlchemy's in-memory object cache — forces fresh SELECT after bulk UPDATE |
| **AD-13 (404 for wrong owner)** | `backend/main.py` | 404 for both "not found" and "wrong owner" — prevents enumeration attacks |
| **AD-23 (sub not email)** | `backend/auth.py`, `backend/models/job.py` | Email can change; sub is immutable — sub is the only identity used for ownership |
| **AD-27 (server-side size check)** | `backend/main.py`, `backend/s3_client.py` | File size from S3 HeadObject, not client claim — clients can lie |
| **TIMESTAMPTZ vs TIMESTAMP** | `backend/models/job.py` | TIMESTAMPTZ stores UTC internally — safe regardless of server timezone |
