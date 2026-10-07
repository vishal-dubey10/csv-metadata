---
inclusion: always
---
# AWS / Distributed Systems Learning Standard

This document establishes the standards for all AWS and distributed systems work in CSVForge.
These standards exist because CSVForge is a learning project — the architecture must be
comprehensible, not just functional.

## Core Principles

### 1. Expose, Don't Hide
AWS integrations must be implemented in a way that exposes the underlying distributed-systems
concepts rather than hiding them behind abstractions. A developer reading this code should
learn how S3, SQS, and Lambda work — not just that they work.

### 2. Resource Documentation
Every AWS resource must have documented: ownership, purpose, permissions, lifecycle,
failure behavior, and relationship to other resources.

### 3. Control Plane vs Data Plane
AWS integrations must distinguish:
- Control plane: configuration, IAM, queue settings, Lambda configuration
- Data plane: actual execution — CSV bytes, S3 objects, SQS messages, Lambda invocations

### 4. Delivery Semantics Documentation
Every asynchronous workflow must document: delivery semantics, retries, visibility timeouts,
idempotency, failure handling, and DLQ behavior.

### 5. Least Privilege IAM
IAM permissions must follow least privilege. Never introduce broad administrator permissions
for runtime workloads. Each runtime role should have exactly the permissions it needs.

### 6. Environment Separation
Development and production resources must be clearly distinguishable.
Name convention: `{service}-{environment}` (e.g., `csvforge-jobs-dev`, `csvforge-jobs-prod`).

### 7. Test Separation
Real AWS integration tests must be explicitly marked with `@pytest.mark.aws`.
They must NEVER run accidentally as part of `pytest tests/` (unit tests only).
Command for AWS tests: `pytest -m aws -v`

### 8. Credential Safety
AWS credentials and secrets must never be committed to source control.
Use IAM roles inside AWS. Use the AWS credential chain for local development.
Never put credentials in .env files that could be committed.

### 9. Mental Models
Difficult infrastructure concepts must be explained with intuitive mental models and analogies.
The factory analogy is the primary mental model for CSVForge (see DAY2_LEARNING_GUIDE.md).

### 10. Educational Continuity
Future implementation days must continue this educational documentation standard automatically.
Each day's learning guide must explain concepts from first principles with the factory analogy.
