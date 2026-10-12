# CSVForge AWS Deployment Runbook

Step-by-step guide to deploying CSVForge to AWS ap-south-1.
**Do not skip the verification step after each stage.**

---

## Safety rules before you start

- Always deploy to dev stacks first (`*-dev` suffix).
- Read the CloudFormation change set before executing it.
- Never `--force-delete` a stack that contains data (S3, RDS).
- All AWS commands in this document assume `--region ap-south-1`.
- Costs are noted at each stage. Stop and clean up if costs are unexpected.

---

## Prerequisites

```bash
# Confirm AWS CLI is configured and you have the right account
aws sts get-caller-identity

# Confirm you're in the right region
aws configure get region   # should be ap-south-1

# Confirm Docker is running (needed for ECR push)
docker ps
```

---

## Deployment order

The stacks must be deployed in this order because each stack imports outputs
from the one before it. CloudFormation will fail with `Export not found` if
you deploy out of order.

```
1. csvforge-network-dev    (no dependencies)
2. csvforge-data-dev       (no dependencies)
3. csvforge-queues-dev     (no dependencies)
4. RDS instance            (manual — not in a stack yet)
5. csvforge-api-dev        (imports from network + data + queues; needs RDS endpoint)
6. csvforge-worker-dev     (imports from network + data + queues; needs RDS endpoint)
7. Alembic migration       (run against RDS after VPC connectivity is confirmed)
8. Worker Docker image     (push to ECR after csvforge-worker-dev creates the repo)
9. API Lambda zip          (package and upload to S3, then update the api stack)
10. Cognito test user      (create via CLI after Cognito User Pool is deployed)
11. Frontend build         (inject Cognito values, build, upload to S3 bucket)
```

---

## Stage 1 — Network stack

**What it creates:** VPC, 2 private subnets, 3 security groups, S3 gateway endpoint,
SQS interface endpoint, Secrets Manager interface endpoint.

**Cost:** VPC endpoints (SQS + Secrets Manager) ≈ $0.02/hr ($14/month). No NAT gateway.

```bash
# Preview — validate the template first
aws cloudformation validate-template \
  --template-body file://infrastructure/network.yaml

# Deploy
aws cloudformation deploy \
  --template-file infrastructure/network.yaml \
  --stack-name csvforge-network-dev \
  --region ap-south-1

# Verify
aws cloudformation describe-stacks \
  --stack-name csvforge-network-dev \
  --query "Stacks[0].Outputs" \
  --output table
```

Expected outputs: `VpcId`, `PrivateSubnetAId`, `PrivateSubnetBId`,
`LambdaSecurityGroupId`, `RDSSecurityGroupId`.

---

## Stage 2 — Data stack (S3 bucket)

**What it creates:** S3 bucket with encryption, CORS for browser uploads, and public access block.

**Cost:** S3 storage is fractions of a cent for development volumes.

```bash
aws cloudformation deploy \
  --template-file infrastructure/data.yaml \
  --stack-name csvforge-data-dev \
  --region ap-south-1

# Verify the bucket exists
aws s3 ls | grep csvforge-files-dev

# Verify CORS is configured
aws s3api get-bucket-cors --bucket csvforge-files-dev
```

---

## Stage 3 — Queues stack (SQS)

**What it creates:** SQS main queue (`csvforge-jobs-dev`) and DLQ (`csvforge-jobs-dev-dlq`).

**Cost:** SQS Standard: first 1 million requests/month free. Negligible for dev.

```bash
aws cloudformation deploy \
  --template-file infrastructure/queues.yaml \
  --stack-name csvforge-queues-dev \
  --region ap-south-1

# Verify both queues exist
aws sqs list-queues --queue-name-prefix csvforge

# Verify DLQ redrive policy
aws sqs get-queue-attributes \
  --queue-url $(aws sqs get-queue-url --queue-name csvforge-jobs-dev --query QueueUrl --output text) \
  --attribute-names RedrivePolicy VisibilityTimeout \
  --output table
```

---

