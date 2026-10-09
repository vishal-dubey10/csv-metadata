# Day 3 Learning Guide — CSV Processing Pipeline and Worker Lambda

This guide explains the complete CSV processing pipeline built in Day 3.
After reading it, you should be able to explain every step from S3 upload to processed output,
every failure mode, and every architectural trade-off — without looking at the code.

---

## 1. What Day 3 Built

Day 3 completed the CSV processing worker. The Day 1/2 skeleton already had the Lambda handler
structure (`worker/handler.py`). Day 3 produced the fully operational version with:

- `worker/csv_processor.py` — parsing, validation, cleaning, and writing (pure Python, no AWS)
- `worker/handler.py` — orchestrates the full 12-step pipeline from SQS event to DB completion
- `worker/exceptions.py` — `InvalidCSVError` to separate permanent from transient failures
- `worker/Dockerfile` — Lambda container image (Python 3.14, AD-05)
- Infrastructure CloudFormation groundwork (`network.yaml`) laid for Day 5-6

---

## 2. The Full Pipeline in One Picture

```
SQS Queue (csvforge-jobs-dev)
     │
     │  event source mapping (BatchSize=1, AD-07)
     ▼
lambda_handler(event, context)
     │
     │  Step 1:  iterate event["Records"]
     │  Step 2:  parse_job_message(record["body"])  → {job_id, s3_key}
     │  Step 3:  SessionLocal()  → open DB session
     │  Step 4:  claim_job_for_processing(db, job_id)
     │              → conditional UPDATE: queued|processing → processing
     │              → returns True (we own it) or False (terminal/already done)
     │              → if False: log and return cleanly (no exception)
     │  Step 5:  get_job_by_id(db, job_id)
     │              → read user_id from DB (NEVER from SQS message — AD-D2-08)
     │  Step 6:  download_file(input_bucket, s3_key, /tmp/{job_id}_input.csv)
     │  Step 7:  parse_and_analyse("/tmp/{job_id}_input.csv")
     │              → validate + parse → AnalysisResult
     │              → InvalidCSVError? → fail_job(), commit, return cleanly
     │  Step 8:  clean(analysis.rows)  → CleanResult  (trim + dedup)
     │  Step 9:  write_csv("/tmp/{job_id}_output.csv", ...)
     │  Step 10: S3 key = processed/{db_job.user_id}/{job_id}/result.csv
     │  Step 11: upload_file(output_bucket, processed_key, output_path)  ← S3 BEFORE DB (AD-14)
     │  Step 12: complete_job(db, job_id, metadata...)
     │           db.commit()
     ▼
SQS deletes the message (Lambda returned successfully)
```

---

## 3. The CSV Processor — Three Functions

### 3.1 `parse_and_analyse(file_path)` → `AnalysisResult`

**What it does:** opens a CSV file, validates every constraint in the AD-21 contract, returns parsed rows as list-of-dicts.

**Why it exists as a separate module:** the Lambda handler orchestrates AWS resources; this function knows nothing about AWS. That separation makes it independently unit-testable — no mocking of S3, SQS, or PostgreSQL needed.

**Validation contract (AD-21):**

| Input | Outcome |
|-------|---------|
| Empty file (0 bytes) | `InvalidCSVError("File is empty")` |
| Invalid UTF-8 | `InvalidCSVError("File encoding is not valid UTF-8: ...")` |
| UTF-8 with BOM | BOM stripped silently (`utf-8-sig` codec) |
| Duplicate column names | `InvalidCSVError("Duplicate column name: 'x' appears 2 times")` |
| Empty column name | `InvalidCSVError("Column has an empty name at position N")` |
| Malformed quoting | `InvalidCSVError("Malformed CSV: ...")` |
| Row with wrong field count | `InvalidCSVError("Row N has M fields; expected K")` |
| Header-only (0 data rows) | Valid — returns `row_count=0` |
| Normal UTF-8 CSV | `AnalysisResult` with column metadata + rows |

