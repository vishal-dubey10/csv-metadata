# CSVForge Documentation Pass Report

---

## MENTAL_MAP.md Rewrite — Completion Note

**Task completed:** Full rewrite of `docs/MENTAL_MAP.md`.

**What was written:**
- Section 1: Project overview (5 sentences, end-to-end flow)
- Section 2: Architecture diagram (ASCII, arrow-by-arrow table)
- Section 3: File index — all 18 files read; every public function/class indexed with one-line descriptions and jump-to docstring pointers
- Section 4: Concept map — 40+ concepts mapped to exact file + symbol + brief explanation
- Section 5: GATE 2027 CS syllabus mapping — 8 areas (Databases, OS, Computer Networks, DS&A, Algorithms, Theory of Computation, Software Engineering, Cloud/Distributed Systems) with topic/sub-topic/concept/file/how columns
- Section 6: Learning path — 19-step reading order with rationale per step
- Appendix: Quick command reference

**GATE 2027 source:** Topics confirmed from IIT Madras's released 2027 syllabus via Made Easy and GeeksForGeeks (2025 publications). Content rephrased for licensing compliance.

**Tests after change:** `PYTHONPATH=. .venv/bin/pytest tests/unit/ -v` → **197 passed in 3.38s**. No Python files modified.

**Commit:** `3877bc2` — `docs: fully rewrite MENTAL_MAP.md with file index, concept map, GATE 2027 syllabus mapping, and learning path`

---

## Final Test Result

```
197 passed in 3.63s
```

All tests passed. No behavior changes introduced.

---

## Files Annotated

| File | Changes Made |
|------|-------------|
| `backend/__init__.py` | Already comprehensive — no changes needed |
| `backend/main.py` | Already comprehensive — no changes needed |
| `backend/auth.py` | Already comprehensive — no changes needed |
| `backend/schemas.py` | Fixed misplaced `model_config` docstring: the string literal that appeared after the class attribute was not a real docstring (Python doesn't recognize it as one). Replaced with a proper `#` comment block explaining `from_attributes=True`, the ORM-to-schema bridge, and the Pydantic v1 → v2 rename from `orm_mode` to `from_attributes`. |
| `backend/s3_client.py` | Already comprehensive — no changes needed |
| `backend/sqs_client.py` | Already comprehensive — no changes needed |
| `backend/db/__init__.py` | Already comprehensive — no changes needed |
| `backend/db/connection.py` | Already comprehensive — no changes needed |
| `backend/db/job_repository.py` | Expanded module docstring with full WHAT, WHY, IMPORTANT CONCEPTS, and Interview concept sections. Rewrote `list_all_jobs` docstring with WHAT/WHY/HOW/Inputs/Output sections. Rewrote `count_all_jobs` docstring with full explanation of the local import alias (`sa_func`). Rewrote `complete_job` docstring with full HOW and WHY S3-before-DB ordering. Rewrote `fail_job` docstring explaining why the worker returns normally (not raises) after calling fail_job — the SQS retry semantics. |
| `backend/models/__init__.py` | Already comprehensive — no changes needed |
| `backend/models/job.py` | Already comprehensive — no changes needed |
| `backend/alembic/env.py` | Already comprehensive — no changes needed |
| `backend/alembic/versions/0001_initial_jobs_table.py` | Already comprehensive — no changes needed |
| `worker/__init__.py` | Already comprehensive — no changes needed |
| `tests/conftest.py` | Expanded module docstring with WHAT section listing all fixtures, fixed a factual error (the original docstring referenced `setdefault("DATABASE_URL", ...)` which doesn't actually happen — corrected to accurately describe how SQLite and PostgreSQL engines are used separately), added GATE CS concept on fixture lifecycle. Expanded `SQLITE_URL` comment to explain the two-engine architecture and why `check_same_thread=False` is needed. Improved `os.environ.setdefault` comment to explain `setdefault` vs direct assignment. |
| `tests/unit/test_health.py` | Expanded module docstring with WHAT, WHY TEST THE HEALTH ENDPOINT, and GATE CS concept on integration-light testing. Added explanation of when health checks are used in production (load balancer probes). |
| `tests/unit/test_job_model.py` | Expanded module docstring with WHAT WOULD FAIL IF THESE TESTS BREAK section (connects each test to a real failure scenario) and GATE CS concept on NOT NULL constraints and enforcing invariants. |
| `tests/unit/test_job_repository_unit.py` | Expanded module docstring with full WHAT'S TESTED / NOT TESTED structure, WHY SQLITE section, ARCHITECTURE OF A REPOSITORY TEST pattern, GATE CS concept on test isolation and the transaction model, and a NOTE on `test_second_claim_returns_false` naming. |

---

## Files Already Comprehensive (no changes needed)

The following files had excellent documentation before this pass — they were read but left unchanged:

- `backend/__init__.py` — clear package marker with full sub-package map
- `backend/main.py` — 400+ lines of module docstring covering WSGI vs ASGI, Mangum, full request lifecycle, endpoint overview, and auth/authz decisions
- `backend/auth.py` — full auth flow diagram, local dev bypass explanation, JWT claim paths, ownership enforcement, authn vs authz interview concept
- `backend/s3_client.py` — presigned URL analogy (visitor pass), HeadObject explanation, strong S3 consistency interview concept, full docstrings on every function
- `backend/sqs_client.py` — claim-check pattern, at-least-once delivery, message format explanation, SQS as job ticket board analogy
- `backend/db/__init__.py` — clean package marker with dependency graph
- `backend/db/connection.py` — restaurant analogy for Engine/Pool/Connection/Session, stale connection problem with pool_pre_ping, pool_size=1 rationale, expire_on_commit=False deep dive, get_db() lifecycle diagram
- `backend/models/__init__.py` — clean package marker with dependency graph
- `backend/models/job.py` — ORM analogy, JobStatus state machine diagram, UUID vs sequential PK trade-offs, ARRAY(TEXT).with_variant() explanation, TIMESTAMP vs TIMESTAMPTZ, server_default vs onupdate comparison table
- `backend/alembic/env.py` — migration chain, offline vs online mode, NullPool rationale, alembic_version table explanation
- `backend/alembic/versions/0001_initial_jobs_table.py` — per-column rationale (UUID, VARCHAR vs ENUM, ARRAY, TIMESTAMPTZ), index design, MVCC context
- `worker/__init__.py` — clear placeholder with future architecture diagram

---

## Concepts Cross-Referenced (not re-explained)

| Concept | Defined In | Cross-Referenced From |
|---------|-----------|----------------------|
| Engine / Session / Pool analogy | `connection.py` | `job_repository.py` (see also) |
| `expire_all()` / identity map | `job_repository.py` (claim_job docstring) | `complete_job`, `fail_job` docstrings |
| `with_variant(TEXT, "sqlite")` for ARRAY | `models/job.py` | `conftest.py` (why SQLite can't test ARRAY) |
| `from_attributes=True` / ORM bridge | `schemas.py` | `main.py` (`_job_to_response` docstring) |
| `check_same_thread=False` | `conftest.py` | test module docstrings reference it |
| `synchronize_session` modes | `job_repository.py` (`transition_to_queued`) | `claim_job_for_processing`, `complete_job`, `fail_job` |
| presigned URL mechanism | `s3_client.py` | `main.py` (download endpoint docstring) |
| SQS at-least-once delivery / idempotency | `sqs_client.py` | `job_repository.py` (`claim_job_for_processing`) |
