"""
CSVForge — authentication and identity dependency.

WHAT:
    This module provides `get_current_user()`, a FastAPI dependency that
    extracts and returns the authenticated user's identity for every protected
    route. It also provides `require_admin()` for admin-only endpoints.

WHY THIS MODULE EXISTS:
    Authentication logic should live in exactly ONE place. If every route
    handler extracted the user_id itself, the extraction code would be
    duplicated 10+ times, and a single bug (e.g., wrong claim key) would
    require fixing every route. Centralising here means:
    - One fix propagates to every route.
    - Tests mock one dependency, not every route handler.
    - The local-dev bypass (AD-19) is applied consistently.

ARCHITECTURE — how auth flows in production:

    Browser
        │ GET /jobs  (Authorization: Bearer <access_token>)
        ▼
    API Gateway (HTTP API)
        │ JWT Authoriser validates token against Cognito JWKS:
        │   - Verifies signature against Cognito public keys
        │   - Verifies iss == https://cognito-idp.<region>.amazonaws.com/<pool_id>
        │   - Verifies token_use == "access"
        │   - Verifies expiry (exp claim)
        │   - Verifies aud / client_id
        │ On failure → 401 before FastAPI runs
        │ On success → injects decoded claims into requestContext
        ▼
    Mangum (ASGI adapter)
        │ Translates API Gateway event (format 2.0) to ASGI scope
        │ The full API Gateway event is at: scope["aws.event"]
        ▼
    FastAPI
        │ Depends(get_current_user) is resolved for every protected route
        │
        ▼
    get_current_user(request: Request) → CurrentUser
        │
        │ Production path:
        │   scope["aws.event"]["requestContext"]["authorizer"]["jwt"]["claims"]
        │   → sub claim = user_id
        │   → cognito:groups claim = group memberships
        │
        │ Local dev path (ENVIRONMENT=local):
        │   X-Dev-User-Id header → user_id
        │   X-Dev-Is-Admin header → is_admin flag
        ▼
    Route handler receives CurrentUser(user_id=..., is_admin=...)

ARCHITECTURE — what FastAPI DOES NOT do:
    FastAPI does NOT validate the JWT signature, expiry, or issuer.
    API Gateway validates the token before FastAPI sees the request.
    FastAPI only reads the already-validated claims from the request context.
    This is correct and intentional — AD-19.

    For local development (no API Gateway), there is no JWT validation at all.
    The ENVIRONMENT=local bypass uses trusted headers (X-Dev-User-Id).
    This bypass NEVER runs in production (where ENVIRONMENT != "local").

COGNITO ACCESS TOKEN CLAIMS (AD-23):
    The access token (not the ID token) is the API credential.
    Relevant claims injected by API Gateway:
        sub               → unique user UUID (authoritative identity)
        cognito:groups    → list of Cognito group memberships
        token_use         → "access" (always for access tokens)
        username          → Cognito username (not used for auth here)
        client_id         → Cognito app client ID
        iss               → issuer URL (validated by API Gateway)
        exp               → expiry (validated by API Gateway)
    NOT present in the access token: email, email_verified (AD-23).

OWNERSHIP ENFORCEMENT:
    Every user-facing endpoint that accesses a job must enforce:
        job.user_id == current_user.user_id
    This prevents IDOR (Insecure Direct Object Reference) attacks where
    user A reads user B's job by guessing the job_id UUID. A UUID is NOT
    an authorization mechanism (it is security through obscurity).
    See AD-23 and section 10 of the task spec.

SECURITY PRINCIPLE:
    Never trust a client-supplied user ID. The user_id in CurrentUser comes
    from a validated JWT claim (production) or a test header (local dev).
    It is NEVER read from the request body or a query parameter.

INTERVIEW CONCEPT:
    "What is the difference between authentication and authorisation?"
    Authentication (authn): verifying IDENTITY — "who are you?"
        In CSVForge: API Gateway validates the JWT, confirming the caller is
        a real, non-expired Cognito user.
    Authorisation (authz): verifying PERMISSION — "can you do this?"
        In CSVForge: get_job_by_id_and_user() checks that the authenticated
        user owns the job they're requesting.
    These are two separate steps. Authentication happens first; authorisation
    uses the identity from authentication.
"""

