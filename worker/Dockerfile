# ─────────────────────────────────────────────────────────────────────────────
# CSVForge Worker — Lambda Container Image Dockerfile
#
# WHAT:
#   Builds a Docker image for the CSV processing Lambda function.
#   The worker Lambda uses PackageType: Image (container-based deployment)
#   rather than the zip-based deployment used by the API Lambda (AD-05).
#
# WHY A CONTAINER (not a zip package):
#   The worker has a binary dependency: psycopg[binary] bundles libpq
#   (the PostgreSQL client library) as a compiled C extension. Compiled
#   extensions are OS/architecture-specific — a binary compiled on macOS
#   will not run on the Amazon Linux 2 environment inside Lambda.
#
#   Options:
#   a) Zip with Lambda layer: bundle psycopg binary in a Lambda layer.
#      Brittle — Lambda layers have size limits, version management is manual.
#   b) Container image: build on the exact OS Lambda uses (provided by AWS).
#      The binary compiles or is installed inside the correct environment.
#      No OS mismatch. Simple, reproducible, production-grade.
#
#   Container images also give us: consistent Python version, pinned OS
#   libraries, and a predictable build process. AD-05.
#
# HOW LAMBDA CONTAINER IMAGES WORK:
#   1. You build a Docker image and push it to Amazon ECR (container registry).
#   2. CloudFormation creates a Lambda function pointing to the ECR image URI.
#   3. When Lambda is invoked, AWS pulls the image and runs your handler.
#   4. The image MUST use a Lambda-compatible base image from ECR Public.
#   5. Lambda calls the function via the Lambda Runtime API:
#      POST /2015-03-31/functions/function/invocations
#      The CMD instruction names the handler: module.function_name.
#
# BASE IMAGE CHOICE (public.ecr.aws/lambda/python:3.14):
#   AWS publishes official Lambda base images on ECR Public. These images:
#   - Pre-install the Lambda Runtime Interface Client (RIC).
#   - Run Amazon Linux 2023 (the same OS as Lambda's managed environment).
#   - Set LAMBDA_TASK_ROOT=/var/task — the directory Lambda looks for code.
#   - Start the Lambda Runtime Interface Emulator (RIE) on local `docker run`.
#
#   Using a non-Lambda base image (e.g., python:3.14-slim) would require
#   manually installing the RIC and configuring the ENTRYPOINT.
#   The official image handles this — simpler and more reliable.
#
# IMPORTANT: BUILD CONTEXT IS THE PROJECT ROOT (not ./worker/)
#   The correct build command is:
#       docker build -f worker/Dockerfile -t csvforge-worker .
#                                                              ↑
#                                                 context = project root
#
#   WHY: The Dockerfile COPYs both worker/ and backend/ directories.
#   If the build context were ./worker/, only files inside worker/ would be
#   accessible to COPY instructions — backend/ would be invisible.
#   With context = . (project root), COPY can access any file in the project.
#   See AD-18 in docs/DECISIONS.md.
#
# LOCAL TESTING WITH LAMBDA RIE:
#   The official base image includes the Lambda Runtime Interface Emulator (RIE)
#   so you can invoke your handler locally without deploying to AWS:
#
#   1. Build the image:
#      docker build -f worker/Dockerfile -t csvforge-worker .
#
#   2. Run with RIE (note: RIE is the default ENTRYPOINT on local builds):
#      docker run -p 9000:8080 \
#        -e S3_BUCKET_NAME=csvforge-files-dev \
#        -e SQS_QUEUE_URL=... \
#        -e DATABASE_URL=postgresql+psycopg://... \
#        -e ENVIRONMENT=local \
#        csvforge-worker
#
#   3. Invoke via HTTP (in a second terminal):
#      curl -XPOST "http://localhost:9000/2015-03-31/functions/function/invocations" \
#        -d '{"Records":[{"body":"{\"job_id\":\"test-1\",\"s3_key\":\"uploads/...csv\"}"}]}'
#
# LAYER CACHING STRATEGY:
#   Same principle as the API Dockerfile: install dependencies (rarely changing)
#   before copying source code (frequently changing). This ensures that editing
#   handler.py does not bust the pip install cache.
#
# AD REFERENCES:
#   AD-05: Lambda deployed as Docker container via ECR
#   AD-18: Build context is project root (not ./worker/)
#   AD-22: Python 3.14 runtime
#   AD-28: Copy backend/ into the container image
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage: build the Lambda container image ───────────────────────────────────
#
# public.ecr.aws/lambda/python:3.13
#   Official AWS Lambda base image for Python 3.13 (stable, GA release).
#   Provides:
#     - LAMBDA_TASK_ROOT=/var/task (where Lambda looks for handler code)
#     - LAMBDA_RUNTIME_DIR=/var/runtime (Lambda Runtime Interface Client)
#     - ENTRYPOINT ["/lambda-entrypoint.sh"] (starts the Lambda RIC or RIE)
#     - CMD ["handler.lambda_handler"] (the handler — overridden below)
#   On ECR Public so no Docker Hub rate limits apply.
#   Python 3.14 was not compatible with Lambda in ap-southeast-2; using 3.13
#   which is the latest stable GA runtime supported across all Lambda regions.
#   See AD-22 in docs/DECISIONS.md for runtime selection rationale.
FROM public.ecr.aws/lambda/python:3.13

