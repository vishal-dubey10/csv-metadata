# Sheet 3 — AWS Concepts in CSVForge

> Every service explained from first principles with the exact code that
> uses it. File paths are real.

---

## 1. IAM — Identity and Access Management

IAM is the permission system for all of AWS. Nothing can call an AWS API
without being identified and authorised.

**Two concepts to know:**

| Concept | What it is | CSVForge example |
|---|---|---|
| **Role** | A named set of permissions that an AWS service *assumes* | `csvforge-api-role` — the API Lambda assumes this role |
| **Policy** | A JSON document that grants or denies specific actions | Allows `secretsmanager:GetSecretValue` on a specific ARN |

```json
// IAM Policy for the API Lambda execution role
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:ap-south-1:...:secret:csvforge-db-secret"
      // "Action" = what operation
      // "Resource" = on which specific thing (not all secrets — only this one)
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:HeadObject"],
      "Resource": "arn:aws:s3:::csvforge-files-dev/*"
      // /* means any object inside this bucket
    },
    {
      "Effect": "Allow",
      "Action": "sqs:SendMessage",
      "Resource": "arn:aws:sqs:ap-south-1:...:csvforge-jobs-dev"
    }
  ]
}
```

**Least privilege principle:** The API Lambda can only do exactly what it needs.
No `s3:*`, no `iam:*`, no `ec2:*`. If the Lambda is compromised, the blast
radius is limited to these three operations on these specific resources.

**How Lambda gets credentials automatically:**

```python
# File: backend/s3_client.py

def get_s3_client():
    return boto3.client("s3", region_name="ap-south-1")
    # No credentials passed explicitly.
    # boto3 reads them from the IAM execution role automatically
    # via the metadata service at http://169.254.169.254/...
    # This is the "IAM instance profile" / "execution role" credential chain.
```

The boto3 credential chain (in order):
1. Environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
2. `~/.aws/credentials` file (local dev)
3. IAM role attached to the Lambda function ← **production path**

---

## 2. Lambda — Serverless Functions

Lambda runs your code without you managing any servers. You upload code,
AWS runs it on-demand.

**Key properties:**

| Property | Value | Why |
|---|---|---|
| Cold start | ~500ms–2s | Container creation + module import |
| Warm invocation | ~1–5ms overhead | Container already running, module already loaded |
| Max duration | 15 minutes | Hard limit — CSV processing must finish in time |
| Memory | 512 MB (configured) | Enough for a 50 MB CSV in memory |
| Concurrency | One request per container | Why pool_size=1 in connection.py |

**Handler protocol:**

Lambda calls a function with signature `handler(event, context)`:

```python
# File: backend/main.py

from mangum import Mangum
handler = Mangum(app)
# "handler" must match the Handler field in the Lambda config:
#   Handler: backend.main.handler
# Lambda imports backend.main, then calls backend.main.handler(event, context)
```

**What the Lambda event looks like (from API Gateway):**

```json
{
  "version": "2.0",
  "rawPath": "/jobs",
  "requestContext": {
    "http": { "method": "POST" },
    "authorizer": {
      "jwt": {
        "claims": {
          "sub": "aabb-ccdd-1122-3344",
          "cognito:groups": "csvforge-admins",
          "token_use": "access"
        }
      }
    }
  },
  "body": "{\"user_email\": \"alice@example.com\", \"original_filename\": \"data.csv\"}"
}
```

Mangum translates this JSON blob into an ASGI request that FastAPI understands.

**Cold start explained:**

```
Request #1 (cold start, ~1500ms):
  AWS creates a new container
  Python interpreter starts
  backend/main.py is imported (creates Engine, Mangum, etc.)
  handler(event, context) is called
  Response returned
  Container stays alive (frozen, waiting)

Request #2 (warm, ~5ms):
  Existing container is thawed
  handler(event, context) is called (module already imported)
  Response returned

MODULE-LEVEL CODE runs ONCE at cold start:
  DATABASE_URL = _build_database_url()   ← one Secrets Manager call
  engine = create_engine(DATABASE_URL)   ← one connection pool created
```

```python
# File: backend/db/connection.py

# This runs at IMPORT TIME (cold start only):
DATABASE_URL: str = _build_database_url()
engine = create_engine(DATABASE_URL, pool_size=1, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
```

---

## 3. API Gateway — HTTP Traffic to Lambda

API Gateway sits in front of Lambda. It:
1. Accepts HTTPS requests from the browser
2. Validates the JWT (before Lambda runs)
3. Forwards the validated request to Lambda
4. Returns Lambda's response to the browser

**JWT Authoriser — the most important concept:**