**Why duplicate headers are a permanent failure:**  
`csv.DictReader` silently drops earlier values for duplicate keys — `name,name,age` / `Alice,Bob,25` would produce `{'name': 'Bob', 'age': '25'}`. Alice is silently dropped. For a data-cleaning tool, silent data loss is unacceptable. Reject with a clear error (AD-21 revised).

### 3.2 `clean(rows)` → `CleanResult`

**Phase 1 — Trim:** strip leading/trailing whitespace from every string value in every row.

**Phase 2 — Dedup:** remove rows that are identical after trimming.

**Why this order matters:**
```
Row A: {'name': 'Alice '}   (trailing space)
Row B: {'name': 'Alice'}    (no trailing space)

Without trim-first:  A ≠ B → both kept  ← WRONG
With trim-first:     A == B → one removed  ← CORRECT
```
Deduplication is always based on the **normalised** form, never the raw form.

### 3.3 `write_csv(file_path, column_names, rows)`

Writes the cleaned rows to `/tmp/{job_id}_output.csv` using `csv.DictWriter`.
- `lineterminator="\r\n"` — RFC 4180 standard CSV line endings
- `extrasaction='ignore'` — silently skip unexpected extra keys (defensive)
- `encoding="utf-8"` — always UTF-8, no BOM on output

---

## 4. Worker Dockerfile — Lambda Container Image

The worker uses a **container image** deployment (not a zip archive), per AD-05.

```dockerfile
FROM public.ecr.aws/lambda/python:3.14
COPY worker/requirements.txt ...
RUN pip install --no-cache-dir -r ... --target ${LAMBDA_TASK_ROOT}
COPY backend/ ${LAMBDA_TASK_ROOT}/backend/
COPY worker/handler.py   ${LAMBDA_TASK_ROOT}/handler.py
COPY worker/csv_processor.py ...
COPY worker/exceptions.py ...
CMD ["handler.lambda_handler"]
```

