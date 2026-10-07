#!/bin/bash
# =============================================================================
# create_dev_resources.sh — Provision CSVForge dev AWS resources
# =============================================================================
# This script creates the foundational AWS resources needed for CSVForge
# development: S3 bucket, SQS queues (main + DLQ), and the Lambda IAM role.
#
# All resources follow the naming convention: {service}-{environment}
# (e.g., csvforge-jobs-dev, csvforge-jobs-dlq-dev)
#
# The script is IDEMPOTENT: running it twice does not duplicate resources.
# Each section checks for existing resources before creating them.
#
# Usage:
#   ./infrastructure/create_dev_resources.sh
#
# Prerequisites:
#   - AWS CLI v2 installed
#   - Valid AWS credentials (aws configure, AWS_PROFILE, or instance role)
#   - Permissions: s3:CreateBucket, sqs:CreateQueue, iam:CreateRole, iam:PutRolePolicy
# =============================================================================
set -euo pipefail

# Locate the project root (one level up from the infrastructure/ directory)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# =============================================================================
# SECTION 1: Verify AWS credentials
# =============================================================================
# aws sts get-caller-identity returns the ARN of the currently-authenticated
# IAM principal. If credentials are missing or expired, this command fails
# with an explicit error rather than silently running with no permissions.
echo "==> Verifying AWS credentials..."
if ! aws sts get-caller-identity --output table 2>/dev/null; then
  echo ""
  echo "ERROR: AWS credentials are not configured or have expired."
  echo ""
  echo "To fix this, run one of the following:"
  echo "  aws configure                        # Interactive credential setup"
  echo "  export AWS_PROFILE=your-profile-name # Use a named profile"
  echo "  export AWS_ACCESS_KEY_ID=...         # Set credentials as env vars"
  echo "  export AWS_SECRET_ACCESS_KEY=..."
  echo ""
  echo "NOTE: AWS credentials are required. Run: aws configure (or set AWS_PROFILE)"
  exit 1
fi
echo "Credentials verified."
echo ""

# =============================================================================
# SECTION 2: Configuration variables
# =============================================================================
# All resource names follow the {service}-{environment} convention (AD-D2-06).
# Changing REGION here affects all resources — keep all resources in one region
# to avoid cross-region latency and complexity (AD-D2-01).
REGION="ap-south-1"
BUCKET_NAME="csvforge-files-dev"
QUEUE_NAME="csvforge-jobs-dev"
DLQ_NAME="csvforge-jobs-dlq-dev"
LAMBDA_ROLE_NAME="csvforge-worker-role-dev"

echo "==> Configuration:"
echo "    Region:      ${REGION}"
echo "    Bucket:      ${BUCKET_NAME}"
echo "    Queue:       ${QUEUE_NAME}"
echo "    DLQ:         ${DLQ_NAME}"
echo "    Lambda role: ${LAMBDA_ROLE_NAME}"
echo ""

# =============================================================================
# SECTION 3: S3 Bucket
# =============================================================================
# S3 is the object store for raw CSV uploads (uploads/) and processed output
# (processed/). A single bucket with prefix-level IAM separation is used for
# dev simplicity (AD-D2-09).
#
# Idempotency: We check if the bucket exists before creating it. The error
# "BucketAlreadyOwnedByYou" is benign — the bucket is already ours.
echo "==> Creating S3 bucket: ${BUCKET_NAME}..."
if aws s3api head-bucket --bucket "${BUCKET_NAME}" --region "${REGION}" 2>/dev/null; then
  echo "    Bucket already exists — skipping creation."
else
  # Note: ap-south-1 (and any non-us-east-1 region) requires the
  # LocationConstraint parameter. us-east-1 must omit it.
  aws s3api create-bucket \
    --bucket "${BUCKET_NAME}" \
    --region "${REGION}" \
    --create-bucket-configuration LocationConstraint="${REGION}"
  echo "    Bucket created."
fi

