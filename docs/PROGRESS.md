# CSVForge Progress

## Project Goal

Build CSVForge in 7 days as a deep-learning exercise. The goal is not maximum implementation speed — it is durable engineering understanding: being able to explain, debug, modify, and defend every component.

## Learning Method

For each CORE task:
1. Read the task and understand why it exists
2. Get the concepts explained
3. Answer prediction questions before implementation
4. Review proposed implementation
5. Implement
6. Run tests
7. Intentionally break something and debug it
8. Explain the implementation back (no looking at code)
9. Review the diff
10. Commit

All implementation uses supervised mode (not autopilot). One task at a time.

## Status

**Phase:** Pre-implementation — planning and architecture audit complete

**Audit date:** October 6, 2026

## Architecture Audit Complete

All issues identified in the planning audit have been resolved in the spec documents. See `docs/DECISIONS.md` for the canonical decision record. Issues resolved:

| Issue | Resolution | AD |
|-------|-----------|-----|
| Duplicate headers should be a permanent validation failure | Revised CSV contract — duplicates = InvalidCSVError | AD-21 |
| Worker memory: 512 MB too low for 50 MiB in-memory processing | Increased to 1024 MB; documented amplification factor | AD-01 |
| Worker Dockerfile missing backend/ modules | Added COPY backend/ to Dockerfile | AD-28 |
| Upload size not enforced server-side | content-length-range + HeadObject verification | AD-27 |
| Worker user_id from S3 key path (untrusted) | Use job.user_id from PostgreSQL DB | AD-08 |
| updated_at not updated on state transitions | Added onupdate=func.now() to SQLAlchemy model | AD-24 |
| DB-before-SQS failure window undocumented | Documented as known limitation; made observable | AD-29 |
| CloudFormation --query-stack-outputs (fictional flag) | Replaced with real AWS CLI patterns | AD-09 |
| CI/CD: build and deploy mixed in buildspec.yml | Separated: CodeBuild builds, Deploy stage deploys | AD-30 |
| S3 CORS missing for direct browser upload | Added to data.yaml and API CORS middleware | AD-28 |
| design.md §7.3 stale visibility timeout (360s) | Fixed to 1800s (matches AD-17) | AD-17 |
| design.md §3.13 React Router v6 (stale) | Updated to v8 | — |
| T-33 worker depends on API Gateway (incorrect) | Corrected: worker is sibling, not dependent | AD-26 |
| Cognito AllowedOAuthFlows: implicit unnecessary | Removed; documented flow in use | — |
| R-AUTH-04 "refresh handled by provider" ambiguous | Clarified: user must re-authenticate on expiry | AD-10 |
| FastAPI 0.140.0 → 0.141.1 | Updated in VERSIONS.md | AD-22 |

## Task Plan

See `.kiro/specs/csv-processing/tasks.md` for the complete 7-day plan.

**CORE tasks:** 28 tasks across Days 1–7  
**STRETCH tasks:** 5+ tasks (CI/CD hardening, CloudFront, etc.)

## Progress Log

*(Updated after each completed task)*