import os
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request


# ── Identity dataclass ────────────────────────────────────────────────────────

@dataclass
class CurrentUser:
    """
    The authenticated caller's identity, extracted from the JWT claims.

    WHAT:
        A lightweight data container holding the two pieces of identity
        information that routes need: who the user is (user_id) and whether
        they are an admin (is_admin).

    WHY A DATACLASS (not a dict):
        Type annotations give the route handler IDE autocomplete and static
        type checking. current_user.user_id is clearly a str; current_user.is_admin
        is clearly a bool. A dict["user_id"] provides no such guarantees.

    user_id:
        The Cognito `sub` claim — an immutable UUID assigned at account creation.
        This is the authoritative, tamper-proof user identity used for:
        - All ownership checks (job.user_id == current_user.user_id)
        - Constructing S3 key prefixes (uploads/{user_id}/...)
        - Filtering job listings (WHERE user_id = current_user.user_id)
        NEVER use email or username for these purposes (AD-23).

    is_admin:
        True if the user is in the 'csvforge-admins' Cognito group.
        Used to gate access to GET /admin/jobs.
        Derived from the `cognito:groups` claim.

    email:
        Optional email for logging/display only. Sourced from the request
        body (user-supplied, not from JWT) or X-Dev-User-Email header.
        NEVER used for authorization decisions (AD-23).
    """

    user_id: str
    is_admin: bool = False
    email: Optional[str] = None


# ── FastAPI dependency ────────────────────────────────────────────────────────

