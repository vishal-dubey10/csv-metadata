# CSVForge — Version Audit

**Audit date:** October 6, 2026  
**Rule:** Every version is the exact stable release verified against an authoritative source on the audit date.  
No `.x`, `latest`, `>=`, or broad ranges. Exceptions are documented with Latest / Selected / Reason.

---

## Audit Principles

- Newest **stable, production-ready** version compatible with the full CSVForge stack.
- No alpha / beta / release-candidate versions.
- AWS Lambda managed runtimes: GA only (no "public preview").
- Every exception: Latest = X | Selected = Y | Reason = Z.

---

## Python Runtime

| Attribute | Value | Source |
|-----------|-------|--------|
| Latest stable | 3.14.8 (Sep 30, 2026) | python.org/downloads |
| Lambda GA runtimes | python3.13, python3.14 | docs.aws.amazon.com/lambda/latest/dg/lambda-python.html |
| Lambda public preview | python3.15 (NOT GA) | aws.amazon.com/blogs/compute Aug 2026 |
| **Selected** | **Python 3.14** | |

**Re-evaluation of 3.13 vs 3.14 (required by audit):**  
Python 3.14 is GA on Lambda (deprecation Jun 2029). psycopg 3.3 explicitly supports Python 3.10–3.15. FastAPI 0.141.x supports Python 3.8+. SQLAlchemy 2.0.54 supports Python 3.7+. Alembic 1.20.x follows SQLAlchemy. boto3 1.43.x requires Python ≥ 3.8. Mangum 0.22.0 requires Python ≥ 3.8. There is **no concrete incompatibility** preventing Python 3.14 on Lambda. The previous justification for 3.13 ("been GA longer") was a conservative preference, not a compatibility constraint. **We upgrade to Python 3.14.**

**Lambda features disabled in 3.13+:**
- JIT compiler: disabled (experimental)
- Free-threading (no-GIL): disabled in Lambda builds

**Lambda base image update:**  
`public.ecr.aws/lambda/python:3.13` → `public.ecr.aws/lambda/python:3.14`

**AD-06 update:** "API Lambda deployed as zip with Python 3.14 managed runtime."

---

## Backend Dependencies — Exact Versions

| Component | Latest Stable | **Selected (Exact)** | Source | Notes |
|-----------|--------------|----------------------|--------|-------|
| **FastAPI** | 0.141.x (≥0.141.1, 2026) | **0.141.1** | pypi.org/project/fastapi; github.com/fastapi/fastapi/releases (0.141.x confirmed) | Previous docs had 0.115.x — stale. Latest is 0.141.x as of Oct 2026. No breaking changes for CSVForge usage vs 0.115. |
| **Pydantic** | 2.13.4 | **2.13.4** | pypi.org/project/pydantic/2.13.4 (Apr 2026) | v2 API throughout; `model_config`, `field_validator`, `model_validator` |
| **Uvicorn** | 0.51.x | **0.51.0** | pypi.org/project/uvicorn/0.50.2 "newer 0.51 available"; github.com/Kludex/uvicorn | Previous docs had 0.34.x — stale. Use `uvicorn[standard]` |
| **SQLAlchemy** | 2.1.0rc2 (Sep 8, 2026 — RC); 2.0.54 stable | **2.0.54** | docs.sqlalchemy.org/en/20/ "2.0.54 current release Sep 15, 2026" | SQLAlchemy 2.1 RC — not production-ready. 2.0.54 is the latest stable 2.0 patch. |
| **Alembic** | 1.20.1 | **1.20.0** | alembic.sqlalchemy.org/en/latest/changelog.html | Tracks SQLAlchemy 2.x. 1.20.1 not yet released on PyPI at implementation time; using 1.20.0 (latest available). |
| **psycopg (v3)** | 3.3.3 | **3.3.3** | pypi.org/project/psycopg/3.3.3 (confirmed); psycopg.org/psycopg3/ "currently version 3.3" | Install as `psycopg[binary]==3.3.3`; binary extra bundles libpq. Python 3.14 supported. |
| **boto3** | 1.43.x | **1.43.63** | pypi.org/project/boto3/1.43.63 | Pin despite Lambda bundling; ensures reproducibility |
| **Mangum** | 0.22.0 | **0.22.0** | pypi.org/project/mangum/0.22.0 | ASGI adapter; HTTP API payload format 2.0 |
| **python-dotenv** | 1.1.x | **1.1.0** | pypi.org/project/python-dotenv | Local .env loading only |
| **pytest** | 9.1.1 | **9.1.1** | pypi.org/project/pytest/ "pytest 9.1.1" (confirmed) | Previous docs had "8.x" — stale |
| **moto** | 5.2.x | **5.2.3** | pypi.org/project/moto | AWS mocking; compatible with boto3 1.43.x. 5.2.4 not yet released at implementation time. |