# ── Layer 1: Install Python dependencies ─────────────────────────────────────
# Copy only the requirements file first (not all source code) so this
# expensive pip install layer is cached when only source code changes.
#
# The worker has its own requirements.txt (worker/requirements.txt) with
# the minimal set of packages the worker actually uses.  This keeps the
# container image smaller than if we installed all backend requirements.
#
# Note: COPY paths are relative to the BUILD CONTEXT (project root, not worker/).
COPY worker/requirements.txt ${LAMBDA_TASK_ROOT}/worker-requirements.txt

# Install into LAMBDA_TASK_ROOT so the packages are found at runtime.
# --no-cache-dir: skip pip's disk cache — unnecessary inside Docker layers.
# --target=${LAMBDA_TASK_ROOT}: install packages directly into the Lambda
#   task root rather than the default site-packages.  Lambda adds
#   LAMBDA_TASK_ROOT to sys.path, so packages installed here are importable.
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/worker-requirements.txt \
    --target ${LAMBDA_TASK_ROOT}

# ── Layer 2: Copy backend shared modules ─────────────────────────────────────
# The worker imports from backend.db and backend.s3_client (AD-28).
# These live in the project's backend/ directory and MUST be available
# inside the container at the same relative path.
#
# Why copy backend/ into LAMBDA_TASK_ROOT?
#   Lambda adds LAMBDA_TASK_ROOT to sys.path. With backend/ inside
#   LAMBDA_TASK_ROOT, the import `from backend.db.job_repository import ...`
#   resolves as:
#     sys.path[0] = /var/task
#     /var/task/backend/db/job_repository.py  ← found ✓
#
# What from backend/ is needed by the worker:
#   backend/db/connection.py        — SessionLocal, database engine
#   backend/db/job_repository.py    — claim_job_for_processing, complete_job, fail_job
#   backend/models/job.py           — Job ORM model, JobStatus enum
#   backend/s3_client.py            — download_file, upload_file
#   backend/sqs_client.py           — parse_job_message (already imported in worker)
#   backend/__init__.py             — makes backend a package
#
# What from backend/ is NOT needed (API-only modules):
#   backend/main.py                 — FastAPI app; not used in worker
#   backend/auth.py                 — JWT dependency; not used in worker
#   backend/schemas.py              — Pydantic schemas; not used in worker
#   backend/alembic/                — migration scripts; not needed at runtime
#
# We copy the entire backend/ directory for simplicity. The extra files add
# a few KB to the image — negligible for a learning project. A production
# optimization would package only the required modules (AD-28 trade-off).
COPY backend/ ${LAMBDA_TASK_ROOT}/backend/

# ── Layer 3: Copy worker source code ─────────────────────────────────────────
# Copy the worker-specific source files AFTER backend/ to maximise layer
# caching: backend/ rarely changes; worker/ changes more often.
#
# The worker module structure inside LAMBDA_TASK_ROOT becomes:
#   /var/task/
#     handler.py          ← lambda_handler() entry point (CMD below)
#     csv_processor.py    ← parse_and_analyse(), clean(), write_csv()
#     exceptions.py       ← InvalidCSVError
#     backend/            ← shared backend modules (from step above)
#       db/
#         connection.py
#         job_repository.py
#       models/
#         job.py
#       s3_client.py
#       sqs_client.py
#
# Note: we copy worker/*.py files individually to exclude worker/Dockerfile
# itself, worker/requirements.txt (already installed), and any __pycache__.
# Using COPY worker/ ${LAMBDA_TASK_ROOT}/ would include those files too —
# harmless but slightly wasteful.
COPY worker/handler.py ${LAMBDA_TASK_ROOT}/handler.py
COPY worker/csv_processor.py ${LAMBDA_TASK_ROOT}/csv_processor.py
COPY worker/exceptions.py ${LAMBDA_TASK_ROOT}/exceptions.py

# ── CMD: Lambda handler entry point ──────────────────────────────────────────
# Format: "module_name.function_name"
# Lambda looks for a file named handler.py in LAMBDA_TASK_ROOT and calls
# the function lambda_handler(event, context) inside it.
#
# Why "handler.lambda_handler" (not "worker.handler.lambda_handler")?
#   handler.py is copied directly into LAMBDA_TASK_ROOT (/var/task/handler.py),
#   not into a worker/ subdirectory. The Lambda runtime resolves the handler
#   relative to LAMBDA_TASK_ROOT: "handler" → /var/task/handler.py.
#   If we had copied to ${LAMBDA_TASK_ROOT}/worker/handler.py, the CMD
#   would be "worker.handler.lambda_handler". Either approach works; placing
#   handler.py at the top level matches the Lambda documentation examples.
#
# The CMD is overridden by the Lambda function's Handler property in
# CloudFormation (worker.yaml), but keeping it correct here ensures
# local RIE testing works without extra configuration.
CMD ["handler.lambda_handler"]
