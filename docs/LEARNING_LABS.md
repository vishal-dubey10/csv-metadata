# CSVForge Learning Labs

Index of all practical labs. Each lab is a hands-on exercise using the real codebase.
**A lab is not complete until you have evidence (command output, test result, or working UI)
and you can explain what you did without looking at the code.**

---

## How to use this file

1. Pick the next lab from the **Sequence** section.
2. Read the **Objective** — one sentence describing what you'll understand by the end.
3. Follow the **Steps** exactly. Do not skip ahead.
4. Record your **Evidence** in the space provided.
5. Answer the **Recall questions** without looking at the code.
6. Mark the lab complete only when you've answered the recall questions correctly.

---

## Sequence

### Track B — Backend and Database (Days 1–4)

| # | Lab | Status | Evidence |
|---|-----|--------|----------|
| B-01 | Start the local environment | ☐ | |
| B-02 | Trace FastAPI startup and the health endpoint | ☐ | |
| B-03 | Inspect the Job ORM model | ☐ | |
| B-04 | Read the connection.py Engine and Session | ☐ | |
| B-05 | Create a job via the API and inspect the DB row | ☐ | |
| B-06 | Trace the upload flow end-to-end (local mock) | ☐ | |
| B-07 | Inspect the conditional UPDATE idempotency guard | ☐ | |
| B-08 | Test authorization — ownership enforcement | ☐ | |
| B-09 | Read the JWT auth dependency | ☐ | |
| B-10 | Introduce a controlled failure — invalid status transition | ☐ | |

### Track C — Worker and Distributed Systems (Day 2–3)

| # | Lab | Status | Evidence |
|---|-----|--------|----------|
| C-01 | Run CSV processor unit tests | ☐ | |
| C-02 | Trace the Lambda handler pipeline | ☐ | |
| C-03 | Inspect the SQS message format | ☐ | |
| C-04 | Verify S3-before-DB invariant | ☐ | |
| C-05 | Simulate a permanent vs transient failure | ☐ | |

### Track A — React Frontend (Day 5)

| # | Lab | Status | Evidence |
|---|-----|--------|----------|
| A-01 | Start the frontend dev server | ☐ | |
| A-02 | Trace the upload wizard state machine | ☐ | |
| A-03 | Inspect the API client and token injection | ☐ | |
| A-04 | Watch polling in the browser DevTools | ☐ | |
| A-05 | Inspect the auth context and dev bypass | ☐ | |

### Track D — AWS Infrastructure (Day 7+)

| # | Lab | Status | Evidence |
|---|-----|--------|----------|
| D-01 | Audit the AWS account and existing resources | ☐ | |
| D-02 | Read the network.yaml VPC and explain it | ☐ | |
| D-03 | Deploy the network stack and verify outputs | ☐ | |
| D-04 | Deploy the data and queues stacks | ☐ | |
| D-05 | Provision RDS and run the Alembic migration | ☐ | |
| D-06 | Deploy the API stack and test /health | ☐ | |
| D-07 | Deploy the worker stack and push the ECR image | ☐ | |
| D-08 | Run the end-to-end smoke test | ☐ | |
| D-09 | Inspect CloudWatch logs for a Lambda invocation | ☐ | |
| D-10 | Simulate a DLQ scenario | ☐ | |

---

## Lab B-01 — Start the local environment

**Objective:** Confirm the local stack starts and the health endpoint responds correctly.

**Files:** `backend/main.py`, `backend/db/connection.py`, `docker-compose.yml`

**Steps:**

```bash
# 1. Start PostgreSQL
docker compose up -d

# 2. Confirm it's running
docker compose ps

# 3. Run Alembic migration (creates the jobs table)
PYTHONPATH=. .venv/bin/alembic upgrade head

# 4. Start the API server
PYTHONPATH=. ENVIRONMENT=local .venv/bin/uvicorn backend.main:app --reload

# 5. In a new terminal — hit the health endpoint
curl http://localhost:8000/health
```

**Prediction before step 5:** What JSON will the health endpoint return?

**Evidence to record:**
```
# Paste the curl output here
```

**Recall questions (answer without looking):**
1. What does `PYTHONPATH=.` do and why is it needed?
2. What is `ENVIRONMENT=local` changing in `connection.py`?
3. What did `alembic upgrade head` actually do to the database?
4. The health endpoint has no `Depends(get_db)` or auth. Why is that correct?

---

## Lab B-02 — Trace FastAPI startup and the health endpoint

**Objective:** Understand what runs at module import time vs at request time.

**Files:** `backend/main.py`, `backend/db/connection.py`

**Steps:**