# Block all public access — CSVForge files are private by default.
# All client access goes through presigned URLs (not bucket policy or public ACLs).
# This is a security baseline: even if a misconfigured bucket policy is applied
# later, public access is blocked at the account level for this bucket.
echo "    Applying public access block..."
aws s3api put-public-access-block \
  --bucket "${BUCKET_NAME}" \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# CORS (Cross-Origin Resource Sharing) allows the browser to PUT directly to S3.
# Without CORS, the browser blocks the presigned PUT request because the S3 domain
# differs from the API domain (same-origin policy).
# AllowedMethods: PUT for upload, GET for direct download of presigned URLs.
# MaxAgeSeconds: browser caches the CORS preflight response for 50 minutes.
echo "    Applying CORS configuration..."
aws s3api put-bucket-cors \
  --bucket "${BUCKET_NAME}" \
  --cors-configuration '{
    "CORSRules": [{
      "AllowedMethods": ["PUT", "GET"],
      "AllowedOrigins": ["*"],
      "AllowedHeaders": ["*"],
      "MaxAgeSeconds": 3000
    }]
  }'

echo "    S3 bucket ready: s3://${BUCKET_NAME}"
echo ""

# =============================================================================
# SECTION 4: SQS Dead Letter Queue (DLQ)
# =============================================================================
# The DLQ is created BEFORE the main queue because the main queue's redrive
# policy references the DLQ ARN — so the DLQ must exist first.
#
# Dead Letter Queue purpose: after MaxReceiveCount=5 failed attempts, SQS
# automatically moves the message to the DLQ. Dead letters are auditable proof
# that work was attempted. Without a DLQ, messages disappear after max retries
# with no trace (AD-D2-05, AD-17).
#
# MessageRetentionPeriod=1209600 = 14 days (the maximum). Gives ops team time
# to inspect and replay failed messages before they expire.
echo "==> Creating Dead Letter Queue: ${DLQ_NAME}..."
DLQ_URL=$(aws sqs get-queue-url \
  --queue-name "${DLQ_NAME}" \
  --region "${REGION}" \
  --query 'QueueUrl' \
  --output text 2>/dev/null || echo "")

if [[ -n "${DLQ_URL}" ]]; then
  echo "    DLQ already exists — skipping creation."
else
  DLQ_URL=$(aws sqs create-queue \
    --queue-name "${DLQ_NAME}" \
    --region "${REGION}" \
    --attributes MessageRetentionPeriod=1209600 \
    --query 'QueueUrl' \
    --output text)
  echo "    DLQ created."
fi