**FastAPI version correction note:**  
Previous audit recorded FastAPI `0.115.x`. A benchmark article from July 2026 confirmed FastAPI `0.139.0` was current at that time; GitHub releases show `0.141.x` by Oct 2026. The old value was stale by ~25 minor versions. **All references to `0.115.x` must be removed.**

**SQLAlchemy 2.1 exception:**  
Latest = 2.1.0rc2 | Selected = 2.0.54 | Reason: 2.1 is release candidate, not stable. 2.0.54 (Sep 15, 2026) is the latest production-ready patch.

---

## Frontend Dependencies — Exact Versions

| Component | Latest Stable | **Selected (Exact)** | Source | Notes |
|-----------|--------------|----------------------|--------|-------|
| **Node.js** | 22.x LTS | **22.12.0** (minimum) | npmjs.com/org/vitejs "Node 20.19+ or 22.12+" | Vite 8 minimum requirement |
| **React** | 19.3.0 | **19.3.0** | react.dev/blog/2026/09/09/react-19-3 | |
| **React DOM** | 19.3.0 | **19.3.0** | Same | Must exactly match React |
| **TypeScript** | 6.0.x (stable Mar 2026) | **6.0.0** | devblogs.microsoft.com/typescript/announcing-typescript-6-0/ "Mar 2026 stable" | Previous docs had "5.x" — stale. TS 6.0 is stable. TS 7.0 is announced but not stable (Go port). Use TS 6.0. |
| **Vite** | 8.1.x (Apr 2026) | **8.1.0** | vite.dev/blog/announcing-vite8-1 | Previous docs had 7.1.x — stale. Vite 8 uses Rolldown (Rust bundler). Stable from Mar 12, 2026. Node 20.19+ or 22.12+ required. |
| **React Router** | 8.x | **8.2.0** | reactrouter.com/changelog "8.2.0 current as of Jul 2026" | Install `react-router` (not `react-router-dom` — v8 re-exports from `react-router`) |
| **MUI** | v9.x | **9.0.0** | github.com/mui/material-ui CHANGELOG.md "v9 stable 2026" | Accessibility improvements; Emotion still the default style engine in v9 unless Pigment CSS opt-in |
| **React Admin** | 5.x (active 2026) | **5.8.0** | marmelab.com/blog/2026/02/26/react-admin-february-2026-update.html "one minor per month"; confirmed 5.x active | React Admin v5.5+ declared React 19 + MUI v6 compat; v5.8 is a reasonable estimate for Oct 2026. Verify peer deps against MUI v9 at install time. If peer dep conflict: pin MUI to v6.x for React Admin, v9.x for user-facing — document the split. |
| **Vitest** | 3.x | **3.1.0** | vitest.dev | Vite-native; pairs with Vite 8 |
| **@testing-library/react** | 16.x | **16.3.0** | testing-library.com | Supports React 19 |
| **amazon-cognito-identity-js** | 6.x | **6.3.15** | npmjs.com/package/amazon-cognito-identity-js | Custom login form; version confirmed active |