async def get_current_user(
    request: Request,
    x_dev_user_id: str = Header(default=""),
    x_dev_user_email: str = Header(default=""),
    x_dev_is_admin: str = Header(default="false"),
) -> CurrentUser:
    """
    FastAPI dependency: extract the authenticated user identity.

    WHAT:
        Reads the caller's identity from the request and returns a CurrentUser.
        Used as `Depends(get_current_user)` in every protected route handler.

    TWO PATHS:

    LOCAL DEV PATH (ENVIRONMENT=local):
        No API Gateway, no JWT validation. The caller passes identity via
        X-Dev-User-Id and X-Dev-User-Email headers. X-Dev-Is-Admin controls
        admin access. This path is gated strictly by ENVIRONMENT=local.

        Usage:
            curl http://localhost:8000/jobs \
              -H "X-Dev-User-Id: user-1" \
              -H "X-Dev-Is-Admin: true"

        SECURITY: This bypass NEVER runs in production (ENVIRONMENT != "local").
        In production, ENVIRONMENT is always set to a non-"local" value by
        the Lambda environment variables in CloudFormation.

    PRODUCTION PATH (ENVIRONMENT != local):
        API Gateway has already validated the JWT. Mangum placed the full
        API Gateway event at scope["aws.event"]. The JWT claims are nested at:
            scope["aws.event"]["requestContext"]["authorizer"]["jwt"]["claims"]

        Claims read:
            sub               → user_id (authoritative, immutable UUID)
            cognito:groups    → determines is_admin flag

        If the claims are absent (bug in API Gateway configuration, direct
        Lambda invoke without API Gateway), the function raises 401.
        FastAPI NEVER validates the JWT signature — that is API Gateway's job.

    RETURNS:
        CurrentUser with user_id, is_admin, and optional email.

    RAISES:
        HTTPException(401) if in production and JWT claims are not present.

    INTERVIEW CONCEPT:
        "Why not validate the JWT in FastAPI?"
        API Gateway validates the JWT before the Lambda function is invoked.
        If the token is invalid or expired, API Gateway returns 401 and
        FastAPI never runs. Validating again in FastAPI would be redundant
        and would require downloading and caching Cognito's JWKS keys in
        every Lambda invocation. The API Gateway JWT authoriser handles this
        more efficiently at the infrastructure layer.
    """
    environment = os.getenv("ENVIRONMENT", "production")

    if environment == "local":
        # ── Local dev bypass (AD-19) ──────────────────────────────────────────
        # Trust X-Dev-User-Id from the caller. No JWT validation.
        # Default to "dev-user-001" if the header is not provided, matching
        # the defaults set in the upload endpoints.
        user_id = x_dev_user_id if x_dev_user_id else "dev-user-001"
        email = x_dev_user_email if x_dev_user_email else None
        is_admin = x_dev_is_admin.lower() in ("true", "1", "yes")
        return CurrentUser(user_id=user_id, is_admin=is_admin, email=email)

    # ── Production path: read API Gateway JWT claims ──────────────────────────
    # Mangum (0.22.0, payload format 2.0) places the API Gateway event at
    # request.scope["aws.event"]. The JWT authoriser claims are nested inside.
    #
    # Full path to claims:
    #   request.scope
    #     └── "aws.event"                   (full API Gateway event)
    #           └── "requestContext"        (request metadata)
    #                 └── "authorizer"      (API Gateway authoriser output)
    #                       └── "jwt"       (JWT authoriser specific)
    #                             └── "claims"  (decoded JWT payload)
    #
    # This structure is defined by API Gateway HTTP API payload format 2.0.
    # See: https://docs.aws.amazon.com/apigateway/latest/developerguide/
    #      http-api-develop-integrations-lambda.html
    try:
        aws_event = request.scope.get("aws.event", {})
        claims = (
            aws_event
            .get("requestContext", {})
            .get("authorizer", {})
            .get("jwt", {})
            .get("claims", {})
        )
    except (AttributeError, TypeError):
        claims = {}

    if not claims:
        # If we reach this point in production without claims, something is
        # misconfigured: either the API Gateway JWT authoriser is not attached,
        # the Lambda function was invoked directly without going through API
        # Gateway, or the Mangum version changed the scope key.
        # Return 401 — do not leak internal details.
        raise HTTPException(
            status_code=401,
            detail="Authentication required. No valid credentials provided.",
        )

    # sub is the Cognito user UUID — the authoritative, immutable user identity.
    user_id = claims.get("sub", "")
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Missing sub claim.",
        )

    # cognito:groups is a space-delimited string in the JWT claims dict
    # (as injected by API Gateway) when the user belongs to groups.
    # It may be absent entirely if the user has no groups.
    # Format: "csvforge-admins other-group" or absent.
    groups_raw = claims.get("cognito:groups", "")
    if isinstance(groups_raw, str):
        groups = groups_raw.split() if groups_raw else []
    elif isinstance(groups_raw, list):
        groups = groups_raw
    else:
        groups = []

    is_admin = "csvforge-admins" in groups

    return CurrentUser(user_id=user_id, is_admin=is_admin)


async def require_admin(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """
    FastAPI dependency: require the caller to be in the csvforge-admins group.

    WHAT:
        A thin wrapper around get_current_user() that additionally checks
        is_admin. Used as `Depends(require_admin)` on admin endpoints.

    WHY A SEPARATE DEPENDENCY (not inline in the route):
        Consistent, reusable admin gate. If the admin group name changes,
        there is one place to update. Route handlers stay readable:
            def list_all_jobs(admin: CurrentUser = Depends(require_admin))

    RAISES:
        HTTPException(403) if the caller is not in the csvforge-admins group.
        403 Forbidden (not 401 Unauthorized) because the user IS authenticated
        — they just lack the required permission.

    INTERVIEW CONCEPT:
        "403 vs 401 — when to use which?"
        401 Unauthorized: the caller has not authenticated (no token, expired
            token, invalid signature). They need to authenticate first.
        403 Forbidden: the caller IS authenticated but lacks permission.
            "I know who you are; you just can't do this."
        Returning 401 for "not an admin" would be confusing — the user is
        already logged in. 403 correctly communicates "access denied."
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Admin access required.",
        )
    return current_user