# Get the DLQ ARN — needed for the main queue's redrive policy.
# The ARN is not returned by create-queue; we must query it separately.
DLQ_ARN=$(aws sqs get-queue-attributes \
  --queue-url "${DLQ_URL}" \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' \
  --output text)

echo "    DLQ URL: ${DLQ_URL}"
echo "    DLQ ARN: ${DLQ_ARN}"
echo ""

# =============================================================================
# SECTION 5: Main SQS Queue
# =============================================================================
# The main queue holds job tickets. Lambda polls this queue via an event source
# mapping (configured separately in deploy_lambda.sh).
#
# Key settings:
# - VisibilityTimeout=1800: AWS recommends 6× Lambda timeout (6 × 300s = 1800s).
#   While a worker holds a message, it is invisible to all other consumers.
#   This prevents two workers from processing the same job simultaneously (AD-17).
# - RedrivePolicy: after MaxReceiveCount=5 failures, move message to DLQ (AD-D2-05).
echo "==> Creating main SQS queue: ${QUEUE_NAME}..."
QUEUE_URL=$(aws sqs get-queue-url \
  --queue-name "${QUEUE_NAME}" \
  --region "${REGION}" \
  --query 'QueueUrl' \
  --output text 2>/dev/null || echo "")

if [[ -n "${QUEUE_URL}" ]]; then
  echo "    Queue already exists — skipping creation."
else
  # The RedrivePolicy JSON must be escaped as a string within the --attributes JSON.
  REDRIVE_POLICY=$(printf '{"maxReceiveCount":"5","deadLetterTargetArn":"%s"}' "${DLQ_ARN}")

  QUEUE_URL=$(aws sqs create-queue \
    --queue-name "${QUEUE_NAME}" \
    --region "${REGION}" \
    --attributes \
      VisibilityTimeout=1800 \
      "RedrivePolicy=${REDRIVE_POLICY}" \
    --query 'QueueUrl' \
    --output text)
  echo "    Queue created."
fi

QUEUE_ARN=$(aws sqs get-queue-attributes \
  --queue-url "${QUEUE_URL}" \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' \
  --output text)

echo "    Queue URL: ${QUEUE_URL}"
echo "    Queue ARN: ${QUEUE_ARN}"
echo ""

# =============================================================================
# SECTION 6: Lambda IAM Role
# =============================================================================
# The Lambda execution role defines:
# 1. Trust policy (lambda-trust-policy.json): who can ASSUME this role.
#    Lambda service principal (lambda.amazonaws.com) must be trusted.
# 2. Inline permission policy (lambda-execution-policy.json): what the role can DO.
#    Least privilege: only the exact S3, SQS, and CloudWatch permissions required (AD-20).
#
# Idempotency: we check if the role exists before creating. If it exists, we
# still re-apply the inline policy to pick up any changes.
IAM_DIR="${SCRIPT_DIR}/iam"

echo "==> Creating Lambda IAM role: ${LAMBDA_ROLE_NAME}..."
if aws iam get-role --role-name "${LAMBDA_ROLE_NAME}" 2>/dev/null; then
  echo "    Role already exists — skipping creation."
else
  aws iam create-role \
    --role-name "${LAMBDA_ROLE_NAME}" \
    --assume-role-policy-document "file://${IAM_DIR}/lambda-trust-policy.json" \
    --description "CSVForge Lambda worker execution role (dev)"
  echo "    Role created."
fi

# Attach the execution policy as an inline policy.
# Inline policies are embedded directly in the role (not managed separately).
# This keeps the policy and role lifecycle together — deleting the role also
# deletes the policy.
echo "    Attaching inline execution policy..."
aws iam put-role-policy \
  --role-name "${LAMBDA_ROLE_NAME}" \
  --policy-name "csvforge-worker-execution-dev" \
  --policy-document "file://${IAM_DIR}/lambda-execution-policy.json"
echo "    Policy attached."

ROLE_ARN=$(aws iam get-role \
  --role-name "${LAMBDA_ROLE_NAME}" \
  --query 'Role.Arn' \
  --output text)

echo "    Role ARN: ${ROLE_ARN}"
echo ""

# =============================================================================
# SECTION 7: Summary
# =============================================================================
# Print all resource identifiers so you can verify what was created.
# No secrets, no credentials appear in this output.
echo "============================================================"
echo "  CSVForge Dev Resources — Provisioning Complete"
echo "============================================================"
echo ""
echo "  S3 Bucket:"
echo "    Name:  ${BUCKET_NAME}"
echo "    URI:   s3://${BUCKET_NAME}"
echo ""
echo "  SQS Dead Letter Queue:"
echo "    Name:  ${DLQ_NAME}"
echo "    URL:   ${DLQ_URL}"
echo "    ARN:   ${DLQ_ARN}"
echo ""
echo "  SQS Main Queue:"
echo "    Name:  ${QUEUE_NAME}"
echo "    URL:   ${QUEUE_URL}"
echo "    ARN:   ${QUEUE_ARN}"
echo ""
echo "  Lambda IAM Role:"
echo "    Name:  ${LAMBDA_ROLE_NAME}"
echo "    ARN:   ${ROLE_ARN}"
echo ""
echo "  Next step: run ./infrastructure/deploy_lambda.sh to deploy the worker."
echo ""
echo "NOTE: AWS credentials are required. Run: aws configure (or set AWS_PROFILE)"
