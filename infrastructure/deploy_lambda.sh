#!/bin/bash
# =============================================================================
# deploy_lambda.sh — Build and deploy the CSVForge Lambda worker
# =============================================================================
# This script packages the Lambda worker function (zip deployment, not Docker),
# creates or updates the Lambda function in AWS, and configures the SQS event
# source mapping so Lambda triggers automatically when messages arrive.
#
# Package strategy: zip (not container image) for this dev deployment script.
# Production uses Docker via ECR (AD-05, AD-18). This zip script is for rapid
# local iteration and learning about Lambda packaging mechanics.
#
# Usage:
#   ./infrastructure/deploy_lambda.sh
#
# Prerequisites:
#   - AWS CLI v2 installed
#   - Valid AWS credentials configured
#   - Python 3.11+ available (for pip install)
#   - Run create_dev_resources.sh first (IAM role must exist)
#   - worker/ and backend/ directories present at project root
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# =============================================================================
# SECTION 1: Verify AWS credentials
# =============================================================================
echo "==> Verifying AWS credentials..."
if ! aws sts get-caller-identity --output table 2>/dev/null; then
  echo ""
  echo "ERROR: AWS credentials are not configured or have expired."
  echo "Run: aws configure  (or set AWS_PROFILE)"
  exit 1
fi
echo "Credentials verified."
echo ""

# =============================================================================
# SECTION 2: Configuration
# =============================================================================
# These values must match what create_dev_resources.sh provisioned.
# The HANDLER value is the Python dotted path: module.function
# Lambda resolves "handler.lambda_handler" as: import handler; handler.lambda_handler(event, ctx)
FUNCTION_NAME="csvforge-worker-dev"
RUNTIME="python3.11"
HANDLER="handler.lambda_handler"
MEMORY=256          # MB — sufficient for CSV processing in dev (1024 MB for prod, AD-01)
TIMEOUT=60          # seconds — dev timeout; production uses 300s (AD-17)
REGION="ap-south-1"
ROLE_NAME="csvforge-worker-role-dev"

echo "==> Configuration:"
echo "    Function:  ${FUNCTION_NAME}"
echo "    Runtime:   ${RUNTIME}"
echo "    Handler:   ${HANDLER}"
echo "    Memory:    ${MEMORY} MB"
echo "    Timeout:   ${TIMEOUT}s"
echo "    Region:    ${REGION}"
echo ""

# =============================================================================
# SECTION 3: Get IAM role ARN
# =============================================================================
# The Lambda function requires an IAM execution role. We created this role in
# create_dev_resources.sh. We query it here to get the ARN dynamically —
# hardcoding ARNs would break if the AWS account number changes.
echo "==> Fetching IAM role ARN for: ${ROLE_NAME}..."
ROLE_ARN=$(aws iam get-role \
  --role-name "${ROLE_NAME}" \
  --query 'Role.Arn' \
  --output text)
echo "    Role ARN: ${ROLE_ARN}"
echo ""

# =============================================================================
# SECTION 4: Get SQS queue URL and ARN
# =============================================================================
# The event source mapping requires the queue ARN. The queue URL is returned
# by get-queue-url; the ARN requires a separate get-queue-attributes call.
echo "==> Fetching SQS queue information..."
QUEUE_URL=$(aws sqs get-queue-url \
  --queue-name "csvforge-jobs-dev" \
  --region "${REGION}" \
  --query 'QueueUrl' \
  --output text)