```
Browser sends:
  GET /jobs
  Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI...

API Gateway JWT Authoriser:
  1. Splits the JWT into header.payload.signature
  2. Downloads Cognito's public keys (JWKS) — cached for ~1 hour
  3. Verifies: signature is valid for this payload with Cognito's key
  4. Verifies: exp (expiry) has not passed
  5. Verifies: iss (issuer) == https://cognito-idp.ap-south-1.amazonaws.com/ap-south-1_AbC123DeF
  6. Verifies: aud (audience) == YOUR_APP_CLIENT_ID

If any check fails → 401 Unauthorized
  Lambda never runs. No DB query. No cost.

If all checks pass → inject claims into event.requestContext.authorizer.jwt.claims
  Lambda runs. FastAPI reads claims from request.scope["aws.event"].
```

**Why FastAPI doesn't validate the JWT itself:**

```python
# File: backend/auth.py

# FastAPI DOES NOT validate the signature.
# It just READS the claims that API Gateway already validated.
aws_event = request.scope.get("aws.event", {})
claims = (
    aws_event
    .get("requestContext", {})
    .get("authorizer", {})
    .get("jwt", {})
    .get("claims", {})
)
user_id = claims.get("sub", "")
# If we're here, API Gateway already verified the token is genuine.
```

If FastAPI validated the JWT, it would need to download Cognito's JWKS on
every cold start (~100ms latency, extra network call). API Gateway handles it
at the infrastructure layer, faster and without code.

---

## 4. Cognito — Managed Authentication

Cognito is a managed user directory. It handles sign-up, login, password
resets, and token issuance. You don't write any auth logic.

**Three token types Cognito issues:**

| Token | Contains | Used for |
|---|---|---|
| **Access token** | sub, groups, exp | API calls — sent as `Authorization: Bearer` |
| **ID token** | email, name, sub | Display info — user's profile |
| **Refresh token** | opaque | Exchanges for new access/ID tokens |

**What the access token JWT payload looks like:**

```json
{
  "sub": "aabb-ccdd-1122-3344",
  "cognito:groups": "csvforge-admins",
  "token_use": "access",
  "exp": 1700000000,
  "iss": "https://cognito-idp.ap-south-1.amazonaws.com/ap-south-1_AbC123DeF",
  "client_id": "1abc2def3ghi4jkl5mno"
}
```

**`sub` — the permanent user ID:**

```python
# File: backend/auth.py

user_id = claims.get("sub", "")
# sub = "aabb-ccdd-1122-3344"
# This UUID is assigned at account creation and NEVER changes.
# Even if the user updates their email, sub stays the same.

# File: backend/models/job.py
user_id: Mapped[str] = mapped_column(
    VARCHAR(128),
    comment="Cognito sub claim — authoritative user identity",
)
# Every job stores the sub as its owner ID.
# Ownership check: WHERE user_id = sub (never WHERE email = email)
```

**Groups (for admin access):**

```python
# File: backend/auth.py

groups_raw = claims.get("cognito:groups", "")
# "cognito:groups" is space-separated: "csvforge-admins other-group"

groups = groups_raw.split() if groups_raw else []
is_admin = "csvforge-admins" in groups
```

**How the frontend talks to Cognito:**

```typescript
// File: frontend/src/auth/AuthContext.tsx

import { CognitoUser, CognitoUserPool, AuthenticationDetails } from 'amazon-cognito-identity-js'

const userPool = new CognitoUserPool({
  UserPoolId: "ap-south-1_AbC123DeF",
  ClientId:   "1abc2def3ghi4jkl5mno",
})

const cognitoUser = new CognitoUser({ Username: email, Pool: userPool })
const authDetails = new AuthenticationDetails({ Username: email, Password: password })

cognitoUser.authenticateUser(authDetails, {
  onSuccess: (session) => {
    const accessToken = session.getAccessToken().getJwtToken()
    // Store in memory; send as Authorization: Bearer on every API call
  },
  onFailure: (err) => { ... }
})
```

---

## 5. S3 — Object Storage

S3 (Simple Storage Service) stores files as "objects" (key + bytes + metadata).
There are no directories — just keys that look like paths.

**Two operations CSVForge uses S3 for:**

| Operation | Who | How |
|---|---|---|
| Upload raw CSV | Browser (direct) | Presigned PUT URL |
| Download processed CSV | Browser (direct) | Presigned GET URL |
| Read CSV for processing | Worker Lambda | `download_file()` |
| Write processed CSV | Worker Lambda | `upload_file()` |

**Presigned URL — the core concept:**