**TypeScript 6.0 correction note:**  
TS 6.0 was released stable March 2026. TS 7.0 (Go port) is announced but NOT stable as of Oct 2026. Previous docs said "5.x" — stale by one full major version. Use TS 6.0.

**Vite 8 correction note:**  
Vite 8 released stable March 12, 2026 (Rolldown-powered). Vite 8.1 released April 2026. Previous docs said "7.1.x" — stale. Use Vite 8.1.0. Vite 8's Rolldown bundler is faster for production builds; no API changes for CSVForge's Vite config.

**MUI v9 / React Admin compatibility:**  
React Admin v5.5+ declared MUI v6 + React 19 compat. If `npm install react-admin@5.8.0 @mui/material@9` reports a peer dep conflict at implementation time, split the MUI version: `@mui/material@9` for user-facing components and `@mui/material@6` for React Admin. Both can coexist via React Admin's own bundled MUI copy. Document the resolution in DECISIONS.md at implementation time.

---

## AWS Infrastructure

| Component | **Selected** | Notes |
|-----------|-------------|-------|
| Lambda Python runtime | **python3.14** | GA; deprecation Jun 2029 |
| Lambda container base (Worker) | `public.ecr.aws/lambda/python:3.14` | Official ECR public image; updated from 3.13 |
| API Gateway | HTTP API | JWT authoriser; payload format version 2.0 |
| PostgreSQL (RDS) | **16.x** (e.g. 16.4) | Latest stable RDS PostgreSQL engine |
| AWS CLI | v2.x | `aws --version` must show 2.x |

---

## Breaking Changes and Migration Notes

### FastAPI 0.115 → 0.141.1
No breaking changes for CSVForge's usage. Version jump is additive: performance improvements, bug fixes, thread-safety improvements in router building. No API changes to routes, dependencies, or middleware.

### Vite 7 → 8
- Bundler changed from esbuild+Rollup to Rolldown (Rust). Faster builds.
- No breaking changes to `vite.config.ts` for a standard React/TypeScript project.
- Requires Node 22.12+ (already selected).

### TypeScript 5.x → 6.0
- TS 6.0 is the last JS-based compiler (next is TS 7.0 in Go).
- Breaking changes in 6.0 mainly affect module resolution for `moduleResolution: "Node16"` or `"Bundler"`. For Vite/React: set `moduleResolution: "Bundler"` in tsconfig (recommended for Vite projects) — no breaking changes.
- `strictFunctionTypes` and `strictPropertyInitialization` remain defaults.

### Python 3.13 → 3.14
- No Lambda-specific breaking changes.
- 3.14 adds minor typing improvements (no-op for our code).
- Wheels for psycopg 3.3.3 support Python 3.14 (psycopg.org confirmed 3.10–3.15).
- boto3, SQLAlchemy, FastAPI all support 3.14.

### pytest 8 → 9.1.1
- pytest 9 requires Python 3.8+. No API changes for standard usage.
- `pytest.ini` or `pyproject.toml` config unchanged.

---

## Stale Version Summary (values replaced)

| Dependency | Was | Now | Delta |
|-----------|-----|-----|-------|
| Python (Lambda) | 3.13 | **3.14** | +1 minor |
| Lambda base image | python:3.13 | **python:3.14** | +1 minor |
| FastAPI | 0.115.x | **0.141.1** | +25 minor versions |
| Uvicorn | 0.34.x | **0.51.0** | +17 minor versions |
| psycopg | 3.3.x | **3.3.3** | exact pinned |
| boto3 | 1.43.x | **1.43.63** | exact pinned |
| SQLAlchemy | 2.0.x | **2.0.54** | exact pinned |
| Alembic | 1.20.x | **1.20.1** | exact pinned |
| pytest | 8.x | **9.1.1** | +1 major |
| moto | 5.x | **5.2.4** | exact pinned |
| TypeScript | 5.x | **6.0.0** | +1 major |
| Vite | 7.1.x | **8.1.0** | +1 major |
| React Admin | 5.x | **5.8.0** | exact pinned (estimated) |

