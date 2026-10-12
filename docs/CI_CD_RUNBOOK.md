# CSVForge CI/CD Runbook

Reference for the complete CI/CD pipeline. Distinguishes what is implemented,
what is deployed, and what still needs manual setup.

---

## Pipeline overview

```
GitHub push to main
        │
        ▼
Stage 1 — Source
  CodeConnections fetches repo → SourceArtifact (ZIP in S3)
        │
        ▼
Stage 2 — Test  (parallel)
  ├─ BackendTests:   pytest 197 unit tests + cfn-lint + validate-template
  └─ FrontendTests:  tsc --noEmit + eslint + vitest 28 tests
        │  (both must pass)
        ▼
Stage 3 — Build  (parallel)
  ├─ BuildAPI:    pip install → ZIP → s3://artifacts/api/csvforge-api-{sha}.zip
  └─ BuildWorker: docker build → ECR push → image digest recorded
        │
        ▼
Stage 4 — Deploy  (parallel)
  ├─ DeployAPI:      lambda update-function-code (S3 ZIP) → wait-updated
  ├─ DeployWorker:   lambda update-function-code (ECR digest) → wait-updated
  └─ DeployFrontend: read SSM → vite build → s3 sync → CloudFront invalidation
        │  (all three must succeed)
        ▼
Stage 5 — Verify
  SmokeTests: /health (200) + GET /jobs with auth (200) + CloudFront (200)
        │
        ▼
Stage 6 — Infrastructure Gate
  Manual approval → reviewer checks CloudFormation change sets → Approve/Reject
```

---

## Deployment order for first-time setup

Deploy these stacks in this exact order. Each stack imports outputs from the ones before it.

```
1. csvforge-pipeline-bootstrap-dev   → artifact bucket + GitHub connection
   THEN: approve the GitHub connection in Console (Developer Tools → Connections)

2. csvforge-network-dev              → VPC, subnets, security groups, VPC endpoints

3. csvforge-data-dev                 → S3 bucket for CSV files

4. csvforge-queues-dev               → SQS main queue + DLQ

5. RDS instance                      → manual (not yet in CloudFormation)
   Create Secrets Manager secret: csvforge-db-credentials-dev

6. csvforge-api-dev                  → Cognito, API Lambda, API Gateway
   Parameters required: RDSEndpoint, DBSecretArn, LambdaZipS3Bucket

7. csvforge-worker-dev               → ECR, Worker Lambda, SQS event source mapping
   Parameters required: RDSEndpoint, DBSecretArn, ImageTag

8. csvforge-frontend-dev             → frontend S3 bucket + CloudFront

9. Run Alembic migration manually    → see Migration procedure below

10. Populate SSM parameters          → see SSM setup below

11. csvforge-pipeline-dev            → the pipeline itself
    Parameters required: GitHubConnectionArn, GitHubOwner, ArtifactBucketName, ...
```

---

## SSM parameters to create after stack deploys

These are read by the deploy and smoke test buildspecs at runtime.
Replace placeholder values with real CloudFormation outputs.

```bash
POOL_ID=$(aws cloudformation describe-stacks --stack-name csvforge-api-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" \
  --output text)
CLIENT_ID=$(aws cloudformation describe-stacks --stack-name csvforge-api-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoAppClientId'].OutputValue" \
  --output text)
API_URL=$(aws cloudformation describe-stacks --stack-name csvforge-api-dev \
  --query "Stacks[0].Outputs[?OutputKey=='APIEndpoint'].OutputValue" \
  --output text)
FRONTEND_BUCKET=$(aws cloudformation describe-stacks --stack-name csvforge-frontend-dev \
  --query "Stacks[0].Outputs[?OutputKey=='FrontendBucketName'].OutputValue" \
  --output text)
CF_DIST_ID=$(aws cloudformation describe-stacks --stack-name csvforge-frontend-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDistributionId'].OutputValue" \
  --output text)
CF_URL=$(aws cloudformation describe-stacks --stack-name csvforge-frontend-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontURL'].OutputValue" \
  --output text)

aws ssm put-parameter --name /csvforge/dev/cognito-user-pool-id    --value "$POOL_ID"     --type String
aws ssm put-parameter --name /csvforge/dev/cognito-app-client-id   --value "$CLIENT_ID"   --type String
aws ssm put-parameter --name /csvforge/dev/api-gateway-url         --value "$API_URL"     --type String
aws ssm put-parameter --name /csvforge/dev/cloudfront-url          --value "$CF_URL"      --type String
aws ssm put-parameter --name /csvforge/dev/frontend-bucket-name    --value "$FRONTEND_BUCKET" --type String
aws ssm put-parameter --name /csvforge/dev/cloudfront-dist-id      --value "$CF_DIST_ID"  --type String
aws ssm put-parameter --name /csvforge/dev/rds-endpoint            --value "<rds-endpoint>" --type String
aws ssm put-parameter --name /csvforge/dev/db-secret-arn           --value "<secret-arn>"  --type String
```

---

## Creating the smoke test user