**Why container, not zip?**  
`psycopg[binary]` bundles a compiled C extension linked against `libpq`. Binary wheels are OS-specific — a binary compiled on macOS will not run on Amazon Linux 2023 (Lambda's OS). The official Lambda base image is built on Amazon Linux 2023, so binaries installed inside it are guaranteed to work.

**Critical build context rule (AD-18):**
```bash
# CORRECT: context = project root (access to both worker/ and backend/)
docker build -f worker/Dockerfile -t csvforge-worker .

# WRONG: context = ./worker/ (backend/ is invisible)
docker build -f worker/Dockerfile -t csvforge-worker ./worker/
```

**Local testing with Lambda RIE:**
```bash
docker run -p 9000:8080 \
  -e S3_BUCKET_NAME=csvforge-files-dev \
  -e DATABASE_URL=postgresql+psycopg://csvforge:password@host.docker.internal:5432/csvforge \
  -e ENVIRONMENT=local \
  csvforge-worker

# In a second terminal:
curl -XPOST "http://localhost:9000/2015-03-31/functions/function/invocations" \
  -d '{"Records":[{"body":"{\"job_id\":\"test-1\",\"s3_key\":\"uploads/user/test-1/original.csv\"}"}]}'
```

---

## 5. Idempotency — Why SQS Delivers the Same Message Twice

SQS Standard queues guarantee **at-least-once** delivery. Under rare conditions (Lambda crash, visibility timeout, network hiccup) the same message can be delivered more than once.

### The primary guard: `claim_job_for_processing()`

```sql
UPDATE jobs
SET status = 'processing'
WHERE id = :job_id
  AND status IN ('queued', 'processing')
```

- `rowcount == 1` → we won the claim, proceed
- `rowcount == 0` → job is in a terminal state (completed/failed), return early

**Why include `'processing'` in the WHERE clause?**  
If Lambda timed out mid-processing, SQS re-delivers the message. The job is already in `'processing'` state. We want the retry to take over — including `'processing'` allows this. AD-08.

**Why a conditional UPDATE (not SELECT + UPDATE)?**  
A SELECT followed by an UPDATE has a race condition: two workers can both read `status='queued'` and both proceed. The conditional UPDATE is **atomic** — only one `UPDATE` can match and write `status='processing'`. The other gets `rowcount=0` and steps back.

### Secondary safety: deterministic S3 keys

The processed output key is `processed/{user_id}/{job_id}/result.csv`. If the worker processes the same job twice, the second `upload_file()` overwrites the same S3 object. S3 `PutObject` is idempotent on the same key — identical content, no orphaned objects.

---

## 6. Permanent vs Transient Failure

This is the most important operational distinction in the worker.

| Failure type | Example | Worker action | SQS action |
|---|---|---|---|
| **Permanent** | Invalid CSV, bad encoding | `fail_job()` + return cleanly | Deletes message |
| **Transient** | S3 throttle, DB timeout | `fail_job()` + **re-raise** | Retries after visibility timeout |

**Why return cleanly for permanent failures?**  
Re-raising would cause SQS to retry. But the file is permanently invalid — retrying with the same broken file loops forever, eventually filling the DLQ with useless noise. `fail_job()` records the reason for the user; returning cleanly signals SQS to delete the message.

**Why re-raise for transient failures?**  
Infrastructure problems (S3 throttling, DB connectivity) may resolve on the next attempt. Re-raising keeps the message in the queue (visible again after visibility timeout). After 5 failures, the message moves to the DLQ for investigation.

---

## 7. The S3-Before-DB Write Ordering (AD-14)

```
Step 11: upload_file(S3)      ← happens FIRST
Step 12: complete_job(DB)     ← happens SECOND
```

**Why this order?**

|  | S3 upload succeeds, DB update fails | S3 upload fails |
|--|--|--|
| **S3 before DB** | Retry can re-upload same deterministic key (idempotent), then update DB | Exception raised, SQS retries, job in `processing` state |
| **DB before S3** | Job shows `completed` with a key that has no S3 object → broken download URL forever | ← same |

The S3-before-DB order makes failure recoverable. The opposite order creates an unrecoverable inconsistency.

---

## 8. Security Invariant — user_id from DB, not SQS

```python
# WRONG — user_id from the SQS message body (untrusted)
processed_key = f"processed/{message['user_id']}/{job_id}/result.csv"

# CORRECT — user_id from the DB record (written by the authenticated API)
processed_key = f"processed/{db_job.user_id}/{job_id}/result.csv"
```

An attacker who can publish to the SQS queue (compromised credentials, misconfigured IAM) could craft a message with `user_id` set to another user's value. If the worker used the SQS `user_id`, the processed file would land in the wrong user's S3 prefix. Since the DB record was written by the authenticated FastAPI endpoint using the verified Cognito `sub` claim, it is authoritative.

---

## 9. Database Connectivity from Lambda — The Architecture Dependency

**The Lambda worker needs to connect to PostgreSQL.** In production this means RDS in the same VPC. A PostgreSQL instance running on your Mac via Docker is **not reachable from AWS Lambda** — it is not publicly exposed.

This is a documented blocker that requires:
1. RDS instance in the same VPC as Lambda (network.yaml + RDS provisioning)
2. VPC endpoint for Secrets Manager (to fetch the DB password)
3. Security group rule allowing Lambda → RDS on port 5432

Until RDS exists, the worker can be tested locally against a local PostgreSQL via the run_local.sh pattern, but end-to-end Lambda → RDS connectivity requires AWS infrastructure.

---

## 10. Key Failure Modes

| Scenario | What happens | Recovery |
|---|---|---|
| Missing input S3 object | `download_file()` raises → `fail_job()` + re-raise → SQS retries | Fix S3 key or re-upload |
| Malformed SQS message | `json.JSONDecodeError` propagates → SQS retries → DLQ after 5 failures | Inspect DLQ message body |
| Worker failure during processing | `fail_job()` + re-raise → SQS retries | Retry with same job |
| S3 upload succeeds, DB update fails | DB update fails → exception → SQS retries → worker re-uploads same key (idempotent) + re-updates DB | Automatic on retry |
| Duplicate message after completion | `claim_job_for_processing()` returns False → early return | Automatic |
| Transient AWS error | `fail_job()` + re-raise → retry → DLQ after 5 failures | Check CloudWatch logs |

---

## 11. Cost and Cleanup

**Lambda worker costs (ap-south-1):**
- $0.0000166667 per GB-second
- 1024 MB × 30s avg = 30.72 GB-seconds per invocation ≈ $0.000512
- 100 jobs/day × $0.000512 ≈ $0.05/day ≈ $1.54/month

**S3 /tmp cleanup:**
Lambda's `/tmp` (512 MB) persists across warm invocations in the same container. Files accumulate across warm runs. Production handlers should delete `/tmp` files after processing. For the current MVP, warm invocations with the same container re-use the same `/tmp` space — files from a prior job may still be present but are overwritten by the next job's deterministic filename.

**To delete all Lambda invocations (dev cleanup):**
```bash
aws lambda delete-function --function-name csvforge-worker-dev --region ap-south-1
```

---

## 12. Verification Commands

```bash
# Run all worker/CSV processor unit tests
PYTHONPATH=. .venv/bin/pytest tests/unit/test_csv_processor.py tests/unit/test_worker_handler.py -v

# Run the complete unit suite (197 tests)
PYTHONPATH=. .venv/bin/pytest tests/unit/ -q

# Build the worker Docker image (requires Docker)
docker build -f worker/Dockerfile -t csvforge-worker .

# Run with Lambda RIE (requires the image above)
docker run -p 9000:8080 \
  -e S3_BUCKET_NAME=csvforge-files-dev \
  -e DATABASE_URL=postgresql+psycopg://csvforge:password@host.docker.internal:5432/csvforge \
  -e ENVIRONMENT=local \
  csvforge-worker
```

---

## 13. Interview Questions

**Q: What is the difference between at-least-once and exactly-once delivery in SQS?**
A: SQS Standard queues guarantee at-least-once delivery — each message is delivered at least once but may occasionally be delivered more than once due to infrastructure redundancy. FIFO queues provide exactly-once processing within a 5-minute deduplication window. CSVForge uses Standard queues with application-level idempotency (`claim_job_for_processing`) rather than FIFO, because job ordering between different jobs is irrelevant and Standard queues have higher throughput at lower cost.

**Q: What is idempotency and why does the Lambda worker need it?**
A: An operation is idempotent if performing it multiple times has the same effect as performing it once. The worker is idempotent because SQS may deliver the same message twice. The `claim_job_for_processing` conditional UPDATE ensures only one invocation "wins" the claim. Deterministic S3 keys ensure re-uploads are safe. `fail_job` and `complete_job` overwrite the same DB row regardless of how many times they are called.

**Q: Why is S3 written before PostgreSQL in the worker?**
A: If S3 is written first and the DB update fails, the next retry re-writes the same S3 key (idempotent PutObject) and then successfully updates the DB. If the DB were written first and the S3 upload failed, the job would show `completed` in the DB with a `processed_s3_key` that has no corresponding S3 object — the download endpoint would generate a broken presigned URL with no way to recover.

**Q: Why use a Lambda container image instead of a zip package for the worker?**
A: `psycopg[binary]` is a compiled C extension linked against `libpq`. Binary wheels are OS-specific. A binary built on macOS won't run on Amazon Linux 2023 (Lambda's OS). The official Lambda base image (`public.ecr.aws/lambda/python:3.14`) runs on Amazon Linux 2023, so packages installed inside it are guaranteed to be compatible. Container images also give consistent Python versions and system libraries.

**Q: What happens when a CSV file has duplicate column names?**
A: `csv.DictReader` silently drops earlier values for duplicate keys. A CSV `name,name,age` / `Alice,Bob,25` produces `{'name': 'Bob', 'age': '25'}` — Alice is silently dropped. For a data-cleaning tool, silent data loss is unacceptable, so CSVForge rejects duplicate headers with `InvalidCSVError` as a permanent failure (AD-21). The job is marked `failed` and the SQS message is deleted (not retried, since the file is permanently invalid).

---

## 14. What Day 3 Defers

- RDS provisioning (done in Day 5/6 as part of full CloudFormation deployment)
- Frontend upload flow (Day 7)
- Lambda deployment to real AWS (Day 6)
- Real end-to-end test with actual SQS → Lambda → S3 → DB (AWS verification phase)
