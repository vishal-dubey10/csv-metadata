# IAM Policies — CSVForge Dev

This directory contains IAM policy documents for the CSVForge development environment.
Each file is used by `../create_dev_resources.sh` to provision the correct IAM roles
and inline policies.

---

## Files

### `lambda-trust-policy.json`

The **trust policy** (also called an "assume-role policy") for the Lambda execution role.

This policy answers the question: *who is allowed to assume this role?*

It grants `lambda.amazonaws.com` the ability to call `sts:AssumeRole`. Without this,
even if the role exists, Lambda cannot use it — the service principal must be explicitly
trusted.

This file is passed to `aws iam create-role --assume-role-policy-document`.

---

### `lambda-execution-policy.json`

The **permission policy** for the Lambda worker role (`csvforge-worker-role-dev`).

Permissions granted:

| Statement | Action | Resource | Why the worker needs it |
|-----------|--------|----------|------------------------|
| `S3InputRead` | `s3:GetObject` | `uploads/*` | Download the raw CSV for processing |
| `S3OutputWrite` | `s3:PutObject` | `processed/*` | Upload the cleaned CSV output |
| `SQSProcessing` | `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` | `csvforge-jobs-dev` | Receive job tickets from the queue and delete them after successful processing |
| `CloudWatchLogs` | `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` | Lambda log group | Emit structured logs to CloudWatch |

This file is attached as an inline policy via `aws iam put-role-policy`.

---

### `api-service-policy.json`

The **permission policy** for the API service role (`csvforge-api-role-dev`).

Permissions granted:

| Statement | Action | Resource | Why the API needs it |
|-----------|--------|----------|---------------------|
| `S3PresignedUpload` | `s3:PutObject` | `uploads/*` | Generate a presigned PUT URL so the browser can upload directly to S3 |
| `SQSPublish` | `sqs:SendMessage` | `csvforge-jobs-dev` | Publish a job ticket after upload is confirmed |

---

## Least Privilege — Why Each Role Gets Exactly What It Needs

The **principle of least privilege** means every role is given only the permissions required
for its specific job. No more.

Below is the reasoning for each permission boundary — the "why NOT" is as important as the "why".

---

### Why Lambda does NOT have `sqs:SendMessage`

Lambda is a **consumer**. It picks up job tickets, processes CSVs, and deletes the tickets.
Publishing new messages to the queue is the **API's** job.

If the Lambda worker could publish to SQS, a bug in the worker could cause it to:
- Re-publish a job after completing it, causing the same job to be processed again
- Publish malformed messages that trigger processing loops
- Spam the queue under certain failure modes (e.g., error-handling code that accidentally publishes)

Removing `sqs:SendMessage` from the Lambda role makes these entire failure classes impossible
at the IAM layer — the Lambda function literally cannot publish, regardless of what the code does.

---

### Why API does NOT have `sqs:ReceiveMessage`

The API is a **producer**. It creates job tickets and hands them to the queue.
Consuming messages is the **Lambda worker's** job.

If the API could receive messages, it would be possible for the API to process a job
synchronously during a request — bypassing Lambda, bypassing retry logic, bypassing
backpressure, and blocking the request thread for the duration of CSV processing.
This is the exact anti-pattern the event-driven architecture is designed to prevent.

Separation of concerns at the IAM level enforces the architectural boundary:
the API publishes, the worker consumes. Neither can do the other's job.

---

### Why Lambda does NOT have `s3:PutObject` on `uploads/*`

Lambda is a **reader** of the uploads prefix. It downloads the raw CSV for processing.
Writing to `uploads/*` is a dangerous capability for the worker because:

- A bug in the worker could **overwrite the original user CSV** — the uploaded file the user
  sent is the only copy. If the worker corrupts it, the data is gone permanently.
- Write access to `uploads/*` opens a path for a malicious message to cause the worker
  to write arbitrary content to any upload path.

The worker writes its output to `processed/*` — a completely separate prefix — and never
needs to touch the uploads prefix except to read.

---

### Why API does NOT have `s3:GetObject` on `processed/*`

The API generates **presigned download URLs** for processed files. A presigned URL is a
temporary credential that allows the **browser** to download directly from S3.

When the API generates a presigned GET URL, the URL contains the IAM signature of the
API's role. The browser presents that URL directly to S3. The API itself never downloads
the file bytes.

If the API needed to read the file (e.g., to serve it as a response body), it would need
`s3:GetObject`. But that architecture would route megabytes of CSV data through the API
process — wasting memory, Lambda execution time, and API Gateway bandwidth for no benefit.

The presigned URL pattern deliberately keeps the data plane (S3 ↔ Browser) separate from
the control plane (Browser ↔ API). The API's role reflects this: it can generate download
credentials (via presigned URL signing), but it never reads the processed bytes.

---

## Summary Table

| Permission | Lambda Worker | API Service | Reason for separation |
|------------|:-------------:|:-----------:|----------------------|
| `s3:GetObject` on `uploads/*` | ✅ | ❌ | Worker reads CSV to process; API doesn't need raw bytes |
| `s3:PutObject` on `uploads/*` | ❌ | ✅ | API generates upload presigned URL; worker must not overwrite originals |
| `s3:GetObject` on `processed/*` | ❌ | ❌ | Browser downloads directly via presigned URL; neither service needs to read output |
| `s3:PutObject` on `processed/*` | ✅ | ❌ | Worker writes cleaned output; API never writes to processed prefix |
| `sqs:SendMessage` | ❌ | ✅ | API publishes job tickets; worker must not create processing loops |
| `sqs:ReceiveMessage` / `sqs:DeleteMessage` | ✅ | ❌ | Worker consumes tickets; API must not process jobs synchronously |
| `logs:*` on Lambda log group | ✅ | — | Lambda emits structured logs; each function has its own log group |

---

## Deployment

These policy files are consumed by `../create_dev_resources.sh`:

```bash
# Create the IAM role with the trust policy
aws iam create-role \
  --role-name csvforge-worker-role-dev \
  --assume-role-policy-document file://lambda-trust-policy.json

# Attach the execution policy as an inline policy
aws iam put-role-policy \
  --role-name csvforge-worker-role-dev \
  --policy-name csvforge-worker-execution-dev \
  --policy-document file://lambda-execution-policy.json
```

The API service policy (`api-service-policy.json`) is attached to a separate IAM role
for the FastAPI service. See `../create_dev_resources.sh` for full provisioning commands.
