"""
CSVForge — FastAPI application entry point.

This module creates the FastAPI app instance, registers middleware,
and defines the health check endpoint.

WSGI vs ASGI:
    WSGI (Web Server Gateway Interface) is the traditional Python web protocol —
    synchronous, one request at a time per thread. Django and Flask use WSGI.

    ASGI (Asynchronous Server Gateway Interface) is the modern async equivalent.
    FastAPI is an ASGI application — it can handle many concurrent connections
    without spinning up a new thread for each one.

UVICORN:
    Uvicorn is an ASGI server (analogous to gunicorn for WSGI). It listens for
    incoming HTTP connections and speaks the ASGI protocol to FastAPI.
    Used for local development and container deployments:
        uvicorn backend.main:app --reload

MANGUM:
    Mangum wraps a FastAPI/ASGI app in an AWS Lambda handler. When Lambda
    receives an API Gateway event (a JSON dict), Mangum translates it into an
    ASGI request, passes it to FastAPI, and packages the ASGI response back
    into the Lambda response format. Same FastAPI app — different transport layer.
        from mangum import Mangum
        handler = Mangum(app)

FULL REQUEST LIFECYCLE:

    Browser
       │ HTTP Request
    Uvicorn (local) / API Gateway + Mangum (Lambda)
       │ ASGI
    FastAPI
       │
    CORS Middleware  ← checks Origin header, adds CORS response headers
       │
    Route Matching  ← @app.get("/health") etc.
       │
    Dependency Resolution  ← Depends(get_db) creates a Session
       │
    Route Handler  ← your function runs here, receives db: Session
       │
    Repository Functions  ← job_repository.py
       │
    SQLAlchemy ORM
       │
    PostgreSQL
       │ response
    Route Handler returns Python dict
       │
    FastAPI serializes to JSON (Pydantic)
       │
    CORS Middleware adds headers
       │
    Uvicorn / Mangum
       │ HTTP Response
    Browser
"""

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ── App instance ──────────────────────────────────────────────────────────────
# FastAPI(title=..., version=..., description=...) populates the OpenAPI spec
# that FastAPI auto-generates at startup. Navigate to http://localhost:8000/docs
# to see the Swagger UI generated from these fields.
#
# Every @app.get() decorator and Python type annotation further populates this
# spec — FastAPI reads each function's docstring, parameter types, and return
# type to generate documentation automatically. No separate doc-writing step needed.
app = FastAPI(
    title="CSVForge API",
    version="0.1.0",
    description="CSV upload, processing, and download service.",
)

# ── CORS middleware ───────────────────────────────────────────────────────────
# Browsers enforce the Same-Origin Policy: a page at origin A cannot read a
# response from origin B unless origin B explicitly allows it via
# Access-Control-Allow-Origin headers.
#
# The React frontend (Vite dev server) runs at http://localhost:5173.
# Without this middleware, every API call from the frontend would be
# blocked by the browser with a CORS error — even though the server
# returned a 200. The error is enforced by the browser, not the server.
#
# allow_credentials=True:
#   By default CORS blocks credentials (cookies and Authorization headers)
#   from cross-origin requests. Setting this to True enables the browser
#   to send cookies and Authorization headers to this API from a different
#   origin. Required when the React frontend is served from
#   http://localhost:5173 and needs to call http://localhost:8000 with
#   an auth token in the Authorization header.
#
# expose_headers=["Content-Range"]:
#   By default, the browser's CORS filter only exposes a small set of
#   "safe" response headers to JavaScript (Content-Type, Cache-Control, etc.).
#   Content-Range is NOT in that default safe list.
#
#   React Admin (the frontend framework) reads the Content-Range header to
#   know the total count of items for pagination. The API responds with:
#       Content-Range: jobs 0-9/42
#   React Admin reads the `/42` part to render page controls ("42 total results").
#   Without expose_headers, the browser's CORS filter hides this header from
#   React Admin's JavaScript, and pagination breaks silently.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev server origin
    allow_credentials=True,   # allow Authorization header + cookies cross-origin
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Range"],  # needed for React Admin list pagination
)

# ── Health endpoint ───────────────────────────────────────────────────────────
# HOW FASTAPI REGISTERS ROUTES:
# @app.get("/health") registers a route: "when the app receives a GET request
# to /health, call this function". FastAPI reads the function's signature,
# docstring, and return type at startup to build the OpenAPI spec.
#
# The return type annotation (-> dict) tells FastAPI and Pydantic how to
# serialize the response — Pydantic converts Python objects to JSON-serializable
# dicts. If the function raises an HTTPException, FastAPI catches it and returns
# the appropriate HTTP error response automatically.
@app.get("/health")
def health_check() -> dict:
    """
    Health check endpoint.

    Returns the service status and current runtime environment.
    Used by:
    - Docker Compose healthchecks (future)
    - Load balancer health probes
    - Quick manual verification that the API is running

    The `environment` field lets you confirm you're hitting the right
    deployment (local vs production) without checking env vars directly.

    Returns 200 always — if this endpoint itself fails, the process is broken.
    """
    return {
        "status": "ok",
        "environment": os.getenv("ENVIRONMENT", "production"),
    }