QUEUE_ARN=$(aws sqs get-queue-attributes \
  --queue-url "${QUEUE_URL}" \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' \
  --output text)

echo "    Queue URL: ${QUEUE_URL}"
echo "    Queue ARN: ${QUEUE_ARN}"
echo ""

# =============================================================================
# SECTION 5: Build the deployment package
# =============================================================================
# Lambda zip deployment package layout:
#   /handler.py               (worker entry point)
#   /csv_processor.py         (CSV processing logic)
#   /exceptions.py            (worker-specific exceptions)
#   /backend/db/              (shared DB layer)
#   /backend/models/          (SQLAlchemy models)
#   /backend/s3_client.py     (S3 utilities)
#   /backend/sqs_client.py    (SQS utilities)
#   /<pip dependencies>/      (installed into root of zip)
#
# Why flatten dependencies to root: Lambda adds the zip root to PYTHONPATH.
# "import boto3" resolves to <zip_root>/boto3/ — but boto3 is already in the
# Lambda managed runtime, so we only need to install dependencies not in the
# runtime (e.g., SQLAlchemy, psycopg, pandas if used).

echo "==> Creating temporary build directory..."
TMPDIR=$(mktemp -d)
echo "    Build dir: ${TMPDIR}"

# Install Python dependencies into the temp directory.
# --quiet suppresses pip progress output.
# -t TARGET installs packages flat into the target directory (not site-packages).
echo "==> Installing worker dependencies..."
pip install -r "${PROJECT_ROOT}/worker/requirements.txt" \
  -t "${TMPDIR}" \
  --quiet
echo "    Dependencies installed."

# Copy worker source files into the build root.
# Lambda resolves "handler.lambda_handler" from the zip root, so handler.py
# must be at the top level of the zip (not in a subdirectory).
echo "==> Copying worker source files..."
cp "${PROJECT_ROOT}/worker/handler.py" "${TMPDIR}/"
cp "${PROJECT_ROOT}/worker/csv_processor.py" "${TMPDIR}/"
cp "${PROJECT_ROOT}/worker/exceptions.py" "${TMPDIR}/"

# Copy shared backend modules that the worker imports.
# The worker uses: backend.db, backend.models, backend.s3_client, backend.sqs_client
# These live in the API's backend/ directory and are shared via COPY (AD-28).
echo "==> Copying shared backend modules..."
mkdir -p "${TMPDIR}/backend"
cp -r "${PROJECT_ROOT}/backend/db" "${TMPDIR}/backend/"
cp -r "${PROJECT_ROOT}/backend/models" "${TMPDIR}/backend/"
cp "${PROJECT_ROOT}/backend/s3_client.py" "${TMPDIR}/backend/"
cp "${PROJECT_ROOT}/backend/sqs_client.py" "${TMPDIR}/backend/"
# backend/__init__.py must exist for Python to treat backend/ as a package
touch "${TMPDIR}/backend/__init__.py"

echo "    Source files copied."

# =============================================================================
# SECTION 6: Create the zip archive
# =============================================================================
# The zip is created from within TMPDIR so that all paths in the archive are
# relative (no leading /tmp/... prefix). Lambda requires relative paths in the zip.
ZIP_PATH="/tmp/csvforge-worker.zip"
echo "==> Creating deployment zip: ${ZIP_PATH}..."
(cd "${TMPDIR}" && zip -r "${ZIP_PATH}" . --quiet)
ZIP_SIZE=$(du -sh "${ZIP_PATH}" | cut -f1)
echo "    Zip created: ${ZIP_SIZE}"
echo ""

# =============================================================================
# SECTION 7: Create or update the Lambda function
# =============================================================================
# Idempotency: check if the function exists first.
# - If it doesn't exist: create it with all configuration.
# - If it exists: update the code (deploy new package), then update the
#   configuration separately (runtime, memory, timeout, env vars).
#
# Why two separate update calls? update-function-code and update-function-configuration
# are separate AWS API operations. Combining them requires waiting for the code
# update to finish (--wait) before the configuration can be updated.
echo "==> Creating or updating Lambda function: ${FUNCTION_NAME}..."

# DATABASE_URL defaults to PLACEHOLDER if not set in the local environment.
# In production, the Lambda retrieves it from Secrets Manager (AD-16).
DB_URL="${DATABASE_URL:-PLACEHOLDER}"

if aws lambda get-function --function-name "${FUNCTION_NAME}" --region "${REGION}" 2>/dev/null; then
  echo "    Function exists — updating code..."
  aws lambda update-function-code \
    --function-name "${FUNCTION_NAME}" \
    --zip-file "fileb://${ZIP_PATH}" \
    --region "${REGION}" \
    --output table

  echo "    Waiting for code update to complete..."
  aws lambda wait function-updated \
    --function-name "${FUNCTION_NAME}" \
    --region "${REGION}"

  echo "    Updating function configuration..."
  aws lambda update-function-configuration \
    --function-name "${FUNCTION_NAME}" \
    --runtime "${RUNTIME}" \
    --handler "${HANDLER}" \
    --memory-size "${MEMORY}" \
    --timeout "${TIMEOUT}" \
    --region "${REGION}" \
    --environment "Variables={
      DATABASE_URL=${DB_URL},
      S3_BUCKET_NAME=csvforge-files-dev,
      S3_OUTPUT_BUCKET=csvforge-files-dev,
      AWS_DEFAULT_REGION=ap-south-1
    }" \
    --output table