Run once after Cognito is deployed:

```bash
POOL_ID=$(aws ssm get-parameter --name /csvforge/dev/cognito-user-pool-id --query Parameter.Value --output text)

aws cognito-idp admin-create-user \
  --user-pool-id $POOL_ID \
  --username ci@csvforge.internal \
  --temporary-password 'TempCI1!' \
  --message-action SUPPRESS

aws cognito-idp admin-set-user-password \
  --user-pool-id $POOL_ID \
  --username ci@csvforge.internal \
  --password 'YourStrongPermanentPassword1!' \
  --permanent

aws ssm put-parameter \
  --name /csvforge/dev/smoke-test-username \
  --value ci@csvforge.internal \
  --type String

aws ssm put-parameter \
  --name /csvforge/dev/smoke-test-password \
  --value 'YourStrongPermanentPassword1!' \
  --type SecureString
```

---

## Migration procedure

Migrations are NEVER run automatically by the pipeline. Always manual.

**Before running:**
1. Review the migration files in `backend/alembic/versions/`.
2. Confirm the migration is backward-compatible.
3. Take an RDS snapshot if the migration is risky.

**Run:**
```bash
aws codebuild start-build \
  --project-name csvforge-migrate-dev \
  --region ap-south-1
```

Watch the build in the Console. Confirm `alembic current` shows the expected revision after completion.

**Production migration** (extra confirmation required):
```bash
aws codebuild start-build \
  --project-name csvforge-migrate-prod \
  --environment-variables-override name=CONFIRM_PRODUCTION,value=yes \
  --region ap-south-1
```

---

## Rollback procedures

**API Lambda rollback:**
```bash
# Find the previous SHA from the deployment record
aws s3 cp s3://csvforge-pipeline-artifacts-dev/deployments/api/current.json - | python3 -m json.tool
# Then deploy the previous SHA's ZIP:
aws lambda update-function-code \
  --function-name csvforge-api-dev \
  --s3-bucket csvforge-pipeline-artifacts-dev \
  --s3-key api/csvforge-api-{previous-sha}.zip
aws lambda wait function-updated --function-name csvforge-api-dev
```

**Worker Lambda rollback:**
```bash
# Find the previous image digest
aws s3 cp s3://csvforge-pipeline-artifacts-dev/deployments/worker/current.json - | python3 -m json.tool
# Then deploy the previous digest:
aws lambda update-function-code \
  --function-name csvforge-worker-dev \
  --image-uri {ecr-repo}@sha256:{previous-digest}
aws lambda wait function-updated --function-name csvforge-worker-dev
```

**Frontend rollback:**
There is no automated frontend rollback (old assets are expired after 90 days).
To roll back: re-trigger the pipeline at the previous commit SHA, or manually sync from a saved dist/ backup.

---

## Pipeline troubleshooting

| Symptom | Where to look | Likely cause |
|---------|--------------|--------------|
| Source stage fails | Console → Connections | GitHub connection in PENDING state |
| BackendTests fails | CodeBuild logs | Test failure or cfn-lint error |
| FrontendTests fails | CodeBuild logs | TypeScript error, lint error, or test failure |
| BuildWorker fails with "daemon" error | CodeBuild project config | PrivilegedMode not enabled |
| BuildWorker fails with ECR 403 | IAM role | Missing ECR permissions |
| DeployAPI fails with ResourceConflict | Previous Lambda update still InProgress | Increase timeout or re-run |
| SmokeTest /health fails with 502 | Lambda CloudWatch logs | Runtime.HandlerNotFound — Mangum handler missing |
| SmokeTest /health fails with 500 | Lambda CloudWatch logs | Cold-start crash — check Secrets Manager IAM |
| SmokeTest frontend fails | CloudFront logs | OAC misconfigured or invalidation not propagated |
| Manual approval timeout | Check SNS email | Email not subscribed, or approver missed notification |

---

## Current status

| Component | Implemented | Deployed | Verified |
|-----------|-------------|----------|---------|
| Lab 1 fixes (Mangum + IAM) | ✅ | ❌ Not deployed | ❌ |
| Bootstrap stack template | ✅ | ❌ | ❌ |
| Backend test buildspec | ✅ | ❌ | ❌ |
| Frontend test buildspec | ✅ | ❌ | ❌ |
| API build buildspec | ✅ | ❌ | ❌ |
| Worker build buildspec | ✅ | ❌ | ❌ |
| API deploy buildspec | ✅ | ❌ | ❌ |
| Worker deploy buildspec | ✅ | ❌ | ❌ |
| Frontend deploy buildspec | ✅ | ❌ | ❌ |
| Smoke test buildspec | ✅ | ❌ | ❌ |
| Migration buildspec | ✅ | ❌ | ❌ |
| frontend.yaml CloudFormation | ✅ | ❌ | ❌ |
| pipeline.yaml CloudFormation | ✅ | ❌ | ❌ |
| Application stacks (5) | ✅ templates | ❌ | ❌ |
| RDS PostgreSQL | ❌ no template | ❌ | ❌ |
