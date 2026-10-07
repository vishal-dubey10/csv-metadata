"""
Unit tests for the /health endpoint.

These tests use FastAPI's TestClient — no real HTTP server, no database.
The app is run in-process via the ASGI interface.

HOW TestClient WORKS:
`TestClient` (from Starlette/FastAPI) makes HTTP requests to the FastAPI app
WITHOUT starting a real HTTP server. Requests go through Python's call stack
directly — no network, no port binding. This means tests run fast and don't
need OS-level cleanup. The `response` object behaves exactly like a
`requests.Response` — it has `.status_code`, `.json()`, `.headers`, etc.

`response.json()` parses the response body as JSON and returns a Python
dict/list. No network deserialization overhead — the response was never
actually bytes-over-a-wire; it's already a Python object internally. The
`.json()` call is still used for consistency with real HTTP client code.

FIXTURE INJECTION:
The `client` fixture is defined in `tests/conftest.py`. pytest automatically
discovers fixtures from conftest.py files in parent directories. When a test
declares `client` as a parameter, pytest finds and calls the `client` fixture
from conftest.py, which creates a `TestClient(app)` and yields it. This is
pytest's dependency injection system for test fixtures.

What these tests verify:
- GET /health returns HTTP 200
- The response body contains status and environment fields
- The environment field reflects the ENVIRONMENT env variable
- The endpoint is reachable (no import errors, no startup crash)
"""

import os


def test_health_returns_200(client):
    """GET /health returns HTTP 200."""
    response = client.get("/health")
    assert response.status_code == 200


def test_health_returns_status_ok(client):
    """The response body contains {"status": "ok"}."""
    response = client.get("/health")
    body = response.json()
    assert body["status"] == "ok"


def test_health_returns_environment(client):
    """
    The response body contains an `environment` field.

    In tests, ENVIRONMENT=local (set in conftest.py).
    The default value ("production") is never returned in a correctly
    configured environment — this test confirms the env var is read.
    """
    response = client.get("/health")
    body = response.json()
    assert "environment" in body
    assert body["environment"] == os.getenv("ENVIRONMENT", "production")


def test_health_response_shape(client):
    """
    The response body has exactly the expected keys.

    Prevents silent addition of unexpected fields (e.g., secrets, internal state)
    to the health endpoint response.
    """
    response = client.get("/health")
    body = response.json()
    assert set(body.keys()) == {"status", "environment"}


def test_health_content_type_is_json(client):
    """The health endpoint returns JSON, not plain text."""
    response = client.get("/health")
    assert "application/json" in response.headers["content-type"]
