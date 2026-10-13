# CSVForge AWS Console Deployment Guide

A step-by-step walkthrough of deploying CSVForge to AWS, with:
- Exact Console navigation for every step
- The source code that depends on each resource
- What would break if the resource is misconfigured
- Prediction exercises before you act
- Verification commands after you act

Follow the steps in order. Each step depends on the previous one.

**Region:** ap-south-1 (Mumbai) for all resources.
**Cost estimate:** ~$15–20/month for the full dev stack while running.
**Safety rule:** read the "What would break" section before creating any resource.

---

## Before you start — verify credentials

```bash
aws sts get-caller-identity
```

Expected output: your account ID, user ARN, and user ID. If this fails, run
`aws configure` or `aws sso login` first.

---

# STEP 1 — Secrets Manager: DB credentials secret

**Why first:** RDS needs credentials. Secrets Manager stores them. Every subsequent
step that references the database uses this secret's ARN.

## What Secrets Manager does for CSVForge

Your RDS instance has a master username and password. Those credentials never appear in:
- CloudFormation templates (visible in the Console)
- Lambda environment variables (visible in the Console)
- Git history (permanently accessible)

Instead, Lambda fetches them at cold start by calling Secrets Manager's API.

## The code that reads this secret

Open `backend/db/connection.py` and find `_build_database_url()`:

```python
def _build_database_url() -> str:
    environment = os.getenv("ENVIRONMENT", "production")

    if environment == "local":
        url = os.getenv("DATABASE_URL")
        return url                       # ← local path: read directly

    # Production path ↓
    secret_arn = os.environ["DB_SECRET_ARN"]    # ← reads from Lambda env var
    host = os.environ["DB_HOST"]
    port = os.getenv("DB_PORT", "5432")
    dbname = os.getenv("DB_NAME", "csvforge")

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)   # ← HTTPS call to Secrets Manager
    secret = json.loads(response["SecretString"])              # ← parses JSON

    username = secret["username"]   # ← {"username": "...", "password": "..."}
    password = secret["password"]

    return f"postgresql+psycopg://{username}:{password}@{host}:{port}/{dbname}"
```

This code runs **once at Lambda cold start** when `backend.main` is first imported. The result (`DATABASE_URL`) is module-level — warm invocations reuse it. That's why fixing a Secrets Manager permission requires a cold start to take effect, not just a function update.

## Impact area

| If this is wrong | What fails | Error message |
|-----------------|------------|---------------|
| Wrong username/password in secret | Lambda cold start | `psycopg.OperationalError: authentication failed` |
| Missing `secretsmanager:GetSecretValue` IAM permission | Lambda cold start | `ClientError: AccessDeniedException` |
| Secret ARN wrong in Lambda env var | Lambda cold start | `ResourceNotFoundException` |
| Secret deleted | Lambda cold start | `ResourceNotFoundException` |