```bash
# Look at what happens when backend.main is imported
# (Trace the import chain: main.py → connection.py → _build_database_url() → engine)

# Create a job via the API with the local dev header
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -H "X-Dev-User-Id: user-1" \
  -H "X-Dev-User-Email: user1@example.com" \
  -d '{"user_email":"user1@example.com","original_filename":"test.csv","file_size_bytes":100}'
```

**Questions to answer:**
1. Name three things that happen at module import time in `connection.py`.
2. Name two things that happen per-request in `get_db()`.
3. Why is the Engine created at module level instead of inside `get_db()`?

**Failure experiment:**
```bash
# Temporarily change .env to use a wrong DATABASE_URL
# (e.g., wrong port: 5433 instead of 5432)
# Then restart uvicorn. What error do you see and when?
```

---

## Lab B-03 — Inspect the Job ORM model

**Objective:** Be able to explain every column, its SQL type, and why it exists.

**Files:** `backend/models/job.py`

**Steps:**

```bash
# After creating a job in B-01, inspect the raw row in PostgreSQL
docker exec -it $(docker compose ps -q postgres) psql -U postgres -d csvforge

# In psql:
\d jobs                    -- show column definitions
SELECT * FROM jobs \x\g    -- show all columns expanded
SELECT id, status, original_s3_key, created_at FROM jobs LIMIT 5;
\q
```

**Recall questions:**
1. Why is `id` a UUID string instead of a BIGSERIAL integer?
2. What is the difference between `server_default` and `default` in SQLAlchemy?
3. Why does `column_names` use `ARRAY(TEXT).with_variant(TEXT, "sqlite")`?
4. What does `onupdate=func.now()` do, and when does it NOT fire?
5. Why VARCHAR(32) for `status` instead of a PostgreSQL native ENUM type?

---

## Lab B-04 — Read connection.py — Engine, Session, get_db

**Objective:** Explain connection pooling and the per-request session lifecycle.

**Files:** `backend/db/connection.py`

**Read the file in full, then answer without looking:**

1. What is a connection pool and why does Lambda use `pool_size=1`?
2. What does `pool_pre_ping=True` protect against?
3. What is `expire_on_commit=False` and when would it cause a `DetachedInstanceError`?
4. Draw the execution order of `get_db()` for a successful request:
   `SessionLocal()` → `yield db` → ... → `db.commit()` → `db.close()`
5. What happens to the session if the route handler raises an exception?

---

## Lab B-05 — Create a job via the API and inspect the DB row

**Objective:** Trace a POST request through FastAPI, Pydantic, the repository, and PostgreSQL.

**Steps:**

```bash
# Create a job
RESPONSE=$(curl -s -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -H "X-Dev-User-Id: user-1" \
  -d '{"user_email":"user1@test.com","original_filename":"data.csv","file_size_bytes":1024}')
echo $RESPONSE | python3 -m json.tool

JOB_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
echo "Job ID: $JOB_ID"

# Inspect the row in PostgreSQL
docker exec -it $(docker compose ps -q postgres) psql -U postgres -d csvforge \
  -c "SELECT id, status, user_id, original_s3_key, created_at FROM jobs WHERE id='$JOB_ID';"

# Get the job back via the API
curl -s "http://localhost:8000/jobs/$JOB_ID" \
  -H "X-Dev-User-Id: user-1" | python3 -m json.tool
```

**Recall questions:**
1. What was the status of the job immediately after creation? Why?
2. Is the `upload_url` in the response a real working URL if you haven't configured S3?
3. The response has `job_id` but the DB column is `id`. Where does the name translation happen?

---

## Lab B-06 — Trace the upload flow end-to-end (local mock)

**Objective:** Understand what both upload endpoints do and why two steps are needed.

**Steps:**

```bash
# Run the upload endpoint tests to see the full expected behavior
PYTHONPATH=. .venv/bin/pytest tests/unit/test_upload_endpoints.py -v

# Then trace a complete request manually using the already-created job:
# Try to call /complete without an S3 upload. What happens?
curl -s -X POST "http://localhost:8000/uploads/$JOB_ID/complete" \
  -H "X-Dev-User-Id: user-1" | python3 -m json.tool
```

**Expected:** a 404 because there's no real S3 object. This is the correct behavior.

**Recall questions:**
1. The completion endpoint calls `object_exists()` and `get_object_size()`. Why both?
2. What does `transition_to_queued()` return and what does the return value mean?
3. What is the difference between the job status immediately after `/jobs` vs after `/complete`?

---

## Lab B-07 — Inspect the conditional UPDATE idempotency guard

**Objective:** Understand why the worker uses a conditional UPDATE instead of SELECT + UPDATE.

**Files:** `backend/db/job_repository.py` — `claim_job_for_processing()`

**Read the function and its docstring completely, then answer:**

