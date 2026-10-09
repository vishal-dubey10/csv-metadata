# Day 4 Learning Guide — API, Authentication, and Authorization

This guide explains the complete API layer built in Day 4.
After reading it, you should be able to explain every endpoint, the Cognito authentication flow,
the ownership enforcement model, and every security decision — without looking at the code.

---

## 1. What Day 4 Built

| Component | File | What it does |
|---|---|---|
| Job schemas | `backend/schemas.py` | `JobResponse`, `JobListResponse`, `JobDownloadResponse`, `AdminJobResponse`, `PostJobRequest` |
| Auth dependency | `backend/auth.py` | `get_current_user()`, `require_admin()`, `CurrentUser` dataclass |
| Job endpoints | `backend/main.py` | `GET /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/download`, `POST /jobs` |
| Admin endpoint | `backend/main.py` | `GET /admin/jobs` with pagination + `Content-Range` header |
| Network infrastructure | `infrastructure/network.yaml` | VPC, private subnets, security groups, VPC endpoints |
| API infrastructure | `infrastructure/api.yaml` | Cognito User Pool, API Lambda, API Gateway, JWT Authoriser |
| Worker infrastructure | `infrastructure/worker.yaml` | ECR repository, Worker Lambda, SQS event source mapping |

---

## 2. Full Authentication Flow

```
Browser
  │  1. User signs in via Cognito Hosted UI or amazon-cognito-identity-js
  │     POST InitiateAuth → Cognito
  │     ← ID Token + Access Token + Refresh Token
  │
  │  2. Frontend stores tokens in memory (not localStorage — XSS risk, AD-10)
  │
  │  3. GET /jobs  →  Authorization: Bearer <access_token>
  ▼
API Gateway (HTTP API)
  │  JWT Authoriser validates the access token:
  │    - Downloads JWKS from Cognito (public keys endpoint)
  │    - Verifies signature (HMAC-SHA256 / RS256)
  │    - Verifies iss = https://cognito-idp.ap-south-1.amazonaws.com/{pool_id}
  │    - Verifies token_use = "access"
  │    - Verifies exp (not expired)
  │    - Verifies aud / client_id matches the App Client
  │  Invalid token → 401 (FastAPI never runs)
  │  Valid token → injects decoded claims into requestContext
  ▼
Mangum (ASGI adapter, 0.22.0)
  │  Translates API Gateway event (payload format 2.0) to ASGI scope
  │  The full event is at scope["aws.event"]
  ▼
FastAPI → get_current_user(request)
  │  Production path:
  │    scope["aws.event"]["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
  │    → user_id (authoritative, tamper-proof Cognito UUID)
  │  Local dev path (ENVIRONMENT=local):
  │    X-Dev-User-Id header → user_id (no JWT needed for local testing)
  ▼
Route handler receives CurrentUser(user_id=..., is_admin=...)
```

---

## 3. What FastAPI Does NOT Do

FastAPI does **not** validate the JWT signature, expiry, or issuer.
API Gateway validates the token before FastAPI ever sees the request.
FastAPI only reads already-validated claims from the request context.

This is correct and intentional (AD-19). The alternative — validating JWTs inside FastAPI — would require:
- Downloading and caching Cognito's JWKS keys on every cold start
- Writing token validation logic that API Gateway already provides for free
- Extra Lambda execution time on every request

The single responsibility principle: API Gateway handles auth; FastAPI handles business logic.

---

## 4. The Cognito Access Token Claims (AD-23)

The **access token** (not the ID token) is the API credential.

| Claim | Value | Used for |
|-------|-------|---------|
| `sub` | Cognito user UUID | `user_id` — authoritative identity for all ownership checks |
| `cognito:groups` | Space-delimited list | Admin check: `"csvforge-admins" in groups` |
| `token_use` | `"access"` | Validated by API Gateway |
| `iss` | Cognito issuer URL | Validated by API Gateway |
| `exp` | Unix timestamp | Validated by API Gateway |
| `client_id` | App Client ID | Validated by API Gateway |
| `email` | ❌ NOT present | Email is only in the ID token — never in the access token |

**Why `sub` is the identity (not `email` or `username`):**
Email can change. The Cognito `sub` is an immutable UUID assigned at account creation. If a user changes their email, all their existing jobs remain accessible because ownership is keyed on `sub`, not email.