## Stage 4 — RDS PostgreSQL (manual — not yet in CloudFormation)

**What it creates:** RDS db.t3.micro PostgreSQL 16 in the private subnets.

**Cost:** db.t3.micro = $0.017/hr ≈ **$12/month**. This is the largest dev cost.
**Stop or delete the instance when not in use.**

```bash
# Get the subnet IDs from the network stack outputs
SUBNET_A=$(aws cloudformation describe-stacks \
  --stack-name csvforge-network-dev \
  --query "Stacks[0].Outputs[?OutputKey=='PrivateSubnetAId'].OutputValue" \
  --output text)
SUBNET_B=$(aws cloudformation describe-stacks \
  --stack-name csvforge-network-dev \
  --query "Stacks[0].Outputs[?OutputKey=='PrivateSubnetBId'].OutputValue" \
  --output text)

# Get the RDS security group ID
RDS_SG=$(aws cloudformation describe-stacks \
  --stack-name csvforge-network-dev \
  --query "Stacks[0].Outputs[?OutputKey=='RDSSecurityGroupId'].OutputValue" \
  --output text)

# Create a DB subnet group (required by RDS)
aws rds create-db-subnet-group \
  --db-subnet-group-name csvforge-subnet-group-dev \
  --db-subnet-group-description "CSVForge dev RDS subnet group" \
  --subnet-ids $SUBNET_A $SUBNET_B

# Create a Secrets Manager secret for DB credentials
# Replace <password> with a strong password
aws secretsmanager create-secret \
  --name csvforge-db-credentials-dev \
  --secret-string '{"username":"csvforge","password":"<password>"}' \
  --region ap-south-1

# Record the secret ARN
DB_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id csvforge-db-credentials-dev \
  --query ARN --output text)

# Create the RDS instance
aws rds create-db-instance \
  --db-instance-identifier csvforge-db-dev \
  --db-instance-class db.t3.micro \
  --engine postgres \
  --engine-version 16.6 \
  --master-username csvforge \
  --master-user-password '<password>' \
  --db-name csvforge \
  --allocated-storage 20 \
  --storage-type gp2 \
  --no-publicly-accessible \
  --db-subnet-group-name csvforge-subnet-group-dev \
  --vpc-security-group-ids $RDS_SG \
  --backup-retention-period 1 \
  --no-multi-az \
  --region ap-south-1

# Wait for the instance to be available (takes 5-10 minutes)
aws rds wait db-instance-available --db-instance-identifier csvforge-db-dev

# Get the endpoint
aws rds describe-db-instances \
  --db-instance-identifier csvforge-db-dev \
  --query "DBInstances[0].Endpoint.Address" \
  --output text
```

Record the RDS endpoint. You need it for the next two stages.

---

## Stage 5 — API stack (Cognito + API Lambda + API Gateway)

**Prerequisites:** Stages 1–4 complete. You need:
- `DB_SECRET_ARN` (from Secrets Manager)
- `RDS_ENDPOINT` (from Stage 4)
- An S3 bucket for the Lambda ZIP artifact (create manually):
  `aws s3 mb s3://csvforge-deploy-artifacts-dev --region ap-south-1`

**Package the API Lambda:**
```bash
# From project root
cd /Users/vishal/csv-metadata

# Install backend dependencies into a package directory
pip install -r backend/requirements.txt -t package/

# Copy application code into the package
cp -r backend package/
cp -r worker package/   # worker modules are imported by the Lambda handler

# Create the ZIP
cd package && zip -r9 ../csvforge-api.zip . && cd ..

# Upload to S3
aws s3 cp csvforge-api.zip s3://csvforge-deploy-artifacts-dev/api/csvforge-api.zip

# Deploy the stack
aws cloudformation deploy \
  --template-file infrastructure/api.yaml \
  --stack-name csvforge-api-dev \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    LambdaZipS3Bucket=csvforge-deploy-artifacts-dev \
    LambdaZipS3Key=api/csvforge-api.zip \
    RDSEndpoint=<rds-endpoint> \
    DBSecretArn=<db-secret-arn> \
  --region ap-south-1
```