1. Write the SQL that `claim_job_for_processing()` sends to the database.
2. Why does `rowcount == 0` mean "don't process this job"?
3. What happens if two workers call `claim_job_for_processing()` at the same time on a `queued` job?
4. Why is `processing` included in the `IN` clause — not just `queued`?
5. Why does the function call `db.expire_all()` after the UPDATE?
6. What is `synchronize_session=False` doing here versus `"evaluate"` in `transition_to_queued`?

---

## Lab B-08 — Test authorization and ownership enforcement

**Objective:** Verify that user A cannot access user B's jobs.

**Steps:**

```bash
# Create a job as user-1
JOB_ID=$(curl -s -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -H "X-Dev-User-Id: user-1" \
  -d '{"user_email":"u1@test.com","original_filename":"a.csv","file_size_bytes":100}' | \
  python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

echo "Job created: $JOB_ID"

# Try to access it as user-2
curl -s "http://localhost:8000/jobs/$JOB_ID" \
  -H "X-Dev-User-Id: user-2" | python3 -m json.tool

# Expected: 404 not 403 — explain why
```

**Recall questions:**
1. What SQL does `get_job_by_id_and_user()` run? Write the WHERE clause.
2. Why does the endpoint return 404 instead of 403 when user-2 tries to access user-1's job?
3. Where in `main.py` is `current_user.user_id` used to enforce ownership?
4. What is an IDOR vulnerability? How does CSVForge prevent it?

---

## Lab C-01 — Run CSV processor unit tests

**Objective:** Understand what the CSV processor validates and what it cleans.

```bash
PYTHONPATH=. .venv/bin/pytest tests/unit/test_csv_processor.py -v
```

Look at the test names and group them mentally:
- Tests for `parse_and_analyse()` — what validation errors does it detect?
- Tests for `clean()` — what does trimming and deduplication do?
- Tests for `write_csv()` — what format does the output use?

**Recall questions:**
1. What makes a CSV "permanently invalid" (raises `InvalidCSVError`)?
2. What is the difference between a permanent failure and a transient failure in the worker?
3. When does the worker return cleanly (no exception)? When does it re-raise?

---

## Lab C-03 — Inspect the SQS message format

**Objective:** Understand the claim-check pattern and why the message is small.

**Files:** `backend/sqs_client.py`

**Steps:**

```bash
# Run the SQS client tests
PYTHONPATH=. .venv/bin/pytest tests/unit/test_sqs_client.py -v

# Read send_job_message() and parse_job_message() carefully
```

**Recall questions:**
1. What exactly is in the SQS message body? Why not include the file contents?
2. What is the claim-check pattern?
3. What is the difference between the SQS MessageId and the CSVForge job_id?
4. What happens if `parse_job_message()` receives malformed JSON? Should it catch the error?

---

## Lab A-01 — Start the frontend dev server

**Objective:** Confirm the frontend starts, the dev auth bypass works, and you can see the job list.

```bash
cd /Users/vishal/csv-metadata/frontend
npm run dev
```

Open http://localhost:5173 in a browser.

The app should log you in automatically (local dev bypass) as `dev-user-001`.
Navigate to the upload page, the job list page, and the admin page.

**DevTools exercise:**
1. Open Network tab.
2. Navigate to the job list page.
3. Find the `GET /jobs` request. Look at the Request Headers — do you see `X-Dev-User-Id`?
4. Look at the Response — what shape does the JSON have?

---

## Lab D-01 — Audit the AWS account and existing resources

**Objective:** Know what is already deployed before creating anything new.

**Steps — read only, no changes:**

```bash
# Check what CloudFormation stacks exist
aws cloudformation list-stacks \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
  --query "StackSummaries[*].[StackName,StackStatus,CreationTime]" \
  --output table \
  --region ap-south-1

# Check for existing S3 buckets with csvforge prefix
aws s3 ls | grep csvforge

# Check for existing SQS queues
aws sqs list-queues --queue-name-prefix csvforge --region ap-south-1

# Check for existing Lambda functions
aws lambda list-functions \
  --query "Functions[?starts_with(FunctionName,'csvforge')].FunctionName" \
  --output text \
  --region ap-south-1

# Check for existing RDS instances
aws rds describe-db-instances \
  --query "DBInstances[*].[DBInstanceIdentifier,DBInstanceStatus]" \
  --output table \
  --region ap-south-1
```

**Record what you find:**
```
Stacks already deployed:

S3 buckets already existing:

SQS queues already existing:

Lambda functions already deployed:

RDS instances:
```

**Recall questions:**
1. Why do we check before creating? What could go wrong if we create a bucket that already exists?
2. What is a CloudFormation stack and why does it matter for cleanup?

---

## Progress tracking template

Copy this block after completing each lab:

```
Lab: ___________
Date: ___________
Evidence: ___________
Tests run: ___________
Recall score: ___ / ___ correct
Concepts still unclear: ___________
Next lab: ___________
```