---

## 5. The Complete API Surface

```
PUBLIC:
  GET  /health                         200 { status: "ok", environment: "..." }

USER-FACING (requires Bearer token in production / X-Dev-User-Id in local):
  POST /jobs                           201 { job_id, upload_url, s3_key }
  POST /uploads/intent                 200 { job_id, upload_url, s3_key }  ← original alias
  POST /uploads/{job_id}/complete      200 { job_id, status: "queued" }
  GET  /jobs                           200 { jobs: [...], total: N }
  GET  /jobs/{job_id}                  200 JobResponse | 404
  GET  /jobs/{job_id}/download         200 { job_id, download_url, filename, expires_in_seconds }
                                         | 404 (not found/wrong owner)
                                         | 409 (not completed yet)

ADMIN-ONLY (requires csvforge-admins Cognito group / X-Dev-Is-Admin: true):
  GET  /admin/jobs                     200 [AdminJobResponse, ...] + Content-Range header
                                         | 403 (not admin)
```

---

## 6. Authorization and Ownership Enforcement

Every user-facing endpoint that touches a specific job enforces ownership.

```python
# CORRECT — uses get_job_by_id_and_user() which filters by user_id
job = get_job_by_id_and_user(db=db, job_id=job_id, user_id=current_user.user_id)
if job is None:
    raise HTTPException(status_code=404, detail="Job not found")

# WRONG — would allow any authenticated user to read any job
job = get_job_by_id(db=db, job_id=job_id)
```

The ownership check happens at the SQL level:
```sql
SELECT * FROM jobs WHERE id = :job_id AND user_id = :user_id
```

This is a single atomic query — there is no window between "check ownership" and "return data."

### 404 vs 403 for wrong-owner requests (AD-13)

When a user requests a job that doesn't belong to them, we return **404** (not 403).

**Why 404?**  
If we returned 403, the caller would know the job *exists* — they just can't access it. This enables enumeration: an attacker could iterate UUIDs and identify which ones exist by the 403 response. With 404 for both "does not exist" and "wrong owner," the attacker learns nothing.

**The trade-off:** legitimate users occasionally get a confusing "not found" error when they paste the wrong job ID. This is acceptable — the security benefit outweighs the UX cost.

---

## 7. Presigned Download URLs

The processed CSV never passes through FastAPI on download.

```
User's browser
    │ GET /jobs/{job_id}/download (authenticated)
    ▼
FastAPI
    │ 1. Verify ownership (get_job_by_id_and_user)
    │ 2. Verify status == "completed" (409 if not)
    │ 3. generate_presigned_download_url(bucket, processed_s3_key, expiry=900s)
    ▼
Response: { download_url: "https://s3.amazonaws.com/...", filename: "data_processed.csv" }

User's browser
    │ GET <download_url>  (directly to S3, no FastAPI involved)
    ▼
S3 returns the CSV bytes
```

**Why a presigned URL (not streaming through FastAPI)?**  
- API Gateway has a 10 MiB response payload limit
- Streaming through Lambda uses execution time (you pay per millisecond) and memory
- S3 delivers files directly to the browser at full bandwidth — faster, cheaper, no Lambda overhead

**Security properties of the presigned URL:**  
- Valid for 900 seconds (15 minutes) — short enough to limit exposure
- Tied to one specific object key — cannot be used to download any other file
- The URL is a bearer token — anyone with the URL can download for 15 minutes
- The frontend should initiate the download immediately after receiving the URL

---

## 8. The Admin Endpoint and React Admin Pagination

`GET /admin/jobs` returns all jobs for all users with React Admin's `ra-data-simple-rest` pagination convention:

**Request:** `GET /admin/jobs?_start=0&_end=25&status=failed`

**Response:**
```
HTTP/1.1 200 OK
Content-Range: jobs 0-24/42
[{ job_id, user_id, user_email, status, original_s3_key, ... }, ...]
```

React Admin reads the `Content-Range` header to know the total count (42) and render page controls. Without this header (or without `expose_headers: ["Content-Range"]` in CORS), pagination breaks silently.

**Admin vs user response shapes:**