else
  echo "    Function does not exist — creating..."
  aws lambda create-function \
    --function-name "${FUNCTION_NAME}" \
    --runtime "${RUNTIME}" \
    --handler "${HANDLER}" \
    --memory-size "${MEMORY}" \
    --timeout "${TIMEOUT}" \
    --role "${ROLE_ARN}" \
    --region "${REGION}" \
    --zip-file "fileb://${ZIP_PATH}" \
    --environment "Variables={
      DATABASE_URL=${DB_URL},
      S3_BUCKET_NAME=csvforge-files-dev,
      S3_OUTPUT_BUCKET=csvforge-files-dev,
      AWS_DEFAULT_REGION=ap-south-1
    }" \
    --output table

  echo "    Waiting for function to be active..."
  aws lambda wait function-active \
    --function-name "${FUNCTION_NAME}" \
    --region "${REGION}"
fi

# Get the Lambda ARN for the summary output
LAMBDA_ARN=$(aws lambda get-function-configuration \
  --function-name "${FUNCTION_NAME}" \
  --region "${REGION}" \
  --query 'FunctionArn' \
  --output text)

echo "    Lambda ARN: ${LAMBDA_ARN}"
echo ""

# =============================================================================
# SECTION 8: Configure SQS Event Source Mapping
# =============================================================================
# The event source mapping is the "wire" between SQS and Lambda.
# When SQS has messages, Lambda polls the queue and invokes the function.
#
# BatchSize=1 means each Lambda invocation processes exactly one SQS message.
# This is intentional (AD-07): with BatchSize > 1, a single failure causes
# the entire batch to retry — including already-successful messages.
#
# Idempotency: check if a mapping already exists for this queue.
# Creating a duplicate mapping would cause Lambda to process each message twice.
echo "==> Configuring SQS event source mapping..."
EXISTING_MAPPING=$(aws lambda list-event-source-mappings \
  --function-name "${FUNCTION_NAME}" \
  --event-source-arn "${QUEUE_ARN}" \
  --region "${REGION}" \
  --query 'EventSourceMappings[0].UUID' \
  --output text 2>/dev/null || echo "None")

if [[ "${EXISTING_MAPPING}" != "None" && -n "${EXISTING_MAPPING}" ]]; then
  echo "    Event source mapping already exists: ${EXISTING_MAPPING}"
  MAPPING_ID="${EXISTING_MAPPING}"
else
  echo "    Creating new event source mapping..."
  MAPPING_ID=$(aws lambda create-event-source-mapping \
    --function-name "${FUNCTION_NAME}" \
    --event-source-arn "${QUEUE_ARN}" \
    --batch-size 1 \
    --region "${REGION}" \
    --query 'UUID' \
    --output text)
  echo "    Event source mapping created: ${MAPPING_ID}"
fi
echo ""

# =============================================================================
# SECTION 9: Cleanup and summary
# =============================================================================
echo "==> Cleaning up build directory..."
rm -rf "${TMPDIR}"
rm -f "${ZIP_PATH}"
echo "    Cleanup complete."
echo ""

echo "============================================================"
echo "  CSVForge Lambda Worker — Deployment Complete"
echo "============================================================"
echo ""
echo "  Lambda Function:"
echo "    Name:    ${FUNCTION_NAME}"
echo "    ARN:     ${LAMBDA_ARN}"
echo "    Runtime: ${RUNTIME}"
echo "    Memory:  ${MEMORY} MB"
echo "    Timeout: ${TIMEOUT}s"
echo ""
echo "  SQS Event Source:"
echo "    Queue:      ${QUEUE_ARN}"
echo "    Batch size: 1"
echo "    Mapping ID: ${MAPPING_ID}"
echo ""
echo "  To test the function manually:"
echo "    aws lambda invoke \\"
echo "      --function-name ${FUNCTION_NAME} \\"
echo "      --payload '{\"Records\":[{\"body\":\"{\\\"job_id\\\":\\\"test-123\\\"}\"}]}' \\"
echo "      /dev/stdout"
echo ""
echo "NOTE: AWS credentials are required. Run: aws configure (or set AWS_PROFILE)"
