# CSVForge — Spec Requirements

> This document formalises the product requirements from `docs/requirements.md` into
> numbered, testable statements. It is the contract between product intent and
> implementation. Each requirement is tagged so design decisions and tasks can trace
> back to it.

---

## 1. Users and Roles

| ID | Requirement |
|----|-------------|
| R-USR-01 | The system has two roles: **User** and **Administrator**. |
| R-USR-02 | A User can upload, inspect, and download their own CSV files. |
| R-USR-03 | An Administrator can view all files, all processing jobs, and all failures across all users. |
| R-USR-04 | A User must not be able to access another user's files or results. |

---

## 2. Authentication

| ID | Requirement |
|----|-------------|
| R-AUTH-01 | Every request to a protected API endpoint must be authenticated. |
| R-AUTH-02 | Unauthenticated requests must receive a 401 response and must not access any data. |
| R-AUTH-03 | The system must support at minimum username/password login for MVP. |
| R-AUTH-04 | Auth tokens must expire. When an access token expires, the user must authenticate again. Silent token refresh is out of MVP scope. Token expiry is enforced by Cognito; the application does not implement refresh logic. |

---

## 3. Upload

| ID | Requirement |
|----|-------------|
| R-UPL-01 | A User can upload a single CSV file per request. |
| R-UPL-02 | The maximum permitted file size is **50 MB**. This is an explicit MVP constraint. Files larger than 50 MB must be rejected with a clear error message before upload begins or immediately upon receipt. |
| R-UPL-03 | Only files with a `.csv` MIME type or `.csv` extension are accepted. Other file types must be rejected. |
| R-UPL-04 | Uploaded files must be stored in object storage (S3), not in the database. |
| R-UPL-05 | After a successful upload, a processing job record must be created in the database with status `queued`. |
| R-UPL-06 | The file must not be publicly accessible via a direct URL at any time. |

---

## 4. Analysis

| ID | Requirement |
|----|-------------|
| R-ANL-01 | The system must extract and store the following metadata for every uploaded file: file name, file size (bytes), row count, column count, column names. |
| R-ANL-02 | Metadata must be derived from the file content during processing, not inferred from the upload request. |

---

## 5. Validation

| ID | Requirement |
|----|-------------|
| R-VAL-01 | The system must detect whether the uploaded file is a valid CSV. |
| R-VAL-02 | If the file is invalid (e.g. unparseable, binary content), the job must be marked `failed` with a human-readable reason. |
| R-VAL-03 | An invalid file must not be retried. The failure is permanent. |

---

## 6. Cleaning

| ID | Requirement |
|----|-------------|
| R-CLN-01 | The system must trim leading and trailing whitespace from all cell values. |
| R-CLN-02 | The system must detect duplicate rows (exact match across all columns after trimming). |
| R-CLN-03 | The system must remove duplicate rows, retaining the first occurrence. |
| R-CLN-04 | The cleaning operations in R-CLN-01 through R-CLN-03 must be applied in that order. |

---

## 7. Processing Status

| ID | Requirement |
|----|-------------|
| R-STS-01 | Every processing job has one of these states: `queued`, `processing`, `completed`, `failed`. |
| R-STS-02 | State transitions are strictly: `queued → processing → completed` or `queued → processing → failed`. |
| R-STS-03 | A User can poll or view the status of their own jobs. |
| R-STS-04 | A `failed` job must record a human-readable reason visible to the user. |

---

## 8. Results and Download

| ID | Requirement |
|----|-------------|
| R-DWN-01 | When a job reaches `completed`, the processed CSV must be available for download. |
| R-DWN-02 | The download must be served via a time-limited pre-signed S3 URL, not a publicly permanent URL. |
| R-DWN-03 | A User must only be able to obtain a download URL for their own files. |
| R-DWN-04 | The processed file must be stored separately from the original file in S3. |

---

## 9. Admin View

| ID | Requirement |
|----|-------------|
| R-ADM-01 | An Administrator can list all uploaded files with metadata. |
| R-ADM-02 | An Administrator can list all processing jobs with status and timestamps. |
| R-ADM-03 | An Administrator can filter jobs by status (e.g. view all `failed` jobs). |
| R-ADM-04 | Admin screens must not expose raw CSV content in the UI or in API responses. |

---

## 10. Security

| ID | Requirement |
|----|-------------|
| R-SEC-01 | CSV file content must never be written to application logs. |
| R-SEC-02 | Secrets (DB passwords, AWS credentials, JWT secrets) must not be committed to version control. |
| R-SEC-03 | Secrets must be injected via environment variables or a secrets manager at runtime. |
| R-SEC-04 | S3 buckets must have public access blocked at the bucket policy level. |
| R-SEC-05 | The API must validate and reject files that exceed the size limit before writing to S3. |

---

## 11. Reliability

| ID | Requirement |
|----|-------------|
| R-REL-01 | A temporary processing failure (network error, transient Lambda error) must be retried up to a configurable maximum. |
| R-REL-02 | A permanent processing failure (invalid CSV, unrecoverable error) must not be retried. |
| R-REL-03 | Processing the same job message more than once must not corrupt the stored state or produce duplicate output files. (Worker idempotency.) |
| R-REL-04 | After exhausting retries, the job message must be routed to a Dead Letter Queue (DLQ). |
| R-REL-05 | The presence of messages in the DLQ must generate an observable alert. |

---

## 12. Observability

| ID | Requirement |
|----|-------------|
| R-OBS-01 | Application errors must be logged in a structured format (JSON). |
| R-OBS-02 | Lambda function errors and DLQ depth must produce CloudWatch metrics. |
| R-OBS-03 | A CloudWatch alarm must fire when DLQ depth > 0. |
| R-OBS-04 | The system must log job lifecycle events: queued, processing started, completed, failed. |

---

## 13. MVP Scope Boundary

The following are explicitly **out of scope** for the MVP:

- File sizes greater than 50 MB
- Multiple file uploads in a single request
- User-configurable cleaning rules
- Real-time push notifications (WebSocket/SSE)
- Billing, quotas, or rate limiting
- Multi-tenancy beyond user-level isolation
- File versioning or audit history