| Field | User (`JobResponse`) | Admin (`AdminJobResponse`) |
|-------|---------------------|--------------------------|
| `job_id` | ✓ | ✓ |
| `status` | ✓ | ✓ |
| `user_id` | ✗ hidden | ✓ (for debugging) |
| `original_s3_key` | ✗ hidden | ✓ (for debugging) |
| `processed_s3_key` | ✗ hidden | ✓ (for debugging) |

S3 keys are hidden from users because they expose the internal bucket structure and could assist in crafting malicious presigned URL requests.

---

## 9. CloudFormation Infrastructure (network + api + worker)

### Stack dependency order (AD-26)

```
1. csvforge-network-dev   → VPC, subnets, security groups, VPC endpoints
2. csvforge-data-dev      → S3 bucket (already existed from Day 2)
3. csvforge-queues-dev    → SQS queues (already existed from Day 2)
4. csvforge-api-dev       → Cognito, API Lambda, API Gateway
5. csvforge-worker-dev    → ECR, Worker Lambda, SQS event source mapping
```

Steps 4 and 5 can run in parallel — they don't depend on each other (AD-26).

### VPC endpoint strategy (AD-15) — No NAT Gateway

| Service | Endpoint type | Monthly cost |
|---------|--------------|-------------|
| S3 | Gateway endpoint | Free |
| SQS | Interface endpoint (PrivateLink) | ~$14/month |
| Secrets Manager | Interface endpoint (PrivateLink) | ~$14/month |
| RDS | No endpoint (same VPC, private IP) | Included in VPC |