**Verify:**
```bash
# Get the API endpoint
API_URL=$(aws cloudformation describe-stacks \
  --stack-name csvforge-api-dev \
  --query "Stacks[0].Outputs[?OutputKey=='APIEndpoint'].OutputValue" \
  --output text)

echo "API URL: $API_URL"

# Test the health endpoint (no auth required)
curl "$API_URL/health"
# Expected: {"status":"ok","environment":"production"}
```

---

## Stage 6 — Worker stack (ECR + Worker Lambda)

**Prerequisites:** Stages 1–4 complete.

```bash
# Deploy the stack (creates ECR repository and Worker Lambda shell)
aws cloudformation deploy \
  --template-file infrastructure/worker.yaml \
  --stack-name csvforge-worker-dev \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    RDSEndpoint=<rds-endpoint> \
    DBSecretArn=<db-secret-arn> \
    ImageTag=latest \
  --region ap-south-1

# Get the ECR repository URI
ECR_URI=$(aws cloudformation describe-stacks \
  --stack-name csvforge-worker-dev \
  --query "Stacks[0].Outputs[?OutputKey=='ECRRepositoryUri'].OutputValue" \
  --output text)

# Build and push the worker Docker image
# Build from project root (not worker/) — Dockerfile copies backend/ modules
docker build -f worker/Dockerfile -t csvforge-worker .

# Authenticate Docker to ECR
aws ecr get-login-password --region ap-south-1 | \
  docker login --username AWS --password-stdin $(echo $ECR_URI | cut -d/ -f1)

# Tag and push
docker tag csvforge-worker "$ECR_URI:latest"
docker push "$ECR_URI:latest"

# Update Lambda to use the new image
aws lambda update-function-code \
  --function-name csvforge-worker-dev \
  --image-uri "$ECR_URI:latest" \
  --region ap-south-1
```

---

## Stage 7 — Alembic migration

The migration must run against the real RDS instance. Since RDS is in a private subnet,
you cannot run `alembic upgrade head` from your laptop directly.

**Option A: Use an EC2 bastion (recommended for learning)**
```bash
# Launch a small EC2 in the same VPC/subnet as RDS
# Then SSH in and run the migration from there
```

**Option B: Run as a one-off Lambda invocation**
```bash
# Package a migration Lambda or use the API Lambda to run the migration
# (out of scope for Day 1 of AWS labs — revisit in the RDS lab)
```

**Option C: Temporarily make RDS publicly accessible for migration only**
```bash
# (High risk — only acceptable for a dev instance, not production)
aws rds modify-db-instance \
  --db-instance-identifier csvforge-db-dev \
  --publicly-accessible \
  --apply-immediately

# Wait, get the endpoint, run migration, then set back to non-public
PYTHONPATH=. DATABASE_URL="postgresql+psycopg://csvforge:<password>@<endpoint>/csvforge" \
  .venv/bin/alembic upgrade head

# Immediately restore
aws rds modify-db-instance \
  --db-instance-identifier csvforge-db-dev \
  --no-publicly-accessible \
  --apply-immediately
```

---

## Stage 8 — Cognito test user

```bash
# Get the User Pool ID from the api stack
POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name csvforge-api-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" \
  --output text)

CLIENT_ID=$(aws cloudformation describe-stacks \
  --stack-name csvforge-api-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoAppClientId'].OutputValue" \
  --output text)

# Create a test user
aws cognito-idp admin-create-user \
  --user-pool-id $POOL_ID \
  --username test@csvforge.dev \
  --temporary-password 'TempPass1!' \
  --message-action SUPPRESS \
  --region ap-south-1

# Set a permanent password (so you can log in immediately)
aws cognito-idp admin-set-user-password \
  --user-pool-id $POOL_ID \
  --username test@csvforge.dev \
  --password 'PermanentPass1!' \
  --permanent \
  --region ap-south-1

# Get an access token to test the API
aws cognito-idp initiate-auth \
  --auth-flow USER_PASSWORD_AUTH \
  --client-id $CLIENT_ID \
  --auth-parameters USERNAME=test@csvforge.dev,PASSWORD='PermanentPass1!' \
  --region ap-south-1
```