All of these produce **HTTP 502** to the API caller (Lambda fails to initialize, API Gateway can't forward the response).

## Console steps

1. Open: **AWS Console → Secrets Manager → ap-south-1**
2. Click **Store a new secret**
3. **Secret type:** Other type of secret
4. **Key/value pairs:**
   - Key: `username` → Value: `csvforge`
   - Key: `password` → Value: choose a strong password (e.g., generate one: `openssl rand -base64 24`)
5. Click **Next**
6. **Secret name:** `csvforge-db-credentials-dev`
   - The exact name matters — `buildspec-migrate.yml` reads `/csvforge/dev/db-secret-arn` from SSM, and `connection.py` reads `DB_SECRET_ARN` from the Lambda env var.
7. **Description:** CSVForge dev RDS master credentials
8. **Automatic rotation:** Disable for now (enable in production)
9. Click **Next → Next → Store**

## Record the ARN

After creation, click the secret name and copy the **Secret ARN**. It looks like:
`arn:aws:secretsmanager:ap-south-1:123456789012:secret:csvforge-db-credentials-dev-AbCdEf`

**Note the random 6-character suffix** (AbCdEf). This is why `lambda-execution-policy.json` uses a trailing wildcard:
```json
"Resource": "arn:aws:secretsmanager:ap-south-1:*:secret:csvforge-db-credentials-dev*"
```

Save the ARN — you'll need it in Steps 3, 4, and 8.

## Verify (CLI)

```bash
aws secretsmanager get-secret-value \
  --secret-id csvforge-db-credentials-dev \
  --query SecretString \
  --output text | python3 -m json.tool
```

Expected:
```json
{
    "username": "csvforge",
    "password": "your-password"
}
```

---

# STEP 2 — Network stack: VPC, subnets, security groups

**Why:** RDS and Lambda live in a private VPC. Without this, RDS would need a public IP (insecure) or Lambda couldn't reach it. All 5 application stacks import outputs from this stack.

## What the network stack creates

```
VPC (10.0.0.0/16)
├── Private Subnet A (10.0.1.0/24, AZ-1)   ← Lambda + RDS
├── Private Subnet B (10.0.2.0/24, AZ-2)   ← Lambda + RDS (for subnet group)
├── LambdaSecurityGroup                     ← all outbound TCP allowed
├── RDSSecurityGroup                        ← inbound 5432 from Lambda SG only
├── VPCEndpointSecurityGroup                ← inbound 443 from Lambda SG
├── S3 Gateway Endpoint                     ← free, routes S3 traffic internally
├── SQS Interface Endpoint                  ← ~$14/month
└── Secrets Manager Interface Endpoint      ← ~$14/month
```

## The code that depends on this

`api.yaml` and `worker.yaml` both reference these outputs:
```yaml
VpcConfig:
  SubnetIds:
    - Fn::ImportValue: !Sub "csvforge-network-dev-PrivateSubnetAId"
    - Fn::ImportValue: !Sub "csvforge-network-dev-PrivateSubnetBId"
  SecurityGroupIds:
    - Fn::ImportValue: !Sub "csvforge-network-dev-LambdaSecurityGroupId"
```

Without the network stack deployed, deploying `api.yaml` fails immediately with:
`Export csvforge-network-dev-PrivateSubnetAId not found`.

## Impact area

| Security group rule | What it protects |
|--------------------|-----------------|
| RDS SG: inbound 5432 from Lambda SG only | RDS is unreachable from anywhere except your Lambda functions |
| Lambda SG: all outbound allowed | Lambda can reach S3 (via gateway endpoint), SQS (via interface endpoint), Secrets Manager (via interface endpoint) |
| VPC Endpoint SG: inbound 443 from Lambda SG | The SQS and Secrets Manager endpoints only accept connections from Lambda |

## Deploy

```bash
aws cloudformation deploy \
  --template-file infrastructure/network.yaml \
  --stack-name csvforge-network-dev \
  --region ap-south-1

# Wait for completion (2-3 minutes for the VPC endpoints)
aws cloudformation wait stack-create-complete \
  --stack-name csvforge-network-dev \
  --region ap-south-1
```

## Verify

```bash
# Check all outputs were created
aws cloudformation describe-stacks \
  --stack-name csvforge-network-dev \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" \
  --output table \
  --region ap-south-1
```

Expected: 7 outputs (VpcId, PrivateSubnetAId, PrivateSubnetBId, LambdaSecurityGroupId, RDSSecurityGroupId, PrivateRouteTableAId, PrivateRouteTableBId).

## Console exploration (learn by looking)

1. **Console → VPC → Your VPCs** — find `csvforge-vpc-dev`. Note the CIDR `10.0.0.0/16`.
2. **Console → VPC → Subnets** — find both private subnets. They have no public IP association.
3. **Console → VPC → Endpoints** — find the S3 gateway endpoint (type: Gateway) and the two interface endpoints (type: Interface). The interface endpoints have private IP addresses in your subnets.
4. **Console → EC2 → Security Groups** — find the three security groups. Click `csvforge-rds-sg` and look at its inbound rules: only port 5432 from the Lambda security group ID — not from a CIDR range.

---

# STEP 3 — Data stack: S3 bucket for CSV files

**Why:** The browser uploads CSV files here. The worker reads from here. Both need the bucket to exist before any uploads work.

## What the bucket stores

```
csvforge-files-dev/
├── uploads/{user_id}/{job_id}/original.csv    ← browser PUT via presigned URL
└── processed/{user_id}/{job_id}/result.csv    ← worker PUT after processing
```

## The code that depends on this

`backend/s3_client.py` — every function reads `S3_BUCKET_NAME`:

```python
def get_s3_client():
    region = os.getenv("AWS_DEFAULT_REGION", "ap-south-1")
    return boto3.client("s3", region_name=region)

def generate_presigned_upload_url(bucket: str, key: str, expiry_seconds: int = 900) -> str:
    client = get_s3_client()
    url = client.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": "text/csv"},
        ExpiresIn=expiry_seconds,
    )
    return url
```

In `backend/main.py`, the upload intent endpoint:
```python
bucket = os.getenv("S3_BUCKET_NAME", "csvforge-files-dev")   # ← reads Lambda env var
upload_url = generate_presigned_upload_url(bucket=bucket, key=s3_key)
```

**Presigned URL mechanics:** when `generate_presigned_url` runs, boto3 computes an HMAC-SHA256 signature over the request parameters (method, bucket, key, expiry) using the Lambda execution role's temporary credentials. The signature is embedded in the URL as a query parameter. When the browser PUTs to that URL, S3 verifies the signature — no IAM credentials needed from the browser.

## The CORS configuration — why it's critical

The bucket's CORS configuration (`data.yaml`) is:
```yaml
CorsConfiguration:
  CorsRules:
    - AllowedMethods: [PUT]
      AllowedOrigins: ["http://localhost:5173"]
      AllowedHeaders: ["*"]
      ExposedHeaders: [ETag]
```

Without this, the browser's preflight OPTIONS request to S3 returns no CORS headers, and the browser blocks the actual PUT. The user sees the upload appear to hang, then fail with a network error — no 4xx response, just silence.

**In production:** replace `http://localhost:5173` with your CloudFront URL. You'll update this after Step 9.

## Deploy

```bash
aws cloudformation deploy \
  --template-file infrastructure/data.yaml \
  --stack-name csvforge-data-dev \
  --region ap-south-1
```

## Verify

```bash
# Bucket exists and has encryption
aws s3api get-bucket-encryption --bucket csvforge-files-dev

# CORS is set
aws s3api get-bucket-cors --bucket csvforge-files-dev

# Public access is blocked
aws s3api get-public-access-block --bucket csvforge-files-dev
```

All three should return without error.

---

# STEP 4 — Queues stack: SQS main queue and DLQ

**Why:** The API publishes a message to SQS after upload confirmation. The worker Lambda reads from SQS. Without this, there's no trigger to process jobs.

## The message flow

```
POST /uploads/{job_id}/complete  (API Lambda)
    │
    │  sqs_client.send_job_message(queue_url, job_id, s3_key)
    │  body: {"job_id": "abc123", "s3_key": "uploads/user1/abc123/original.csv"}
    ▼
SQS queue (csvforge-jobs-dev)
    │  VisibilityTimeout: 1800s — message hidden during processing
    │  MaxReceiveCount: 5 → DLQ after 5 failures
    ▼
Lambda event source mapping polls → triggers lambda_handler(event, context)
```

## The code that depends on this

`backend/sqs_client.py`:
```python
def send_job_message(queue_url: str, job_id: str, s3_key: str) -> str:
    client = get_sqs_client()
    body = json.dumps({"job_id": job_id, "s3_key": s3_key})
    response = client.send_message(QueueUrl=queue_url, MessageBody=body)
    return response["MessageId"]
```

`backend/main.py`, upload complete endpoint:
```python
queue_url = os.getenv(
    "SQS_QUEUE_URL",
    "https://sqs.ap-south-1.amazonaws.com/ACCOUNT_ID_PLACEHOLDER/csvforge-jobs-dev",
)
send_job_message(queue_url=queue_url, job_id=job_id, s3_key=s3_key)
```

**The VisibilityTimeout safety mechanism:**
When a Lambda receives a message, SQS hides that message for 1800 seconds (the VisibilityTimeout). During that window, no other Lambda can pick up the same message. This prevents two workers processing the same job simultaneously. The worker's `claim_job_for_processing()` conditional UPDATE is the application-level guard; the VisibilityTimeout is the infrastructure-level guard. They work together.

## Deploy

```bash
aws cloudformation deploy \
  --template-file infrastructure/queues.yaml \
  --stack-name csvforge-queues-dev \
  --region ap-south-1
```

## Verify

```bash
QUEUE_URL=$(aws sqs get-queue-url \
  --queue-name csvforge-jobs-dev \
  --region ap-south-1 \
  --query QueueUrl --output text)

aws sqs get-queue-attributes \
  --queue-url "$QUEUE_URL" \
  --attribute-names VisibilityTimeout RedrivePolicy MessageRetentionPeriod \
  --output table \
  --region ap-south-1
```

Expected: `VisibilityTimeout=1800`, `RedrivePolicy` pointing to the DLQ ARN, `MessageRetentionPeriod=345600`.

**Predict before running:** What is 345600 seconds in days? Why is that the right retention period for a dev queue?

---

# STEP 5 — RDS PostgreSQL instance

**This is the most expensive resource: ~$12/month. Stop the instance when not in use.**

## Create a DB subnet group (required by RDS)

RDS must know which subnets it can use. AWS requires at least 2 AZs in a subnet group.

```bash
# Get subnet IDs from the network stack
SUBNET_A=$(aws cloudformation describe-stacks \
  --stack-name csvforge-network-dev \
  --query "Stacks[0].Outputs[?OutputKey=='PrivateSubnetAId'].OutputValue" \
  --output text --region ap-south-1)

SUBNET_B=$(aws cloudformation describe-stacks \
  --stack-name csvforge-network-dev \
  --query "Stacks[0].Outputs[?OutputKey=='PrivateSubnetBId'].OutputValue" \
  --output text --region ap-south-1)

aws rds create-db-subnet-group \
  --db-subnet-group-name csvforge-subnet-group-dev \
  --db-subnet-group-description "CSVForge dev DB subnet group" \
  --subnet-ids "$SUBNET_A" "$SUBNET_B" \
  --region ap-south-1
```

## Create the RDS instance

```bash
RDS_SG=$(aws cloudformation describe-stacks \
  --stack-name csvforge-network-dev \
  --query "Stacks[0].Outputs[?OutputKey=='RDSSecurityGroupId'].OutputValue" \
  --output text --region ap-south-1)

# Read the password from Secrets Manager (don't hardcode it here)
DB_PASS=$(aws secretsmanager get-secret-value \
  --secret-id csvforge-db-credentials-dev \
  --query SecretString --output text | \
  python3 -c "import sys,json; print(json.load(sys.stdin)['password'])")

aws rds create-db-instance \
  --db-instance-identifier csvforge-db-dev \
  --db-instance-class db.t3.micro \
  --engine postgres \
  --engine-version 16.6 \
  --master-username csvforge \
  --master-user-password "$DB_PASS" \
  --db-name csvforge \
  --allocated-storage 20 \
  --storage-type gp2 \
  --no-publicly-accessible \
  --db-subnet-group-name csvforge-subnet-group-dev \
  --vpc-security-group-ids "$RDS_SG" \
  --backup-retention-period 1 \
  --no-multi-az \
  --deletion-protection \
  --region ap-south-1
```

`--deletion-protection` prevents accidental deletion. You must disable it before deleting:
```bash
aws rds modify-db-instance \
  --db-instance-identifier csvforge-db-dev \
  --no-deletion-protection \
  --apply-immediately \
  --region ap-south-1
```

Wait for creation (~8 minutes):
```bash
aws rds wait db-instance-available \
  --db-instance-identifier csvforge-db-dev \
  --region ap-south-1
```

## Record the endpoint

```bash
RDS_ENDPOINT=$(aws rds describe-db-instances \
  --db-instance-identifier csvforge-db-dev \
  --query "DBInstances[0].Endpoint.Address" \
  --output text --region ap-south-1)
echo "RDS endpoint: $RDS_ENDPOINT"
```

Save this. You need it for Steps 6 and 7.

## Store in SSM (for the migration buildspec)

```bash
SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id csvforge-db-credentials-dev \
  --query ARN --output text)

aws ssm put-parameter \
  --name /csvforge/dev/rds-endpoint \
  --value "$RDS_ENDPOINT" \
  --type String \
  --region ap-south-1

aws ssm put-parameter \
  --name /csvforge/dev/db-secret-arn \
  --value "$SECRET_ARN" \
  --type String \
  --region ap-south-1
```

## The database schema connection

The database is empty at this point — no tables, no data. The `jobs` table doesn't exist yet. That's created in Step 9 (Alembic migration). Creating the database before the migration is correct — the migration tool creates the table structure.

**Predict:** if you started the Lambda API right now and called `POST /jobs`, what would happen? At which exact line of code would it fail and what exception type would it raise?

(Answer: `db.execute(stmt)` in `create_job()` inside `job_repository.py` would raise `sqlalchemy.exc.ProgrammingError: relation "jobs" does not exist` — PostgreSQL's error for referencing a non-existent table.)

---

# STEP 6 — API stack: Cognito, API Lambda, API Gateway

**Prerequisites:** Steps 1–5 complete. You need:
- `RDS_ENDPOINT` (from Step 5)
- `SECRET_ARN` (from Step 1)
- An S3 bucket for the Lambda ZIP artifact

## Create the Lambda ZIP deployment bucket

This is different from the CSV files bucket. It holds the packaged Lambda code.

```bash
aws s3 mb s3://csvforge-deploy-artifacts-dev --region ap-south-1
```

## Package the API Lambda ZIP

```bash
cd /Users/vishal/csv-metadata

# Create clean package directory
rm -rf /tmp/csvforge-api-package
mkdir -p /tmp/csvforge-api-package

# Install production dependencies only (NOT requirements-dev.txt)
pip install -r backend/requirements.txt -t /tmp/csvforge-api-package/ --quiet

# Copy application code
cp -r backend /tmp/csvforge-api-package/

# Remove unnecessary files
find /tmp/csvforge-api-package -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find /tmp/csvforge-api-package -name "*.dist-info" -type d -exec rm -rf {} + 2>/dev/null || true

# Create ZIP (from inside the package dir so paths are relative)
cd /tmp/csvforge-api-package
zip -r9 /tmp/csvforge-api.zip . --quiet
echo "ZIP size: $(du -sh /tmp/csvforge-api.zip | cut -f1)"
cd -

# Upload to S3
aws s3 cp /tmp/csvforge-api.zip \
  s3://csvforge-deploy-artifacts-dev/api/csvforge-api.zip \
  --region ap-south-1
```

## Verify the handler is in the ZIP

```bash
# The handler must be at backend/main.py and import from mangum
unzip -l /tmp/csvforge-api.zip | grep "backend/main.py"
unzip -p /tmp/csvforge-api.zip backend/main.py | grep "Mangum"
```

Both should return results. If `grep "Mangum"` returns nothing, the Lab 1 fix wasn't included — recheck `backend/main.py`.

## Deploy the API stack

```bash
RDS_ENDPOINT=$(aws ssm get-parameter \
  --name /csvforge/dev/rds-endpoint \
  --query Parameter.Value --output text)
SECRET_ARN=$(aws ssm get-parameter \
  --name /csvforge/dev/db-secret-arn \
  --query Parameter.Value --output text)

aws cloudformation deploy \
  --template-file infrastructure/api.yaml \
  --stack-name csvforge-api-dev \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    LambdaZipS3Bucket=csvforge-deploy-artifacts-dev \
    LambdaZipS3Key=api/csvforge-api.zip \
    RDSEndpoint="$RDS_ENDPOINT" \
    DBSecretArn="$SECRET_ARN" \
  --region ap-south-1
```

This takes 2–4 minutes.

## What gets created

- **Cognito User Pool** `csvforge-users-dev` — the identity store. Users sign up here; Cognito issues JWTs.
- **Cognito App Client** `csvforge-web-dev` — the client application that can authenticate users.
- **Cognito Admin Group** `csvforge-admins` — users in this group can call `GET /admin/jobs`.
- **API Lambda** `csvforge-api-dev` — your FastAPI application, packaged as a ZIP.
- **API Gateway** `csvforge-api-dev` — HTTP API that routes browser requests to the Lambda.
- **JWT Authoriser** — validates Cognito access tokens before Lambda runs.

## The JWT authoriser and the `get_current_user()` connection

`api.yaml` creates:
```yaml
APIAuthoriser:
  Type: AWS::ApiGatewayV2::Authorizer
  Properties:
    AuthorizerType: JWT
    JwtConfiguration:
      Issuer: !Sub "https://cognito-idp.${AWS::Region}.amazonaws.com/${CognitoUserPool}"
      Audience:
        - !Ref CognitoAppClient
```

When a request arrives with `Authorization: Bearer <token>`, API Gateway:
1. Downloads Cognito's JWKS (public keys) from the Issuer URL
2. Verifies the token signature
3. Verifies `exp`, `iss`, and `aud` claims
4. On success: injects decoded claims into `event.requestContext.authorizer.jwt.claims`

Your `backend/auth.py` reads exactly that path:
```python
aws_event = request.scope.get("aws.event", {})
claims = (
    aws_event
    .get("requestContext", {})
    .get("authorizer", {})
    .get("jwt", {})
    .get("claims", {})
)
user_id = claims.get("sub", "")   # ← Cognito's immutable user UUID
```

FastAPI never validates the JWT. API Gateway already did. FastAPI just reads the result.

## Verify

```bash
API_URL=$(aws cloudformation describe-stacks \
  --stack-name csvforge-api-dev \
  --query "Stacks[0].Outputs[?OutputKey=='APIEndpoint'].OutputValue" \
  --output text --region ap-south-1)

echo "API URL: $API_URL"
curl -s "$API_URL/health" | python3 -m json.tool
```

Expected: `{"status": "ok", "environment": "production"}`

**If you get 502:** Lambda failed to initialize. Check CloudWatch logs:
```bash
aws logs tail /aws/lambda/csvforge-api-dev --since 5m --region ap-south-1
```

## Store outputs in SSM

```bash
POOL_ID=$(aws cloudformation describe-stacks --stack-name csvforge-api-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" \
  --output text --region ap-south-1)
CLIENT_ID=$(aws cloudformation describe-stacks --stack-name csvforge-api-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoAppClientId'].OutputValue" \
  --output text --region ap-south-1)

aws ssm put-parameter --name /csvforge/dev/cognito-user-pool-id --value "$POOL_ID"   --type String --region ap-south-1
aws ssm put-parameter --name /csvforge/dev/cognito-app-client-id --value "$CLIENT_ID" --type String --region ap-south-1
aws ssm put-parameter --name /csvforge/dev/api-gateway-url       --value "$API_URL"  --type String --region ap-south-1
```

---

# STEP 7 — Worker stack: ECR, Worker Lambda, SQS event source mapping

**Prerequisites:** Steps 1–5 and Docker running locally.

## Build and push the worker Docker image

```bash
cd /Users/vishal/csv-metadata

# Get your AWS account ID for the ECR URI
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="$ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com/csvforge-worker-dev"

# Deploy worker.yaml first to create the ECR repository
RDS_ENDPOINT=$(aws ssm get-parameter --name /csvforge/dev/rds-endpoint --query Parameter.Value --output text)
SECRET_ARN=$(aws ssm get-parameter --name /csvforge/dev/db-secret-arn --query Parameter.Value --output text)

aws cloudformation deploy \
  --template-file infrastructure/worker.yaml \
  --stack-name csvforge-worker-dev \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    RDSEndpoint="$RDS_ENDPOINT" \
    DBSecretArn="$SECRET_ARN" \
    ImageTag=latest \
  --region ap-south-1
```

```bash
# Build the Docker image (build context = project root, NOT ./worker/)
# This is critical — the Dockerfile COPYs both worker/ AND backend/
docker build -f worker/Dockerfile -t csvforge-worker .
```

**Predict before running:** how long will this build take? What is the largest layer? What happens if you run it again immediately without changing any files?

(Answer: 2–4 minutes on the first run — the base Lambda image is ~500MB and Python packages take time. The `pip install` layer is largest. On a second run without changes, every layer is cached and it completes in seconds.)

```bash
# Authenticate Docker to ECR
aws ecr get-login-password --region ap-south-1 | \
  docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com"

# Tag and push
GIT_SHA=$(git rev-parse --short HEAD)
docker tag csvforge-worker "$ECR_URI:$GIT_SHA"
docker tag csvforge-worker "$ECR_URI:latest"
docker push "$ECR_URI:$GIT_SHA"
docker push "$ECR_URI:latest"

# Get the immutable image digest
IMAGE_DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' "$ECR_URI:$GIT_SHA")
echo "Image digest: $IMAGE_DIGEST"
```

## Update the Lambda to use the image

```bash
aws lambda update-function-code \
  --function-name csvforge-worker-dev \
  --image-uri "$IMAGE_DIGEST" \
  --region ap-south-1

aws lambda wait function-updated \
  --function-name csvforge-worker-dev \
  --region ap-south-1

echo "Worker Lambda updated."
```

## Verify the SQS event source mapping

```bash
aws lambda list-event-source-mappings \
  --function-name csvforge-worker-dev \
  --region ap-south-1 \
  --query "EventSourceMappings[0].{State:State,BatchSize:BatchSize,EventSourceArn:EventSourceArn}" \
  --output table
```

Expected: `State=Enabled`, `BatchSize=1`, `EventSourceArn=arn:aws:sqs:...csvforge-jobs-dev`.

## The event source mapping and `lambda_handler` connection

`worker.yaml` creates:
```yaml
SQSEventSourceMapping:
  Type: AWS::Lambda::EventSourceMapping
  Properties:
    FunctionName: !Ref WorkerLambda
    EventSourceArn:
      Fn::ImportValue: !Sub "${QueuesStackName}-MainQueueArn"
    BatchSize: 1
```

This is the wire between SQS and Lambda. Lambda polls SQS every 0–20 seconds. When a message exists, Lambda receives it as:

```python
event = {
    "Records": [
        {
            "body": "{\"job_id\": \"abc123\", \"s3_key\": \"uploads/...\"}"
        }
    ]
}
```

`worker/handler.py` starts with:
```python
def lambda_handler(event: dict, context: object) -> None:
    for record in event.get("Records", []):
        _process_record(record)
```

```python
def _process_record(record: dict) -> None:
    message = parse_job_message(record["body"])   # ← json.loads the body string
    job_id = message["job_id"]
    s3_key = message["s3_key"]
    ...
```

**BatchSize: 1** means each Lambda invocation processes exactly one SQS message. This means a failure on one job doesn't affect other jobs. If BatchSize were 10 and the 3rd job caused an exception, all 10 messages would be retried.

---

# STEP 8 — Frontend stack: S3 + CloudFront

```bash
aws cloudformation deploy \
  --template-file infrastructure/frontend.yaml \
  --stack-name csvforge-frontend-dev \
  --region ap-south-1
```

```bash
# Get outputs and store in SSM
CF_DIST_ID=$(aws cloudformation describe-stacks --stack-name csvforge-frontend-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDistributionId'].OutputValue" \
  --output text --region ap-south-1)
CF_URL=$(aws cloudformation describe-stacks --stack-name csvforge-frontend-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontURL'].OutputValue" \
  --output text --region ap-south-1)
FRONTEND_BUCKET=$(aws cloudformation describe-stacks --stack-name csvforge-frontend-dev \
  --query "Stacks[0].Outputs[?OutputKey=='FrontendBucketName'].OutputValue" \
  --output text --region ap-south-1)

aws ssm put-parameter --name /csvforge/dev/cloudfront-dist-id    --value "$CF_DIST_ID"       --type String --region ap-south-1
aws ssm put-parameter --name /csvforge/dev/cloudfront-url        --value "$CF_URL"           --type String --region ap-south-1
aws ssm put-parameter --name /csvforge/dev/frontend-bucket-name  --value "$FRONTEND_BUCKET"  --type String --region ap-south-1

echo "CloudFront URL: $CF_URL"
```

## Update the CORS allowed origin on the API Lambda

The API's CORS middleware uses `ALLOWED_ORIGINS`. Now that you have the CloudFront URL, update it:

```bash
# Get the current environment variables
CURRENT_ENV=$(aws lambda get-function-configuration \
  --function-name csvforge-api-dev \
  --query 'Environment.Variables' \
  --output json --region ap-south-1)

# Update ALLOWED_ORIGINS to include the CloudFront URL
aws lambda update-function-configuration \
  --function-name csvforge-api-dev \
  --environment "Variables={
    ENVIRONMENT=production,
    DB_SECRET_ARN=$(aws ssm get-parameter --name /csvforge/dev/db-secret-arn --query Parameter.Value --output text),
    DB_HOST=$(aws ssm get-parameter --name /csvforge/dev/rds-endpoint --query Parameter.Value --output text),
    DB_PORT=5432,
    DB_NAME=csvforge,
    S3_BUCKET_NAME=csvforge-files-dev,
    SQS_QUEUE_URL=$(aws sqs get-queue-url --queue-name csvforge-jobs-dev --region ap-south-1 --query QueueUrl --output text),
    ALLOWED_ORIGINS=$CF_URL
  }" \
  --region ap-south-1

aws lambda wait function-updated --function-name csvforge-api-dev --region ap-south-1
```

Also update the S3 CORS configuration to allow the CloudFront origin:

```bash
aws s3api put-bucket-cors \
  --bucket csvforge-files-dev \
  --cors-configuration "{
    \"CORSRules\": [{
      \"AllowedMethods\": [\"PUT\", \"GET\"],
      \"AllowedOrigins\": [\"http://localhost:5173\", \"$CF_URL\"],
      \"AllowedHeaders\": [\"*\"],
      \"MaxAgeSeconds\": 3000
    }]
  }"
```

---

# STEP 9 — Alembic migration (Lab 12)

**This creates the `jobs` table. Without it, every API call that touches the database fails.**

## Why the migration runs here (not at Lambda startup)

Running `alembic upgrade head` inside `lambda_handler` would be wrong for three reasons:
1. It would run on every Lambda cold start — potentially hundreds of times per day.
2. It would extend cold start time by several seconds.
3. Alembic migrations need the full migration history on disk, not just the latest revision.

Migrations run exactly once per schema change, explicitly, from a controlled environment.

## The migration path

The CodeBuild migration project uses VPC config to reach RDS. For the first migration, you can run it locally if RDS has temporary public access, OR use the CodeBuild project.

### Option A: CodeBuild project (recommended — uses VPC)

```bash
# Trigger the migration job
aws codebuild start-build \
  --project-name csvforge-migrate-dev \
  --region ap-south-1

# Watch the logs
BUILD_ID=$(aws codebuild list-builds-for-project \
  --project-name csvforge-migrate-dev \
  --query "ids[0]" --output text --region ap-south-1)
aws codebuild batch-get-builds --ids "$BUILD_ID" \
  --query "builds[0].{status:buildStatus,phase:currentPhase}" \
  --output table --region ap-south-1
```

### Option B: Temporarily make RDS public (quick for learning)

```bash
# Enable public access temporarily
aws rds modify-db-instance \
  --db-instance-identifier csvforge-db-dev \
  --publicly-accessible \
  --apply-immediately \
  --region ap-south-1

aws rds wait db-instance-available --db-instance-identifier csvforge-db-dev --region ap-south-1

# Get the public endpoint
RDS_PUBLIC=$(aws rds describe-db-instances \
  --db-instance-identifier csvforge-db-dev \
  --query "DBInstances[0].Endpoint.Address" \
  --output text --region ap-south-1)

DB_PASS=$(aws secretsmanager get-secret-value \
  --secret-id csvforge-db-credentials-dev \
  --query SecretString --output text | \
  python3 -c "import sys,json; print(json.load(sys.stdin)['password'])")

# Run the migration
cd /Users/vishal/csv-metadata
ENVIRONMENT=local \
DATABASE_URL="postgresql+psycopg://csvforge:${DB_PASS}@${RDS_PUBLIC}:5432/csvforge" \
PYTHONPATH=. .venv/bin/alembic upgrade head

# IMMEDIATELY restore to non-public
aws rds modify-db-instance \
  --db-instance-identifier csvforge-db-dev \
  --no-publicly-accessible \
  --apply-immediately \
  --region ap-south-1
```

## What the migration does — the code connection

`backend/alembic/versions/0001_initial_jobs_table.py` creates the `jobs` table with all 15 columns and 3 indexes. This is what the ORM model in `backend/models/job.py` maps to.

The `alembic_version` table is created automatically by Alembic to track applied revisions. After the migration:

```sql
SELECT * FROM alembic_version;
-- version_num
-- 0001
```

## Verify the migration ran

```bash
# Connect to the database (while it's temporarily public, or via psql through a bastion)
ENVIRONMENT=local \
DATABASE_URL="postgresql+psycopg://csvforge:${DB_PASS}@${RDS_PUBLIC}:5432/csvforge" \
PYTHONPATH=. .venv/bin/alembic current
# Expected: 0001 (head)

# Test that the table exists by calling the API
curl -s "$API_URL/health"
# Expected: {"status":"ok","environment":"production"}

# Create a test job (requires auth — see Step 10 for test user creation)
```

---

# STEP 10 — Cognito test user

```bash
POOL_ID=$(aws ssm get-parameter --name /csvforge/dev/cognito-user-pool-id --query Parameter.Value --output text)
CLIENT_ID=$(aws ssm get-parameter --name /csvforge/dev/cognito-app-client-id --query Parameter.Value --output text)

# Create a test user
aws cognito-idp admin-create-user \
  --user-pool-id "$POOL_ID" \
  --username test@csvforge.dev \
  --temporary-password 'TempPass1!' \
  --message-action SUPPRESS \
  --region ap-south-1

# Set a permanent password
aws cognito-idp admin-set-user-password \
  --user-pool-id "$POOL_ID" \
  --username test@csvforge.dev \
  --password 'TestUser1!Permanent' \
  --permanent \
  --region ap-south-1

# Get an access token
TOKEN_RESPONSE=$(aws cognito-idp initiate-auth \
  --auth-flow USER_PASSWORD_AUTH \
  --client-id "$CLIENT_ID" \
  --auth-parameters USERNAME=test@csvforge.dev,PASSWORD='TestUser1!Permanent' \
  --region ap-south-1)

ACCESS_TOKEN=$(echo "$TOKEN_RESPONSE" | python3 -c "
import sys, json
r = json.load(sys.stdin)
print(r['AuthenticationResult']['AccessToken'])
")

echo "Access token (first 30 chars): ${ACCESS_TOKEN:0:30}..."
```

## Call an authenticated endpoint

```bash
# Get the job list (should be empty initially)
curl -s "$API_URL/jobs" \
  -H "Authorization: Bearer $ACCESS_TOKEN" | python3 -m json.tool
```

Expected: `{"jobs": [], "total": 0}`

---

# STEP 11 — Build and deploy the frontend

```bash
cd /Users/vishal/csv-metadata/frontend

# Write the production environment file
cat > .env.production << EOF
VITE_API_URL=$API_URL
VITE_COGNITO_USER_POOL_ID=$POOL_ID
VITE_COGNITO_APP_CLIENT_ID=$CLIENT_ID
VITE_AWS_REGION=ap-south-1
EOF

# Build
npm run build

# Upload index.html with no-cache
aws s3 cp dist/index.html "s3://$FRONTEND_BUCKET/index.html" \
  --cache-control "no-cache, no-store, must-revalidate" \
  --content-type "text/html" \
  --region ap-south-1

# Upload all other assets with immutable cache
aws s3 sync dist/ "s3://$FRONTEND_BUCKET/" \
  --exclude "index.html" \
  --cache-control "public, max-age=31536000, immutable" \
  --delete \
  --region ap-south-1

# Invalidate CloudFront
aws cloudfront create-invalidation \
  --distribution-id "$CF_DIST_ID" \
  --paths "/*"

echo "Frontend deployed to: $CF_URL"
```

---

# STEP 12 — End-to-end smoke test

```bash
# Test the full upload → process → download flow

# 1. Create an upload intent
RESPONSE=$(curl -s -X POST "$API_URL/jobs" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"user_email\":\"test@csvforge.dev\",\"original_filename\":\"test.csv\",\"file_size_bytes\":120}")
echo "$RESPONSE" | python3 -m json.tool

JOB_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
UPLOAD_URL=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['upload_url'])")

# 2. Upload a test CSV directly to S3 via presigned URL
cat > /tmp/test.csv << 'EOF'
name,email,score
Alice,alice@example.com,95
Bob,bob@example.com,87
Alice,alice@example.com,95
Charlie,charlie@example.com,91
EOF

curl -s -X PUT "$UPLOAD_URL" \
  -H "Content-Type: text/csv" \
  --data-binary @/tmp/test.csv
echo "Upload complete."

# 3. Confirm the upload (triggers SQS → Lambda worker)
curl -s -X POST "$API_URL/uploads/$JOB_ID/complete" \
  -H "Authorization: Bearer $ACCESS_TOKEN" | python3 -m json.tool

# 4. Poll for completion (worker processes asynchronously)
for i in {1..15}; do
  STATUS=$(curl -s "$API_URL/jobs/$JOB_ID" \
    -H "Authorization: Bearer $ACCESS_TOKEN" | \
    python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "[$i] Status: $STATUS"
  if [[ "$STATUS" == "completed" || "$STATUS" == "failed" ]]; then
    break
  fi
  sleep 5
done

# 5. Get download URL and verify
DOWNLOAD=$(curl -s "$API_URL/jobs/$JOB_ID/download" \
  -H "Authorization: Bearer $ACCESS_TOKEN")
echo "$DOWNLOAD" | python3 -m json.tool

DOWNLOAD_URL=$(echo "$DOWNLOAD" | python3 -c "import sys,json; print(json.load(sys.stdin)['download_url'])")
curl -s "$DOWNLOAD_URL" | head -5
```

**Expected final output:** the CSV without the duplicate Alice row (3 data rows instead of 4), trimmed of whitespace.

---

# STEP 13 — Deploy the pipeline

```bash
# Get the GitHub connection ARN from the bootstrap stack
CONNECTION_ARN=$(aws cloudformation describe-stacks \
  --stack-name csvforge-pipeline-bootstrap-dev \
  --query "Stacks[0].Outputs[?OutputKey=='GitHubConnectionArn'].OutputValue" \
  --output text --region ap-south-1)

aws cloudformation deploy \
  --template-file infrastructure/pipeline.yaml \
  --stack-name csvforge-pipeline-dev \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    GitHubConnectionArn="$CONNECTION_ARN" \
    GitHubOwner=vishal-dubey10 \
    GitHubRepo=csv-metadata \
    GitHubBranch=main \
    ArtifactBucketName=csvforge-pipeline-artifacts-dev \
    NotificationEmail=your@email.com \
  --region ap-south-1
```

**After deploying:** push a trivial commit to `main`. Watch the pipeline execute in the Console (CodePipeline → csvforge-pipeline-dev).

---

# Troubleshooting quick reference

```bash
# Lambda logs (most useful for 502 errors)
aws logs tail /aws/lambda/csvforge-api-dev    --since 10m --region ap-south-1
aws logs tail /aws/lambda/csvforge-worker-dev --since 10m --region ap-south-1

# Check current DB migration state
PYTHONPATH=. .venv/bin/alembic current

# Check SQS queue depth
aws sqs get-queue-attributes \
  --queue-url $(aws sqs get-queue-url --queue-name csvforge-jobs-dev --query QueueUrl --output text) \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
  --region ap-south-1

# Check DLQ (failed messages land here)
aws sqs get-queue-attributes \
  --queue-url $(aws sqs get-queue-url --queue-name csvforge-jobs-dev-dlq --query QueueUrl --output text) \
  --attribute-names ApproximateNumberOfMessages \
  --region ap-south-1

# Check pipeline status
aws codepipeline get-pipeline-state \
  --name csvforge-pipeline-dev \
  --query "stageStates[*].{stage:stageName,status:latestExecution.status}" \
  --output table --region ap-south-1
```

---

# Cost management

```bash
# Stop RDS when not in use (~$12/month → $0 while stopped)
aws rds stop-db-instance --db-instance-identifier csvforge-db-dev --region ap-south-1

# Start again when needed
aws rds start-db-instance --db-instance-identifier csvforge-db-dev --region ap-south-1
aws rds wait db-instance-available --db-instance-identifier csvforge-db-dev --region ap-south-1

# VPC endpoints cost ~$28/month even while idle
# Delete them when you're done learning for the day:
# (Find endpoint IDs in the Console → VPC → Endpoints)

# Full cleanup (WARNING: destroys all data)
# aws cloudformation delete-stack --stack-name csvforge-pipeline-dev
# aws cloudformation delete-stack --stack-name csvforge-frontend-dev
# ... (see AWS_DEPLOYMENT_RUNBOOK.md for full cleanup sequence)
```
