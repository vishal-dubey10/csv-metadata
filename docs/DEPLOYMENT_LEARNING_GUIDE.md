# CSVForge — AWS Deployment Learning Guide

> This guide covers everything that was deployed, why each decision was made, what went wrong during deployment, and the interview concepts behind each component. Target audience: a developer who knows Python and is learning AWS.

---

## Table of Contents

1. [The Big Picture](#1-the-big-picture)
2. [The Factory Analogy](#2-the-factory-analogy)
3. [Layer-by-Layer: Every AWS Service Explained](#3-layer-by-layer-every-aws-service-explained)
   - [3.1 VPC & Networking](#31-vpc--networking)
   - [3.2 S3 — Object Storage](#32-s3--object-storage)
   - [3.3 SQS — Job Queue](#33-sqs--job-queue)
   - [3.4 RDS — PostgreSQL](#34-rds--postgresql)
   - [3.5 Secrets Manager](#35-secrets-manager)
   - [3.6 Cognito — Authentication](#36-cognito--authentication)
   - [3.7 Lambda — Serverless Compute](#37-lambda--serverless-compute)
   - [3.8 API Gateway](#38-api-gateway)
   - [3.9 CloudFront — CDN](#39-cloudfront--cdn)
   - [3.10 IAM — Least Privilege](#310-iam--least-privilege)
4. [Deployment War Stories](#4-deployment-war-stories)
5. [The End-to-End Request Flow](#5-the-end-to-end-request-flow)
6. [10 Practical Exercises](#6-10-practical-exercises)
7. [30 Interview Questions with Model Answers](#7-30-interview-questions-with-model-answers)

---

## 1. The Big Picture

CSVForge is a CSV processing pipeline. Users upload CSV files through a React frontend. The files land in S3. A worker Lambda reads them, cleans them (deduplication, validation), writes results back to S3, and the frontend downloads the processed file.

```
┌──────────────────────────────────────────────────────────┐
│  Browser (React SPA)                                     │
│  https://d38s2v3jcf0q6l.cloudfront.net                   │
└──────────┬────────────────────────────────┬──────────────┘
           │ HTTPS (Cognito JWT)            │ HTTPS (presigned S3 URL)
           ▼                                ▼
┌─────────────────────┐         ┌───────────────────────┐
│  CloudFront CDN      │         │  S3 Bucket            │
│  (serves React app)  │         │  csvforge-files-dev    │
└─────────────────────┘         │  uploads/ processed/   │
                                └───────┬───────────────┘
                                        │
┌─────────────────────┐         ┌───────▼───────────────┐
│  API Gateway        │         │  SQS Queue             │
│  (HTTP API + JWT)   │         │  csvforge-jobs-dev     │
└────────┬────────────┘         └───────┬───────────────┘
         │                              │
┌────────▼────────────┐         ┌───────▼───────────────┐
│  API Lambda          │         │  Worker Lambda         │
│  (FastAPI + Mangum)  │         │  (Container image)     │
│  In VPC              │         │  In VPC                │
└────────┬────────────┘         └───────┬───────────────┘
         │                              │
         └──────────┬───────────────────┘
                    ▼
         ┌─────────────────────┐
         │  RDS PostgreSQL 16  │
         │  csvforge-db-dev     │
         │  Private subnet     │
         └─────────────────────┘
```

### All Deployed Resources

| Resource | Name | Region |
|---|---|---|
| VPC stack | csvforge-network-dev | ap-southeast-2 |
| S3 data bucket | csvforge-files-dev | ap-southeast-2 |
| SQS main queue | csvforge-jobs-dev | ap-southeast-2 |
| SQS DLQ | csvforge-jobs-dev-dlq | ap-southeast-2 |
| RDS PostgreSQL | csvforge-db-dev | ap-southeast-2 |
| Secrets Manager | csvforge/db/dev | ap-southeast-2 |
| Cognito User Pool | csvforge-users-dev | ap-southeast-2 |
| API Lambda | csvforge-api-dev | ap-southeast-2 |
| API Gateway | csvforge-api-dev | ap-southeast-2 |
| Worker Lambda | csvforge-worker-dev | ap-southeast-2 |
| ECR repository | csvforge-worker-dev | ap-southeast-2 |
| CloudFront | d38s2v3jcf0q6l.cloudfront.net | Global |
| Frontend S3 | csvforge-frontend-dev | ap-southeast-2 |

---

## 2. The Factory Analogy

Think of CSVForge as a **factory** that cleans CSV files:

| Factory Role | AWS Resource | Why |
|---|---|---|
| **Factory building** | VPC | Everything happens inside this private network |
| **Loading dock** | API Gateway | Where orders come in (HTTP requests from browsers) |
| **Receptionist** | Cognito | Checks your ID before letting you in |
| **Front desk** | API Lambda (FastAPI) | Takes your order, writes a job ticket |
| **Order ticket board** | SQS | Where job tickets are pinned for the kitchen |
| **Pantry** | S3 | Where ingredients (raw CSVs) and finished products (processed CSVs) are stored |
| **Chef** | Worker Lambda | Reads the order ticket, gets ingredients, cooks, writes result |
| **Refrigerator** | RDS PostgreSQL | Where job records are tracked (status, metadata) |
| **Safe** | Secrets Manager | Where the refrigerator key (DB password) is kept |
| **Dining room** | CloudFront + S3 | Where customers sit and see the menu (React app) |
| **Failed orders room** | SQS DLQ | Where unprocessable orders go for supervisor review |

### The full journey of a CSV file:

1. **Customer arrives** — Browser loads the React app from CloudFront
2. **Receptionist checks ID** — Cognito authenticates the user, issues a JWT
3. **Customer places order** — POST /uploads/intent creates a job record in PostgreSQL
4. **Order ticket pinned** — SQS message sent with the job_id
5. **Customer puts ingredients in pantry** — Browser uploads CSV directly to S3 via presigned URL
6. **Customer confirms delivery** — POST /uploads/{job_id}/complete verifies the upload
7. **Chef picks up the order** — Worker Lambda polls SQS, receives the message
8. **Chef gets ingredients** — Downloads CSV from S3 to /tmp
9. **Chef cooks** — Parses, validates, deduplicates the CSV
10. **Chef stores the finished product** — Uploads processed CSV to S3
11. **Chef updates the order** — Updates job status to "completed" in PostgreSQL
12. **Customer picks up the order** — GET /jobs/{id}/download returns a presigned download URL

---

## 3. Layer-by-Layer: Every AWS Service Explained

### 3.1 VPC & Networking

**What it is:** A Virtual Private Cloud — your own isolated network inside AWS. No other AWS customer can see or reach your VPC.

**Why we need it:** RDS (PostgreSQL) must not be publicly accessible. Placing it in a private subnet (no public IP) means only resources in the same VPC can reach it. Lambda functions run inside the VPC to connect to RDS.

**What was deployed:**
- VPC: 10.0.0.0/16 (65,536 IPs)
- 2 private subnets in separate Availability Zones (10.0.1.0/24 and 10.0.2.0/24)
- Lambda Security Group: allows all outbound, no inbound
- RDS Security Group: allows port 5432 only from Lambda SG
- S3 Gateway Endpoint (free — routes S3 traffic inside AWS backbone)
- SQS Interface Endpoint (~$0.01/hr per AZ)
- Secrets Manager Interface Endpoint (~$0.01/hr per AZ)
- **No NAT Gateway** — saves ~$33/month. Lambda reaches AWS services via VPC endpoints instead.

**Interview Q: "What is CIDR notation?"**
CIDR (Classless Inter-Domain Routing) defines IP ranges. `10.0.0.0/16` means the first 16 bits are fixed (10.0) and the last 16 bits are available for hosts. That gives 2^16 = 65,536 possible IPs. Subnets use `/24` (256 IPs each).

**Interview Q: "Why no NAT Gateway?"**
A NAT Gateway lets resources in a private subnet reach the internet. It costs ~$0.045/hr (~$33/month) plus data charges. Since Lambda only needs to reach AWS services (S3, SQS, Secrets Manager), VPC endpoints provide the same connectivity privately and more cheaply. S3 gateway endpoints are free; interface endpoints cost ~$0.01/hr per AZ but are still cheaper than NAT.

**Interview Q: "What is a security group?"**
A stateful virtual firewall attached to AWS resources. "Stateful" means if you allow outbound traffic on port X, the return traffic on the same connection is automatically allowed — you don't need a matching inbound rule. Security groups reference each other (RDS SG allows traffic from Lambda SG), not CIDR ranges, for precise access control.

**Interview Q: "What is the difference between a gateway endpoint and an interface endpoint?"**
Gateway endpoints (S3, DynamoDB) modify your route table to route traffic through the AWS backbone. They are free. Interface endpoints (PrivateLink) create network interfaces (ENIs) in your subnets with private IPs. They cost ~$0.01/hr per AZ but support services that don't offer gateway endpoints (SQS, Secrets Manager, etc.).

---

### 3.2 S3 — Object Storage

**What it is:** Amazon S3 (Simple Storage Service) is object storage. You store files ("objects") by key in a "bucket." There are no directories — slashes in keys are just characters that look like paths.

**What was deployed:**
- Bucket: `csvforge-files-dev`
- Public access blocked at every level (ACLs, bucket policies, access points)
- Server-side encryption (SSE-S3, AES-256, free)
- CORS configured for browser direct upload via presigned PUT URL
- Key structure: `uploads/{user_id}/{job_id}/original.csv` and `processed/{user_id}/{job_id}/result.csv`

**Interview Q: "What is a presigned URL and why use it?"**
A presigned URL is a time-limited signed S3 URL that grants a caller temporary access to one specific operation (PUT or GET) on one specific key — without requiring AWS credentials. The browser uploads CSV files directly to S3, bypassing Lambda entirely. This is critical because Lambda has a 6 MB payload limit — a 50 MB CSV can't pass through Lambda. The presigned URL lets the browser write directly to S3.

**Interview Q: "What is the difference between object storage and block storage?"**
Block storage (EBS) presents raw disk blocks — the OS manages the filesystem, file seek, partial writes. Object storage (S3) stores and retrieves complete objects by key over HTTP — no partial updates, no filesystem semantics. Objects can't be partially written; you replace them entirely. Object storage scales to exabytes because each object is independent; block storage is bounded by one disk's capacity.

**Interview Q: "Why is the job_id in the S3 key?"**
The key is deterministic. Given a job record in PostgreSQL, you always know exactly where its file lives — no need to search S3. It also namespaces files per job: a re-upload cannot overwrite a different job's file because each job has its own unique ID prefix.

---

### 3.3 SQS — Job Queue

**What it is:** Amazon SQS (Simple Queue Service) is a managed message queue. Producers (API Lambda) send messages; consumers (Worker Lambda) receive and process them.

**What was deployed:**
- Main queue: `csvforge-jobs-dev`
- Dead-Letter Queue: `csvforge-jobs-dev-dlq`
- VisibilityTimeout: 1800s (6 x Lambda timeout of 300s)
- MaxReceiveCount: 5 (after 5 failures, message moves to DLQ)
- MessageRetentionPeriod: 345600s (4 days)
- Standard queue (at-least-once delivery, best-effort ordering)

**Interview Q: "What is the SQS visibility timeout and why does it matter?"**
When a consumer receives a message, SQS hides it from other consumers for VisibilityTimeout seconds. This prevents two Lambda instances from processing the same job simultaneously. If the consumer crashes, the message reappears after the timeout and another worker retries it. The timeout must be LONGER than the Lambda timeout — if it's shorter, the message becomes visible while Lambda is still processing, causing duplicate processing.

**Interview Q: "What is a Dead-Letter Queue?"**
A separate queue where unprocessable messages land after MaxReceiveCount failed attempts. The DLQ prevents a "poison pill" (a message that always crashes the consumer) from blocking the queue forever. Messages in the DLQ are not automatically processed — they sit there for human investigation. You can replay them to the main queue after fixing the bug.

**Interview Q: "Standard vs FIFO queues?"**
Standard queues guarantee at-least-once delivery with best-effort ordering. A message may be delivered more than once, so consumers must be idempotent (safe to process the same message twice). CSVForge's `claim_job_for_processing()` uses a conditional UPDATE (`WHERE status='queued'`) — re-processing is harmless because the second claim finds nothing to update. FIFO queues guarantee exactly-once with strict ordering but cost more and cap at 3000 msg/s. For independent CSV processing jobs, Standard is the right choice.

**Interview Q: "What is at-least-once delivery and why does it require idempotency?"**
SQS Standard may occasionally deliver the same message twice (during redelivery after a crash, or due to internal SQS scaling). Your consumer must handle duplicate messages safely. CSVForge achieves this with a conditional UPDATE: `UPDATE jobs SET status='processing' WHERE id=? AND status='queued'`. If the message is a duplicate, the UPDATE affects 0 rows (status is already 'completed' or 'processing'), and the handler exits cleanly.

---

### 3.4 RDS — PostgreSQL

**What it is:** Amazon RDS (Relational Database Service) is a managed PostgreSQL instance. AWS handles backups, patches, OS updates, and failover. You just connect and use it.

**What was deployed:**
- Instance: `csvforge-db-dev`
- Engine: PostgreSQL 16.9
- Instance class: db.t3.micro (1 vCPU, 1 GB RAM — smallest available)
- Storage: 20 GB gp2 (SSD)
- Single-AZ (no standby — dev environment)
- Backup retention: 1 day
- Private subnet group spanning 2 AZs (required by RDS even for Single-AZ)
- Security group: only accepts port 5432 from Lambda SG

**Interview Q: "What is a DB subnet group?"**
RDS won't let you place a database in a single subnet — it requires a named collection of subnets across at least 2 AZs. Even for a Single-AZ instance, the subnet group must span 2 AZs so RDS can failover during maintenance windows. The subnet group tells RDS which subnets it's allowed to use; the actual instance lands in one of them.

**Interview Q: "Why use RDS instead of DynamoDB?"**
CSVForge needs ACID transactions for job state transitions. A job goes from `queued` → `processing` → `completed`. If two workers try to claim the same job, only one should succeed. PostgreSQL's row-level locking and conditional UPDATE provide this guarantee natively. DynamoDB is great for simple key-value lookups but lacks transactional integrity across multiple items (though it has limited transaction support). For a job tracking system with conditional updates, PostgreSQL is the natural choice.

**Interview Q: "Why is RDS in a private subnet?"**
A database should never be directly accessible from the internet. Placing RDS in a private subnet (no public IP, no internet gateway route) means only resources in the same VPC can reach it. The Lambda functions, which run in the same VPC, connect via private IP over port 5432. No NAT gateway, no bastion host, no public exposure.

---

### 3.5 Secrets Manager

**What it is:** AWS Secrets Manager stores encrypted secrets (passwords, API keys) and provides them to authorized services at runtime. It costs $0.40/secret/month.

**What was deployed:**
- Secret: `csvforge/db/dev` containing `{"username":"csvforge","password":"...","dbname":"csvforge"}`
- Lambda fetches the secret at cold start using an IAM permission
- VPC endpoint for Secrets Manager (so Lambda can reach it without NAT)

**Interview Q: "Why not put the DB password in a Lambda environment variable?"**
Lambda environment variables are visible to anyone with `lambda:GetFunctionConfiguration` permission. They also appear in CloudFormation templates and can be logged accidentally. Secrets Manager stores the password encrypted (KMS) and Lambda fetches it at runtime using an IAM permission scoped to the exact secret ARN. The password is never in plaintext in your template, your git history, or your logs. Secrets Manager also enables automatic rotation — you can change the password without redeploying Lambda.

**Interview Q: "Secrets Manager vs Parameter Store?"**
Both store secrets. Parameter Store (Systems Manager) is simpler and cheaper ($0.05/10,000 API calls). Secrets Manager adds automatic rotation, cross-account access, and a higher cost ($0.40/secret/month). For a database password that needs rotation in production, Secrets Manager is the right choice. For a simple config value, Parameter Store is sufficient.

---

### 3.6 Cognito — Authentication

**What it is:** Amazon Cognito is a managed user directory and authentication service. It stores user credentials, handles login flows, and issues JWT tokens.

**What was deployed:**
- User Pool: `csvforge-users-dev` (stores users, validates passwords)
- App Client: `csvforge-web-dev` (represents the frontend, no client secret)
- Admin Group: `csvforge-admins` (for role-based access to /admin/jobs)
- Password policy: 8+ chars, uppercase, lowercase, number, symbol
- MFA: OFF (dev simplicity)
- Explicit auth flows: USER_PASSWORD_AUTH + REFRESH_TOKEN_AUTH

**Interview Q: "What is the difference between an ID token and an access token?"**
An ID token contains identity claims (email, sub, groups) — it tells you WHO the user is. An access token contains authorization scopes — it tells you WHAT the user can do. API Gateway's JWT Authoriser validates the access token. FastAPI reads the `sub` claim (Cognito's unique user ID) from the token to identify the user. CSVForge stores `sub` as the `user_id` in the jobs table — not the email, because emails can change.

**Interview Q: "Why no client secret for the app client?"**
A browser (public client) cannot keep a secret — JavaScript is fully visible to the user. App clients for browser apps must not have a client secret. The authentication code flow with PKCE (Proof Key for Code Exchange) provides security without a secret. PKCE generates a random challenge on each login and verifies it with a verifier — preventing interception of the authorization code.

**Interview Q: "How does Cognito group-based authorization work?"**
1. An admin adds a user to the `csvforge-admins` group via CLI
2. On login, Cognito includes `cognito:groups: ["csvforge-admins"]` in the access token
3. API Gateway JWT Authoriser passes the claim to FastAPI
4. `get_current_user()` reads `cognito:groups` and sets `is_admin=True`
5. `require_admin()` permits or denies access to GET /admin/jobs

---

### 3.7 Lambda — Serverless Compute

**What it is:** AWS Lambda runs your code in response to events without provisioning servers. You pay per invocation, not per server.

**What was deployed:**
- **API Lambda** (`csvforge-api-dev`): zip package, python3.14, 512 MB, 30s timeout, FastAPI + Mangum, runs in VPC
- **Worker Lambda** (`csvforge-worker-dev`): container image, python3.13, 1024 MB, 300s timeout, CSV processing, runs in VPC, triggered by SQS (BatchSize=1)

**Interview Q: "Why does the worker use a container image but the API uses a zip?"**
The worker uses `psycopg[binary]` — a C extension compiled against `libpq` (PostgreSQL client library). When you zip-package on macOS, the `.so` file is compiled for darwin/arm64 — it won't run on Lambda's Amazon Linux x86_64. A Docker container built FROM the official Lambda base image (`public.ecr.aws/lambda/python:3.13`) guarantees the binary is compiled in the exact same OS/architecture Lambda runs. The API has no native C dependencies that are OS-specific, so a zip works fine there.

**Interview Q: "What is a Lambda cold start?"**
When Lambda receives a request for a function that hasn't been invoked recently, it needs to: (1) download the code/container image, (2) start the runtime, (3) initialize the handler module. This "cold start" adds 1-10 seconds of latency. Subsequent invocations reuse the warm container and respond in milliseconds. Cold starts happen after deployments, after idle periods (~15 min), or during scaling events. For the API Lambda, this means the first request after idle takes ~3-4 seconds; for the worker, the cold start is ~2.4 seconds (visible in the CloudWatch logs: "Init Duration: 2392 ms").

**Interview Q: "Why does Lambda need special VPC configuration?"**
By default, Lambda runs OUTSIDE any VPC — it can reach the internet but NOT your private RDS database. To connect to RDS, you configure Lambda with VpcConfig (subnet IDs + security group). Lambda then creates Elastic Network Interfaces (ENIs) in your subnets and gets private IPs. The AWSLambdaVPCAccessExecutionRole managed policy grants the permissions to create/delete ENIs.

**Interview Q: "Why BatchSize=1 for SQS?"**
With batch size > 1, a Lambda exception causes ALL messages in the batch to be retried — including successfully processed ones. Each CSV job is independent and potentially large (50 MB). Processing multiple jobs per invocation risks OOM (out of memory) and makes failure analysis harder. Batch size 1 means each message is processed independently; one failure affects exactly one job.

---

### 3.8 API Gateway

**What it is:** Amazon API Gateway is a managed HTTP front door for your backend. It receives browser requests, validates JWTs, and forwards them to Lambda.

**What was deployed:**
- HTTP API (not REST API) — lower latency, lower cost, native JWT support
- JWT Authoriser (validates Cognito tokens against JWKS endpoint)
- Routes: `$default` (JWT auth), `GET /health` (no auth), `OPTIONS /{proxy+}` (no auth, for CORS)
- CORS configured at gateway level (AllowOrigins includes CloudFront URL)
- Payload format version 2.0

**Interview Q: "HTTP API vs REST API?"**
API Gateway offers two flavors. HTTP API is the modern, lower-latency, lower-cost option (~70% cheaper). It supports JWT authorisers, CORS, and Lambda proxy integration. REST API adds features like API keys, usage plans, request validation, and request/response transformation. For CSVForge (JWT auth, simple proxy to Lambda), HTTP API is sufficient. Choose REST API only when you need API keys, request validation, or transformation templates.

**Interview Q: "What is a JWT Authoriser?"**
A JWT (JSON Web Token) Authoriser validates tokens against a JWKS (JSON Web Key Set) endpoint automatically — no Lambda needed, no custom code. It verifies the token signature, checks the issuer (iss), expiration (exp), and audience (aud). If valid, it injects the decoded claims into the Lambda event. If invalid, it returns 401 before Lambda even runs. JWT Authorisers are designed for Cognito and any OIDC provider.

**Interview Q: "Why do CORS preflights fail with a JWT Authoriser?"**
A browser sends an OPTIONS preflight before any cross-origin request carrying headers (like Authorization). The preflight has no Authorization header — the browser is asking permission first. The JWT Authoriser on the `$default` route sees no token and returns 401. The fix: add an explicit `OPTIONS /{proxy+}` route with `AuthorizationType: NONE`. This lets preflights pass through to Lambda, where FastAPI's CORSMiddleware returns the correct Access-Control headers.

---

### 3.9 CloudFront — CDN

**What it is:** Amazon CloudFront is a Content Delivery Network with 450+ edge locations worldwide. It serves your React app from the nearest edge, not from your S3 bucket in ap-southeast-2.

**What was deployed:**
- Distribution ID: E3VJEQ46S3Q51H
- URL: https://d38s2v3jcf0q6l.cloudfront.net
- Origin: private S3 bucket (csvforge-frontend-dev) via Origin Access Control (OAC)
- Viewer protocol: redirect-to-https
- Default root object: index.html
- Custom error responses: 403→200, 404→200 (both serve index.html for SPA routing)
- Price class: 100 (US, EU, Asia Pacific only — cheaper than global)
- Compression: enabled (gzip/brotli)

**Interview Q: "Why CloudFront + private S3 instead of S3 static website hosting?"**
S3 static website hosting serves over HTTP only and from a single region. CloudFront provides HTTPS (required for Cognito tokens), global edge caching, DDoS protection, and custom error responses for SPA routing. The S3 bucket stays private — only CloudFront can read it via OAC (Origin Access Control). No one can access the bucket directly.

**Interview Q: "How does SPA routing work with CloudFront?"**
React Router uses browser history routing (e.g., /jobs/abc123). When a user navigates directly to /jobs/abc123, the browser asks CloudFront for that path — but S3 only has index.html at the root. S3 returns 403 (private bucket) or 404. CloudFront's custom error response maps 403→200 and 404→200, serving index.html with HTTP 200. React Router then reads window.location.pathname and routes client-side. Without this, users would see a 404 page on any direct navigation.

**Interview Q: "OAC vs OAI?"**
OAI (Origin Access Identity) is the older mechanism — it creates a special CloudFront user that S3 allows. OAC (Origin Access Control) is the current AWS recommendation — it uses SigV4 signing at the CloudFront level, supports more S3 features (including SSE-KMS), and doesn't require managing a separate IAM principal. Use OAC for all new distributions.

---

### 3.10 IAM — Least Privilege

**What it is:** AWS IAM (Identity and Access Management) controls who can do what. Every Lambda has an execution role with a set of permissions.

**What was deployed:**
- API Lambda role: S3 (uploads + processed), SQS (send message), Secrets Manager (get secret), CloudWatch Logs, VPC networking
- Worker Lambda role: S3 (read uploads + write processed), Secrets Manager (get secret), CloudWatch Logs, VPC networking, SQS (receive/delete)
- Both roles use managed policies for VPC and SQS, plus inline policies for resource-specific permissions

**Interview Q: "What is the difference between a trust policy and a permission policy?"**
A trust policy says WHO can assume a role (e.g., "lambda.amazonaws.com can assume this role"). A permission policy says WHAT the role can do once assumed (e.g., "s3:GetObject on bucket-X"). The trust policy is attached to the role; the permission policy is attached to or embedded in the role. Both are needed — the trust policy grants the ability to become the role; the permission policy grants the abilities the role has.

**Interview Q: "What is least privilege IAM?"**
Every role should have exactly the permissions it needs — no more. The worker Lambda can read from `uploads/*` and write to `processed/*`, but it cannot write to `uploads/` (preventing corruption of input files) or read from `processed/` (not needed). The API Lambda can generate presigned URLs (s3:PutObject + s3:GetObject) and send SQS messages, but cannot receive or delete SQS messages (that's the worker's job). Scoping permissions to specific ARNs and prefixes limits the blast radius of a compromised role.

---

## 4. Deployment War Stories

### 4.1 The OCI Image Index Problem

**Symptom:** `The image manifest, config or layer media type for the source image ... is not supported`

**Root cause:** Docker Desktop on Apple Silicon uses BuildKit by default. When you run `docker build --platform linux/amd64`, BuildKit produces an OCI Image Index (multi-arch manifest list) with an attestation manifest — even if there's only one platform. Lambda only accepts Docker Image Manifest V2 Schema 2, not OCI Image Index.

**Fix:** Build with `--provenance=false`:
```bash
docker buildx build --platform linux/amd64 --provenance=false --output type=docker -f worker/Dockerfile -t csvforge-worker:lambda .
```
The `--provenance=false` flag tells BuildKit not to attach the attestation manifest, producing a plain Docker V2 image that Lambda accepts.

**Interview Q: "How would you debug a Lambda container image that won't deploy?"**
1. Check `docker inspect` for the architecture (must match Lambda's expected architecture)
2. Check the ECR image's manifest type — if size is ~856 bytes, it's an OCI index, not a V2 manifest (~3000+ bytes)
3. Rebuild with `--provenance=false` to suppress the attestation manifest
4. Verify the base image is an officially supported Lambda runtime (python:3.13, not python:3.14)

### 4.2 python:3.14 Not Supported as Lambda Base Image

**Symptom:** Same "media type not supported" error, even after fixing the OCI index issue.

**Root cause:** The `public.ecr.aws/lambda/python:3.14` base image is a pre-release/mainline build. It uses a different manifest format that Lambda rejects in some regions. Lambda officially supports python:3.8 through python:3.13 for container images.

**Fix:** Use `python:3.13` as the base image. The application code is fully compatible — Python 3.14 adds no features that 3.13 lacks for this codebase.

### 4.3 AWS_DEFAULT_REGION Is a Reserved Lambda Env Var

**Symptom:** `Lambda was unable to configure your environment variables because the environment variables you have provided contains reserved keys`

**Root cause:** CloudFormation template included `AWS_DEFAULT_REGION: !Ref AWS::Region` in the Lambda environment variables. Lambda sets this variable automatically and won't let you override it.

**Fix:** Remove `AWS_DEFAULT_REGION` from the CloudFormation template's `Environment.Variables` block. Lambda already sets it to the region the function runs in.

### 4.4 CORS Preflight 401

**Symptom:** Browser shows CORS errors. All API requests fail with 401, even before the actual request is sent.

**Root cause:** The JWT Authoriser on the `$default` route rejects OPTIONS preflight requests (which never carry an Authorization header). The browser sees the 401 and blocks the actual request.

**Fix:** Two changes were needed:
1. Add an explicit `OPTIONS /{proxy+}` route with `AuthorizationType: NONE` — lets preflights bypass the JWT Authoriser
2. Add the CloudFront URL to the API Gateway's `CorsConfiguration.AllowOrigins` — API Gateway handles OPTIONS at the gateway level using this list

### 4.5 psycopg dist-info Needed for Version Detection

**Symptom:** `psycopg version 3.0.2 or higher is required`

**Root cause:** The initial Lambda zip excluded `*.dist-info/*` directories (to save space). psycopg uses `importlib.metadata.version("psycopg-binary")` to detect its own version at runtime. Without the dist-info directory, the version lookup returns `"0.0.0.0"` — which is lower than the required `3.0.2`.

**Fix:** Include dist-info directories in the zip. They're small (a few KB each) and required for Python's package metadata system.

### 4.6 Worker `No module named 'worker'`

**Symptom:** Worker Lambda crashes with `Runtime.ImportModuleError: No module named 'worker'`

**Root cause:** `handler.py` imports `from worker.csv_processor import ...`. The Dockerfile originally copied `csv_processor.py` and `exceptions.py` to `/var/task/` (flat — no `worker/` directory). Python couldn't find the `worker` package.

**Fix:** Create a `worker/` package directory inside the container:
```dockerfile
RUN mkdir -p ${LAMBDA_TASK_ROOT}/worker
RUN touch ${LAMBDA_TASK_ROOT}/worker/__init__.py
COPY worker/csv_processor.py ${LAMBDA_TASK_ROOT}/worker/csv_processor.py
COPY worker/exceptions.py ${LAMBDA_TASK_ROOT}/worker/exceptions.py
```

---

## 5. The End-to-End Request Flow

### Upload Flow (user uploads a CSV):

```
1. Browser loads React app
   └─ CloudFront serves index.html + JS from edge location

2. User signs in
   └─ Browser → Cognito (USER_PASSWORD_AUTH)
   └─ Cognito returns access_token (JWT)

3. User clicks "Upload"
   └─ Browser → API Gateway: POST /uploads/intent
      Authorization: Bearer <access_token>
      Body: {user_email, original_filename, file_size_bytes}
   └─ API Gateway JWT Authoriser validates token
   └─ Lambda (FastAPI/Mangum) receives event
   └─ FastAPI extracts user_id from JWT sub claim
   └─ job_repository.create_job() → INSERT into PostgreSQL
   └─ s3_client.generate_presigned_put_url() → S3 presigned URL
   └─ Response: {job_id, upload_url, s3_key}

4. Browser uploads file directly to S3
   └─ Browser → S3: PUT <presigned_url>
   └─ File stored at: uploads/{user_id}/{job_id}/original.csv
   └─ S3 returns 200 + ETag

5. Browser confirms upload
   └─ Browser → API Gateway: POST /uploads/{job_id}/complete
   └─ Lambda receives event
   └─ s3_client.head_object() → verifies file exists in S3
   └─ job_repository.update_job(status='queued')
   └─ sqs_client.send_message() → SQS message {job_id, s3_key}
   └─ Response: {job_id, status: 'queued'}

6. Worker Lambda picks up the message
   └─ SQS Event Source Mapping polls queue
   └─ Lambda receives event with 1 record
   └─ handler.lambda_handler() runs:
      a. parse_job_message() → extract job_id, s3_key
      b. SessionLocal() → connect to RDS PostgreSQL
      c. claim_job_for_processing() → conditional UPDATE
         WHERE status='queued' (idempotency guard)
      d. download_file() → S3 GET to /tmp/original.csv
      e. parse_and_analyse() → validate CSV headers
      f. clean() → trim whitespace, deduplicate rows
      g. write_csv() → write to /tmp/result.csv (RFC 4180 CRLF)
      h. upload_file() → S3 PUT processed/{user_id}/{job_id}/result.csv
      i. complete_job() → UPDATE status='completed', metadata
      j. SQS auto-deletes message on success
```

### Download Flow (user downloads the processed file):

```
1. Browser → API Gateway: GET /jobs/{job_id}/download
   Authorization: Bearer <access_token>

2. API Gateway validates JWT → Lambda receives event

3. FastAPI:
   └─ get_current_user() extracts user_id from JWT
   └─ get_job_by_id() → SELECT from PostgreSQL
   └─ Verify ownership (job.user_id == token sub)
   └─ Verify status == 'completed'
   └─ s3_client.generate_presigned_get_url() → S3 presigned URL
   └─ Response: {download_url, filename, expires_in_seconds}

4. Browser downloads via presigned URL
   └─ Browser → S3: GET <presigned_url>
   └─ S3 returns the processed CSV file
```

---

## 6. 10 Practical Exercises

### Exercise 1: Upload a CSV via the frontend

1. Go to https://d38s2v3jcf0q6l.cloudfront.net
2. Sign in: `vishal@test.com` / `CsvForge2024!Dev`
3. Upload a CSV file with some duplicate rows
4. Watch the job status change: queued → processing → completed
5. Download the processed file and verify duplicates were removed

### Exercise 2: Send a raw SQS message via CLI

```bash
aws sqs send-message \
  --queue-url https://sqs.ap-southeast-2.amazonaws.com/431251892391/csvforge-jobs-dev \
  --message-body '{"job_id":"<your-job-id>","s3_key":"uploads/<user_id>/<job_id>/original.csv"}' \
  --region ap-southeast-2 --profile csvforge
```
Then check the job status via API. The worker will process it within seconds.

### Exercise 3: Check CloudWatch logs for the worker

```bash
aws logs tail /aws/lambda/csvforge-worker-dev --since 10m \
  --region ap-southeast-2 --profile csvforge
```
Look for START, END, REPORT lines. The REPORT shows Duration, Billed Duration, Memory Used, and Init Duration (cold start).

### Exercise 4: Explore the RDS configuration

```bash
aws rds describe-db-instances \
  --db-instance-identifier csvforge-db-dev \
  --region ap-southeast-2 --profile csvforge \
  --output table
```
Note: InstanceClass, Engine, StorageType, AllocatedStorage, PubliclyAccessible (should be false).

### Exercise 5: Create a CloudWatch alarm for DLQ messages

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name csvforge-dlq-messages \
  --metric-name ApproximateNumberOfMessagesVisible \
  --namespace AWS/SQS \
  --dimensions Name=QueueName,Value=csvforge-jobs-dev-dlq \
  --evaluation-periods 1 --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --region ap-southeast-2 --profile csvforge
```
This alerts you when any message lands in the Dead-Letter Queue.

### Exercise 6: Add yourself to the admin group

```bash
aws cognito-idp admin-add-user-to-group \
  --user-pool-id ap-southeast-2_dTJDWZwLM \
  --username vishal@test.com \
  --group-name csvforge-admins \
  --region ap-southeast-2 --profile csvforge
```
Then re-login and try GET /admin/jobs (should return all jobs across all users).

### Exercise 7: Upload an invalid CSV

Create a CSV with missing headers or duplicate column names. Upload it and check the job status — it should become `failed` with a failure_reason explaining the validation error.

### Exercise 8: Check the VPC endpoints in the AWS Console

Go to VPC → Endpoints in the AWS Console. You'll see:
- S3 gateway endpoint (free, route-table based)
- SQS interface endpoint (PrivateLink, ENI-based)
- Secrets Manager interface endpoint

### Exercise 9: Rotate the DB secret in Secrets Manager

```bash
aws secretsmanager rotate-secret \
  --secret-id csvforge/db/dev \
  --region ap-southeast-2 --profile csvforge
```
Note: Without a rotation Lambda configured, this will fail — but it demonstrates the command. In production, you'd configure a rotation Lambda that changes the RDS password.

### Exercise 10: Create a CloudFront invalidation

```bash
aws cloudfront create-invalidation \
  --distribution-id E3VJEQ46S3Q51H \
  --paths "/*" \
  --region ap-southeast-2 --profile csvforge
```
This forces CloudFront to fetch fresh copies from S3. Used after deploying a new frontend build.

---

## 7. 30 Interview Questions with Model Answers

### VPC & Networking

**Q1: What is a VPC and why would you put a Lambda function inside one?**
A VPC is your private, isolated network in AWS. By default, Lambda runs outside any VPC and can reach the internet but not private resources like RDS. Putting Lambda inside a VPC gives it a private IP in your subnet so it can talk to RDS over port 5432 without the database being exposed to the internet.

**Q2: What is the difference between a public and private subnet?**
A public subnet has a route to an Internet Gateway (IGW), so resources in it can have public IPs and be reached from the internet. A private subnet has no route to an IGW — resources get no public IP and are only reachable from within the VPC. RDS lives in a private subnet; Lambda also runs in private subnets but reaches AWS services via VPC endpoints.

**Q3: What is a VPC endpoint and when would you use one?**
A VPC endpoint lets resources in a private subnet reach AWS services (S3, SQS, Secrets Manager) without going through the internet or a NAT Gateway. Gateway endpoints (S3, DynamoDB) are free and modify route tables. Interface endpoints (PrivateLink) create ENIs in your subnets and cost ~$0.01/hr per AZ. They save money and improve security by keeping traffic on the AWS backbone.

**Q4: Why would you use two Availability Zones for subnets?**
Two AZs provide basic fault tolerance — if one data center goes down, the other still serves traffic. AWS requires RDS subnet groups to span at least 2 AZs even for Single-AZ instances (for maintenance failover). Lambda also distributes invocations across subnets in different AZs for resilience.

### S3

**Q5: What is a presigned URL and why is it used for file uploads?**
A presigned URL is a time-limited, signed S3 URL that grants temporary access to a specific operation on a specific object. It lets the browser upload directly to S3, bypassing Lambda's 6 MB payload limit. The API generates the URL using IAM credentials; the browser uses the URL without needing AWS credentials.

**Q6: What is the difference between S3 object storage and EBS block storage?**
S3 stores complete objects by key over HTTP — no partial writes, no filesystem. EBS presents raw disk blocks managed by an OS filesystem. S3 scales to exabytes (each object is independent); EBS is bounded by one disk. S3 is for files; EBS is for databases and boot volumes.

**Q7: How does S3 CORS configuration work?**
S3's CORS configuration tells the browser which origins, methods, and headers are allowed for cross-origin requests. When the browser makes a PUT to S3 from a different origin (e.g., localhost:5173), S3 returns CORS headers that the browser checks. Without this configuration, the browser blocks the upload before it's sent.

### SQS

**Q8: What is the SQS visibility timeout and why must it be longer than the Lambda timeout?**
When a consumer receives a message, SQS hides it for VisibilityTimeout seconds. If the consumer crashes, the message reappears after the timeout. If the timeout is shorter than the Lambda timeout, the message becomes visible while Lambda is still processing — causing another Lambda to pick it up and process it twice. CSVForge sets it to 1800s (6 × 300s Lambda timeout).

**Q9: What is a Dead-Letter Queue and when would you use one?**
A DLQ is a separate queue that receives messages after MaxReceiveCount failed processing attempts. It prevents poison-pill messages (that always crash the consumer) from blocking the queue forever. Messages sit in the DLQ for human investigation. You can replay them after fixing the bug.

**Q10: Standard vs FIFO SQS queues — which would you choose?**
Standard: at-least-once delivery, best-effort ordering, unlimited throughput. Requires idempotent consumers. FIFO: exactly-once, strict ordering, 3000 msg/s cap. CSVForge uses Standard because CSV jobs are independent and order doesn't matter. The idempotency guard (`WHERE status='queued'`) handles duplicate deliveries safely.

**Q11: What does "at-least-once delivery" mean and how do you handle it?**
At-least-once means a message may be delivered more than once (during redelivery after a crash). Your consumer must be idempotent — processing the same message twice should produce the same result as processing it once. CSVForge uses a conditional UPDATE (`WHERE status='queued'`) so the second processing attempt finds nothing to update.

### RDS

**Q12: Why put RDS in a private subnet?**
A database should never be directly accessible from the internet. A private subnet has no route to an Internet Gateway, so RDS gets no public IP and can only be reached from within the VPC. Lambda functions in the same VPC connect via private IP over port 5432.

**Q13: What is a DB subnet group?**
A named collection of subnets across at least 2 AZs. RDS requires this even for Single-AZ instances because it needs the option to failover to another AZ during maintenance. The subnet group tells RDS which subnets it's allowed to use.

**Q14: RDS vs DynamoDB — when would you choose which?**
RDS (PostgreSQL) for: ACID transactions, complex queries, joins, relational schemas. DynamoDB for: simple key-value lookups, massive scale (millions of TPS), predictable single-digit-millisecond latency. CSVForge needs conditional UPDATEs for job state transitions, which PostgreSQL handles natively with row-level locking.

### Secrets Manager

**Q15: Why use Secrets Manager instead of environment variables?**
Lambda env vars are visible via `lambda:GetFunctionConfiguration` and appear in CloudFormation templates. Secrets Manager encrypts the secret (KMS) and Lambda fetches it at runtime via IAM — the password never appears in plaintext in your code, templates, or logs. Secrets Manager also supports automatic rotation.

**Q16: Secrets Manager vs Parameter Store?**
Parameter Store is cheaper ($0.05/10K calls) and simpler. Secrets Manager costs $0.40/secret/month but adds automatic rotation, cross-account access, and integration with RDS. For a DB password that needs rotation, use Secrets Manager. For a simple config value, Parameter Store is sufficient.

### Cognito

**Q17: What is the difference between a Cognito ID token and access token?**
The ID token contains identity claims (email, sub, groups) — it says WHO the user is. The access token contains authorization scopes — it says WHAT the user can do. API Gateway validates the access token. FastAPI reads the `sub` claim from it to identify the user.

**Q18: Why does the app client have no secret?**
Browser apps are "public clients" — JavaScript is visible to the user, so a secret can't be kept. The authentication code flow with PKCE provides security without a secret. PKCE generates a random challenge on each login and verifies it with a verifier, preventing code interception.

**Q19: How does Cognito group-based authorization work?**
A user is added to a Cognito group. On login, Cognito includes `cognito:groups` in the JWT. API Gateway passes the claim to Lambda. FastAPI reads the groups and sets `is_admin=True` if the user is in `csvforge-admins`. The `require_admin()` dependency checks this before allowing access to /admin/jobs.

### Lambda

**Q20: What is a Lambda cold start and how can you reduce it?**
A cold start is the initial latency when Lambda provisions a new container: downloading code, starting the runtime, and initializing the handler. It adds 1-10 seconds. To reduce: use provisioned concurrency (pre-warm instances), keep deployment packages small, minimize imports at module level, and use SnapStart (for Java). For container images, smaller images pull faster.

**Q21: Why does the worker use a container image but the API uses a zip?**
The worker has psycopg[binary] — a C extension compiled against libpq. On macOS, the .so is darwin/arm64 — won't run on Lambda's Amazon Linux. A Docker container built FROM the official Lambda base image ensures correct compilation. The API has no OS-specific C dependencies, so a zip works fine.

**Q22: Why BatchSize=1 for the SQS event source mapping?**
With batch > 1, a Lambda exception causes ALL messages in the batch to retry — including successfully processed ones. CSV jobs are independent and potentially large (50 MB). BatchSize=1 means one failure affects one job, not the whole batch. Partial batch reporting only helps when BatchSize > 1.

**Q23: What is Mangum and why is it needed?**
Mangum wraps a FastAPI (ASGI) app so it can receive API Gateway events. Lambda's event format (a JSON dict) is not ASGI — Mangum translates the API Gateway event into an ASGI request, passes it to FastAPI, and packages the ASGI response back into Lambda's response format. Without Mangum, you'd have to rewrite the entire app to use Lambda's raw event model.

### API Gateway

**Q24: HTTP API vs REST API — which would you choose?**
HTTP API: lower latency, lower cost (~70% cheaper), native JWT support, simpler. REST API: API keys, usage plans, request validation, transformation templates. For a simple JWT-authenticated proxy to Lambda, HTTP API is sufficient. Choose REST API when you need API keys, usage plans, or request/response transformation.

**Q25: How does the JWT Authoriser work?**
It validates the Bearer token against a JWKS endpoint (Cognito's public keys). It checks the signature, issuer (iss), expiration (exp), and audience (aud). If valid, it injects decoded claims into the Lambda event at `requestContext.authorizer.jwt.claims`. If invalid, it returns 401 — Lambda never runs. No custom code needed.

**Q26: Why do CORS preflights fail with a JWT Authoriser and how do you fix it?**
OPTIONS preflight requests never carry an Authorization header (the browser is asking permission first). The JWT Authoriser on the $default route rejects them with 401. Fix: add an explicit `OPTIONS /{proxy+}` route with `AuthorizationType: NONE` so preflights bypass the authoriser. Also add the frontend origin to the API Gateway's CorsConfiguration.AllowOrigins.

### CloudFront

**Q27: Why use CloudFront + private S3 instead of S3 static website hosting?**
S3 static website hosting serves over HTTP only (no HTTPS) and from one region. CloudFront provides HTTPS (required for Cognito tokens), global edge caching, DDoS protection, and custom error responses for SPA routing. The bucket stays private — only CloudFront reads it via OAC.

**Q28: How does SPA routing work with CloudFront?**
React Router uses paths like /jobs/abc123. When a user navigates directly there, S3 returns 403/404 (the object doesn't exist). CloudFront's custom error response maps 403→200 and 404→200, serving index.html. React Router then reads the URL and routes client-side. Without this, users see a 404 on direct navigation.

### IAM

**Q29: What is least privilege IAM and how does CSVForge apply it?**
Every role has exactly the permissions it needs — no more. The worker Lambda can read `uploads/*` and write `processed/*` but cannot write to `uploads/` (preventing input corruption). The API Lambda can generate presigned URLs and send SQS messages but cannot receive SQS messages. Permissions are scoped to specific ARNs and prefixes, limiting blast radius.

**Q30: What is the difference between a trust policy and a permission policy?**
The trust policy (assume role policy) says WHO can assume the role — e.g., `lambda.amazonaws.com`. The permission policy says WHAT the role can do once assumed — e.g., `s3:GetObject on bucket-X`. Both are needed: the trust policy grants the ability to become the role; the permission policy grants the abilities the role has.

---

## Appendix: AWS CLI Quick Reference

```bash
# Check all stacks
aws cloudformation describe-stacks --region ap-southeast-2 --profile csvforge --query 'Stacks[].{Name:StackName,Status:StackStatus}' --output table

# Check API health
curl -s https://wem0j5l8ad.execute-api.ap-southeast-2.amazonaws.com/health

# Check worker logs
aws logs tail /aws/lambda/csvforge-worker-dev --since 10m --region ap-southeast-2 --profile csvforge

# Check RDS status
aws rds describe-db-instances --db-instance-identifier csvforge-db-dev --region ap-southeast-2 --profile csvforge --query 'DBInstances[0].{Status:DBInstanceStatus,Engine:Engine}' --output table

# Check SQS queue depth
aws sqs get-queue-attributes --queue-url https://sqs.ap-southeast-2.amazonaws.com/431251892391/csvforge-jobs-dev --attribute-names All --region ap-southeast-2 --profile csvforge

# Create a test user
aws cognito-idp admin-create-user --user-pool-id ap-southeast-2_dTJDWZwLM --username test@example.com --temporary-password 'TempPass123!' --message-action SUPPRESS --region ap-southeast-2 --profile csvforge
aws cognito-idp admin-set-user-password --user-pool-id ap-southeast-2_dTJDWZwLM --username test@example.com --password 'NewPass123!' --permanent --region ap-southeast-2 --profile csvforge

# Get auth token
aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH --auth-parameters USERNAME=test@example.com,PASSWORD='NewPass123!' --client-id 5r9anb9elko6k79daq21d74q0e --region ap-southeast-2 --profile csvforge --query 'AuthenticationResult.AccessToken' --output text

# Invalidate CloudFront
aws cloudfront create-invalidation --distribution-id E3VJEQ46S3Q51H --paths "/*" --region ap-southeast-2 --profile csvforge

# Re-authenticate AWS session
aws login --region ap-southeast-2 --profile csvforge
```