Gateway endpoints are free and route traffic through the AWS backbone (not the internet). Interface endpoints create ENIs in your subnets with private IPs — traffic stays within the VPC. Without these endpoints, Lambda in a private subnet cannot reach S3, SQS, or Secrets Manager (there's no internet gateway, no NAT).

---

## 10. Cognito User Pool Configuration

| Setting | Value | Why |
|---------|-------|-----|
| Username attribute | Email | Users identify themselves by email |
| Access token validity | 60 minutes | Users must re-authenticate hourly (AD-10) |
| App client secret | None | Browser apps can't keep secrets (public client) |
| Allowed auth flows | USER_PASSWORD_AUTH + REFRESH_TOKEN | Custom login form; production should use SRP |
| Admin group | `csvforge-admins` | Group membership → `is_admin=True` in API |

**Why in-memory token storage (not localStorage):**  
localStorage is accessible to any JavaScript on the page — an XSS vulnerability anywhere on the domain could steal the token. In-memory storage survives only in the current tab and page load. The trade-off: users must re-authenticate after a page refresh (AD-10). Silent refresh via HttpOnly cookie is out of MVP scope.

---

## 11. Error Handling Reference

| Endpoint | Error | HTTP code | Cause |
|---|---|---|---|
| Any | No/invalid token (production) | 401 | API Gateway JWT validation failed |
| Any protected | Valid user, no admin group | 403 | `require_admin` gate |
| `GET /jobs/{id}` | Wrong owner | 404 | IDOR protection — indistinguishable from not-found |
| `GET /jobs/{id}/download` | Job not completed | 409 | Caller should poll until `status=completed` |
| `POST /uploads/complete` | File not in S3 | 404 | Browser never uploaded, or wrong bucket |
| `POST /uploads/complete` | File > 50 MiB | 422 | HeadObject size check failed |
| `POST /uploads/complete` | Already queued | 409 | Second complete call on same job |

---

## 12. Key Decisions for AD Record

**AD-13 — 404 not 403 for wrong-owner requests:**  
Prevents IDOR enumeration. Returning 403 reveals the resource exists. With 404, attackers learn nothing from a failed request.

**AD-19 — API Gateway validates JWT; FastAPI reads claims:**  
FastAPI never validates JWT signatures. API Gateway handles validation against Cognito JWKS. FastAPI reads already-validated claims from `scope["aws.event"]["requestContext"]["authorizer"]["jwt"]["claims"]`. Local dev bypass via `X-Dev-User-Id` header, gated strictly by `ENVIRONMENT=local`.

**AD-23 — Access token is the API credential; email is informational only:**  
The access token contains `sub` (user UUID) but NOT `email`. Email from the request body is stored for admin display only — never used for auth or ownership decisions. This prevents a user from forging ownership by submitting another user's email.

---

## 13. Verification Commands

```bash
# Run all Day 4 tests
PYTHONPATH=. .venv/bin/pytest tests/unit/test_auth.py tests/unit/test_job_endpoints.py -v

# Run the full unit suite (197 tests)
PYTHONPATH=. .venv/bin/pytest tests/unit/ -q

# Start the API locally
ENVIRONMENT=local DATABASE_URL=postgresql+psycopg://csvforge:password@localhost:5432/csvforge \
S3_BUCKET_NAME=csvforge-files-dev \
SQS_QUEUE_URL=https://sqs.ap-south-1.amazonaws.com/123456789012/csvforge-jobs-dev \
.venv/bin/uvicorn backend.main:app --reload

# Smoke-test the new endpoints (requires the API running locally)
# List jobs (empty for new user)
curl http://localhost:8000/jobs -H "X-Dev-User-Id: user-1"

# List all jobs as admin
curl http://localhost:8000/admin/jobs -H "X-Dev-User-Id: admin-1" -H "X-Dev-Is-Admin: true"

# Non-admin gets 403
curl -i http://localhost:8000/admin/jobs -H "X-Dev-User-Id: user-1"
```

---

## 14. Interview Questions

**Q: What is the difference between authentication and authorisation?**
A: Authentication (authn) = verifying *identity*: "who are you?" Cognito issues a JWT; API Gateway validates it; FastAPI reads the `sub` claim as `user_id`. Authorisation (authz) = verifying *permission*: "can you do this?" `get_job_by_id_and_user` checks that `user_id` matches `job.user_id`. These are sequential: auth first, then authz with the verified identity.

**Q: What is an IDOR vulnerability and how does CSVForge prevent it?**
A: IDOR (Insecure Direct Object Reference) occurs when an API returns a resource to any authenticated caller who knows its ID, without verifying they own it. If `GET /jobs/abc-123` returned the job to whoever asked, any authenticated user who guessed or found the UUID could read another user's data. CSVForge prevents this by always calling `get_job_by_id_and_user(db, job_id, user_id=current_user.user_id)` — ownership is verified at the SQL level in a single atomic query.

**Q: Why does the admin endpoint return 403 but user job endpoints return 404 for wrong access?**
A: The distinction is about what the caller already knows. `/admin/jobs` is a publicly known endpoint path — any authenticated user can attempt it. 403 correctly says "I know who you are; you lack permission." For `/jobs/{job_id}`, returning 403 for a wrong-owner request would reveal that the specific job *exists*, enabling enumeration attacks. 404 is indistinguishable from "this ID doesn't exist" — the attacker learns nothing.

**Q: How does API Gateway JWT validation work at the cryptographic level?**
A: Cognito publishes its public signing keys at a JWKS (JSON Web Key Set) endpoint. API Gateway downloads and caches these keys. When a request arrives with a Bearer token, API Gateway decodes the JWT header (which identifies which key was used), retrieves that public key from the cache, and verifies the signature. If the token was tampered with after signing, the signature check fails. The private key never leaves Cognito. FastAPI receives only tokens that passed this check — it never sees invalid tokens.

**Q: Why doesn't CSVForge validate JWTs inside FastAPI?**
A: API Gateway already validates tokens before FastAPI runs. Doing it again in FastAPI would be redundant and would require downloading/caching Cognito's JWKS on every Lambda cold start. The API Gateway JWT Authoriser handles JWKS caching and rotation automatically. This is the single responsibility principle: API Gateway handles auth; FastAPI handles application logic.

**Q: What is the Content-Range header and why does React Admin need it?**
A: React Admin's `ra-data-simple-rest` data provider reads `Content-Range: jobs 0-24/42` to know there are 42 total records when displaying page 1 (rows 0-24). Without this header, React Admin can't render pagination controls or show "42 results." The header must also be in `expose_headers` in the CORS configuration — by default, browsers only expose a small set of "safe" headers to JavaScript from cross-origin responses, and `Content-Range` is not in that set.

---

## 15. What Day 4 Defers

- Cognito Hosted UI / custom login form (Day 7 frontend)
- Real JWT validation against an actual Cognito User Pool (AWS verification phase)
- Full CloudFormation deployment to AWS (Day 6)
- Frontend job listing and status polling (Day 7)
- RDS provisioning (Day 5/6 infrastructure)