```python
# File: backend/s3_client.py

def generate_presigned_upload_url(bucket, key, expiry_seconds=900):
    client = get_s3_client()
    url = client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ContentType": "text/csv",
        },
        ExpiresIn=expiry_seconds,    # 15 minutes
    )
    return url
```

**How it works cryptographically:**

```
1. boto3 computes HMAC-SHA256(canonical_request, execution_role_secret_key)
2. Embeds the signature + expiry + key ID in the URL as query params
3. Browser sends PUT to that URL
4. S3 recomputes the HMAC using the key ID to look up the secret key
5. If signatures match and not expired → S3 accepts the PUT
6. The browser NEVER receives AWS credentials — only a signed URL
```

**HeadObject — checking a file without downloading it:**

```python
# File: backend/s3_client.py

def object_exists(bucket, key) -> bool:
    client = get_s3_client()
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True       # object exists
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False  # object doesn't exist — normal "not yet uploaded" case
        raise             # 403, 503, etc. — real error, propagate it

def get_object_size(bucket, key) -> int:
    client = get_s3_client()
    response = client.head_object(Bucket=bucket, Key=key)
    return response["ContentLength"]   # bytes, from S3's authoritative metadata
```

HeadObject fetches metadata only — zero bytes of file content transferred.
Used to verify upload happened AND to check file size (can't trust client claim).

**Key naming convention:**

```
uploads/{user_id}/{job_id}/original.csv    ← raw file, written by browser
processed/{user_id}/{job_id}/result.csv    ← processed file, written by worker
```

Including `user_id` enables future IAM conditions to restrict each user to
only their own prefix. The `job_id` makes every key globally unique.

---

## 6. SQS — Message Queue

SQS (Simple Queue Service) decouples the API from the worker. The API
enqueues a message and returns immediately. The worker reads from the queue
independently and processes jobs asynchronously.

**Why a queue instead of calling the worker directly:**

```
WITHOUT SQS:
  Browser → API → Worker.process_csv() → waits 15 seconds → returns

  Problem: Browser waits 15 seconds. If the network drops, the job is lost.

WITH SQS:
  Browser → API → SQS.enqueue(job_id) → returns in <100ms
            Worker reads from queue → processes independently

  Browser waits <100ms. Network drop doesn't lose the job.
  If the worker crashes, SQS re-delivers the message.
```

**Sending a message:**

```python
# File: backend/sqs_client.py

def send_job_message(queue_url, job_id, s3_key) -> str:
    client = get_sqs_client()
    body = json.dumps({"job_id": job_id, "s3_key": s3_key})
    # Only identifiers — not file content
    # SQS limit is 256 KB; a 50 MB CSV would never fit
    response = client.send_message(QueueUrl=queue_url, MessageBody=body)
    return response["MessageId"]   # SQS-assigned ID, different from job_id
```

**Receiving a message (in the Worker Lambda):**

```python
# File: backend/sqs_client.py

def parse_job_message(body: str) -> dict:
    return json.loads(body)
    # Input:  '{"job_id": "550e8400-...", "s3_key": "uploads/..."}'
    # Output: {"job_id": "550e8400-...", "s3_key": "uploads/..."}

# The SQS event the Worker Lambda receives:
{
  "Records": [{
    "body": "{\"job_id\": \"550e8400-...\", \"s3_key\": \"uploads/...\"}",
    "receiptHandle": "AQEB...",   # used to delete the message after processing
    "messageId": "1234-5678-..."
  }]
}
```

**At-least-once delivery — the idempotency requirement:**

```
SQS guarantees delivery "at least once".
This means the same message CAN arrive twice:
  - Lambda timed out after receiving but before deleting the message
  - Rare SQS infrastructure events

The Worker MUST be idempotent:
  processing job-X twice must be safe — same result, no double-writing.

How CSVForge handles it:
  claim_job_for_processing() uses a conditional UPDATE:
    UPDATE jobs SET status='processing'
    WHERE id=job_id AND status IN ('queued', 'processing')

  First delivery:  status='queued'  → matches WHERE, rowcount=1, process
  Second delivery: status='processing' → still matches WHERE, rowcount=1
                   Worker re-processes — but overwrites same S3 key
                   (deterministic key = safe overwrite)
```

---

## 7. Secrets Manager — Secure Credential Storage

Secrets Manager stores sensitive values (passwords, API keys) encrypted at rest.
The Lambda execution role is granted `GetSecretValue` on a specific secret ARN.
The password never appears in code, config files, or the AWS Console.

```python
# File: backend/db/connection.py

secret_arn = os.environ["DB_SECRET_ARN"]   # an ARN, not a password

client = boto3.client("secretsmanager")
response = client.get_secret_value(SecretId=secret_arn)
secret = json.loads(response["SecretString"])
# secret = {"username": "csvforge", "password": "actual-password-here"}
# The password is ONLY in memory, ONLY during cold start, NEVER logged
```

**Why not just put the password in a Lambda environment variable?**

```
Lambda env vars → visible in AWS Console to anyone with Lambda:GetFunction
Secrets Manager → encrypted, IAM-gated, audit-logged, rotation-supported

If you're in a team:
  - Developer A deploys the Lambda (has Lambda access)
  - Developer B manages billing (has Lambda:GetFunction to view config)
  - Neither can see the DB password in Secrets Manager without explicit IAM grant

Secrets Manager also supports automatic rotation:
  - Every 30 days, rotate the password
  - The app gets the new password on next cold start
  - No code changes, no redeployment
```

---

## 8. RDS — Managed Relational Database

RDS (Relational Database Service) is a managed PostgreSQL instance. AWS
handles backups, patching, failover, and hardware. You write SQL and use it
like any PostgreSQL database.

**How the app connects:**

```python
# File: backend/db/connection.py

# Connection URL format:
# postgresql+psycopg://{username}:{password}@{host}:{port}/{dbname}

engine = create_engine(
    DATABASE_URL,
    pool_size=1,         # one connection per Lambda container
    max_overflow=0,
    pool_pre_ping=True,  # probe dead connections before use
)
```

**Connection pool math:**

```
RDS t3.micro max_connections ≈ 100

At 50 concurrent Lambda invocations:
  pool_size=1 → 50 × 1 = 50 connections  ✅ under the limit
  pool_size=5 → 50 × 5 = 250 connections ❌ exceeds the limit

At 100 concurrent Lambda invocations:
  pool_size=1 → 100 connections  ✅ exactly at the limit
```

**VPC — why RDS is not publicly accessible:**

RDS runs inside a VPC (Virtual Private Cloud) — a private network. Only
resources inside the same VPC (the Lambda function) can connect. The
database has no public internet endpoint. Even if someone got the
credentials, they can't reach the database from outside the VPC.

---

## 9. CloudFormation — Infrastructure as Code

CloudFormation lets you define AWS resources as YAML/JSON files. Instead
of clicking through the Console, you describe what you want and CloudFormation
creates/updates/deletes resources to match.

**Example: defining the SQS queue:**

```yaml
# File: buildspec/buildspec-infra-changeset.yml (references the template)
# The template itself is in infrastructure/ (not shown but follows this pattern)

Resources:
  JobsQueue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub "csvforge-jobs-${Environment}"
      # !Sub substitutes ${Environment} from Parameters
      VisibilityTimeout: 60     # seconds a message is hidden after received
      MessageRetentionPeriod: 86400  # 24 hours: how long SQS keeps unprocessed messages

  DeadLetterQueue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub "csvforge-jobs-dlq-${Environment}"
      # Messages that fail MaxReceiveCount times go here for investigation
```

**Outputs — values other stacks or Lambda env vars need:**

```yaml
Outputs:
  QueueUrl:
    Value: !Ref JobsQueue
    Export:
      Name: !Sub "csvforge-queue-url-${Environment}"
  # Other stacks can import this with:
  # !ImportValue "csvforge-queue-url-dev"
```

**Why CloudFormation instead of clicking in the Console:**

```
Console clicks:
  - Not reproducible (try to recreate from memory 6 months later)
  - Not reviewable (no diff — was this change intentional?)
  - Not versioned (no rollback if you made a mistake)

CloudFormation:
  - Every resource is code in git — reviewable, versioned, diffable
  - `aws cloudformation deploy` creates or updates to match the template
  - Rollback: deploy an older version of the template
  - Multiple environments: same template, different Environment parameter
```

---

## 10. boto3 — The AWS Python SDK

boto3 is the official Python library for all AWS API calls. It handles
signing, retries, and response parsing.

```python
# File: backend/s3_client.py

import boto3

# Create a client for one service
client = boto3.client("s3", region_name="ap-south-1")

# S3 API calls:
client.head_object(Bucket="csvforge-files-dev", Key="uploads/.../original.csv")
client.generate_presigned_url("put_object", Params={...}, ExpiresIn=900)
client.upload_file(Filename="/tmp/result.csv", Bucket="...", Key="processed/...")
client.download_file(Bucket="...", Key="...", Filename="/tmp/input.csv")

# File: backend/sqs_client.py
client = boto3.client("sqs", region_name="ap-south-1")
client.send_message(QueueUrl="https://sqs....", MessageBody=body)

# File: backend/db/connection.py
client = boto3.client("secretsmanager")
client.get_secret_value(SecretId="arn:aws:secretsmanager:...")
```

**boto3 error handling:**

```python
# File: backend/s3_client.py

from botocore.exceptions import ClientError

try:
    client.head_object(Bucket=bucket, Key=key)
except ClientError as e:
    # ClientError wraps all AWS API errors
    error_code = e.response["Error"]["Code"]
    # "404" → object not found
    # "403" → permission denied
    # "503" → service unavailable
    if error_code == "404":
        return False   # expected case
    raise              # unexpected error — propagate
```

---

## 11. VPC — Virtual Private Cloud

A VPC is an isolated virtual network inside AWS. Resources in a VPC can
talk to each other but are not reachable from the internet by default.

```
CSVForge VPC:
  ┌─────────────────────────────────────────────────────┐
  │  Private subnet ap-south-1a                         │
  │  ┌───────────┐    ┌────────────────────────────┐   │
  │  │ Lambda    │───►│ RDS PostgreSQL              │   │
  │  │ functions │    │ (not publicly accessible)   │   │
  │  └───────────┘    └────────────────────────────┘   │
  └─────────────────────────────────────────────────────┘
              │
         Internet Gateway (outbound only for Lambda→SQS, Lambda→S3)
```

Lambda connects to SQS and S3 via VPC Endpoints or via the internet
gateway. RDS has no internet route — only Lambda inside the VPC can reach it.

---

## 12. ARN — Amazon Resource Name

An ARN is a unique identifier for any AWS resource. Format:

```
arn:{partition}:{service}:{region}:{account-id}:{resource}

arn:aws:s3:::csvforge-files-dev
│   │   │   │  └─ resource: bucket name (no region/account for global S3)
│   │   │   └─── account ID (empty for S3 — global namespace)
│   │   └─────── region (empty for S3 — global)
│   └─────────── service: s3
└─────────────── partition: aws (commercial)

arn:aws:secretsmanager:ap-south-1:123456789012:secret:csvforge-db-secret
│   │   │              │            │             └─ resource name
│   │   │              │            └────────────── account ID
│   │   │              └─────────────────────────── region
│   │   └────────────────────────────────────────── service
└───┴─────────────────────────────────────────────── aws

arn:aws:lambda:ap-south-1:123456789012:function:csvforge-api-dev
```

ARNs are used in IAM policies to specify exactly which resource an
action is allowed on. `"Resource": "arn:aws:s3:::csvforge-files-dev/*"`
means "any object inside this specific bucket" — not all S3 buckets.

---

## AWS Concepts Quick Reference

| Concept | Service | One-line explanation | CSVForge usage |
|---|---|---|---|
| **Execution role** | IAM | Permissions a Lambda assumes | API Lambda assumes `csvforge-api-role` |
| **Least privilege** | IAM | Grant only the permissions actually needed | API Lambda: 3 specific Actions on 3 specific Resources |
| **Presigned URL** | S3 | Signed, time-limited URL for one S3 operation | Browser uploads/downloads directly — no proxy |
| **HeadObject** | S3 | Fetch metadata without downloading bytes | Verify upload happened; check file size |
| **Cold start** | Lambda | Container creation + module import overhead | Module-level code (Engine, Secrets call) runs once |
| **Warm invocation** | Lambda | Reuse existing container + module | Pool is reused; `get_db()` runs per request |
| **JWT Authoriser** | API Gateway | Validates Cognito tokens before Lambda runs | 401 returned before FastAPI if token invalid |
| **`sub` claim** | Cognito | Immutable user UUID in every JWT | `job.user_id = sub` for ownership |
| **At-least-once** | SQS | Same message may arrive twice | Idempotent `claim_job_for_processing()` |
| **Claim check pattern** | SQS | Message holds reference, not data | SQS message = {job_id, s3_key} not file bytes |
| **Secrets Manager** | SecretsManager | Encrypted storage for credentials | DB password never in code or env vars |
| **Connection pooling** | RDS | Reuse TCP connections across requests | `pool_size=1` per Lambda container |
| **VPC** | VPC | Private network — RDS unreachable from internet | Only Lambda in same VPC reaches RDS |
| **ARN** | IAM/all | Globally unique resource identifier | Used in IAM policies to target specific resources |
| **CloudFormation** | CloudFormation | Infrastructure as code — resources as YAML | Defines queues, roles, Lambda functions |
| **boto3** | Python SDK | Python library for all AWS API calls | S3, SQS, Secrets Manager calls |