---

## Stage 9 — End-to-end smoke test

```bash
API_URL=<your-api-gateway-endpoint>
ACCESS_TOKEN=<token-from-stage-8>

# 1. Create an upload job
RESPONSE=$(curl -s -X POST "$API_URL/jobs" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"user_email":"test@csvforge.dev","original_filename":"test.csv","file_size_bytes":100}')
echo $RESPONSE | python3 -m json.tool

JOB_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
UPLOAD_URL=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['upload_url'])")

# 2. Upload a test CSV directly to S3
echo "name,email,city
Alice,alice@example.com,Mumbai
Bob,bob@example.com,Delhi
Alice,alice@example.com,Mumbai" > /tmp/test.csv

curl -s -X PUT "$UPLOAD_URL" \
  -H "Content-Type: text/csv" \
  --data-binary @/tmp/test.csv

# 3. Confirm the upload
curl -s -X POST "$API_URL/uploads/$JOB_ID/complete" \
  -H "Authorization: Bearer $ACCESS_TOKEN" | python3 -m json.tool

# 4. Poll for completion
for i in {1..12}; do
  STATUS=$(curl -s "$API_URL/jobs/$JOB_ID" \
    -H "Authorization: Bearer $ACCESS_TOKEN" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "Attempt $i: $STATUS"
  if [[ "$STATUS" == "completed" || "$STATUS" == "failed" ]]; then break; fi
  sleep 5
done

# 5. Get the download URL
curl -s "$API_URL/jobs/$JOB_ID/download" \
  -H "Authorization: Bearer $ACCESS_TOKEN" | python3 -m json.tool
```

---

## Cleanup (stop incurring charges)

```bash
# 1. Delete CloudFormation stacks in reverse order
# WARNING: this deletes all resources including any data

aws cloudformation delete-stack --stack-name csvforge-worker-dev --region ap-south-1
aws cloudformation delete-stack --stack-name csvforge-api-dev --region ap-south-1
aws cloudformation wait stack-delete-complete --stack-name csvforge-worker-dev
aws cloudformation wait stack-delete-complete --stack-name csvforge-api-dev

# 2. Delete RDS (must be done manually — not in a stack)
aws rds delete-db-instance \
  --db-instance-identifier csvforge-db-dev \
  --skip-final-snapshot \
  --region ap-south-1

# 3. Empty the S3 bucket (required before deleting it)
aws s3 rm s3://csvforge-files-dev --recursive

# 4. Delete remaining stacks
aws cloudformation delete-stack --stack-name csvforge-queues-dev --region ap-south-1
aws cloudformation delete-stack --stack-name csvforge-data-dev --region ap-south-1
aws cloudformation delete-stack --stack-name csvforge-network-dev --region ap-south-1

# 5. Delete the ECR repository if you pushed an image
aws ecr delete-repository --repository-name csvforge-worker-dev --force --region ap-south-1

# 6. Delete the Secrets Manager secret
aws secretsmanager delete-secret \
  --secret-id csvforge-db-credentials-dev \
  --force-delete-without-recovery \
  --region ap-south-1
```

---

## Missing infrastructure (deployment gaps)

These are not yet implemented and are required before the frontend works in production:

1. **Frontend S3 bucket** — for static website hosting (`index.html`, built JS/CSS).
   No CloudFormation template exists yet. Create manually or add a `frontend.yaml` stack.

2. **CloudFront distribution** — to serve the frontend over HTTPS. Without it, the S3
   static website is HTTP only, and the browser will refuse to send credentials to it.

3. **`npm run build` with Cognito values** — the Vite build must be run with the real
   `VITE_COGNITO_USER_POOL_ID` and `VITE_COGNITO_CLIENT_ID` values injected. These come
   from the CloudFormation outputs of `csvforge-api-dev`. A build script that reads the
   stack outputs and writes `.env.production` is needed.
