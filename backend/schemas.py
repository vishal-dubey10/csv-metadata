"""
CSVForge — Pydantic request/response schemas for upload endpoints.

WHAT:
    This module defines three Pydantic v2 models used by the upload endpoints:
      - UploadIntentRequest:  the JSON body the client sends to POST /uploads/intent
      - UploadIntentResponse: the JSON body FastAPI returns from POST /uploads/intent
      - UploadCompleteResponse: the JSON body returned from POST /uploads/{job_id}/complete

WHY THIS FILE EXISTS AS A SEPARATE MODULE:
    FastAPI routes can accept and return plain dicts, but Pydantic models are
    better in every measurable way for an API contract:

    1. VALIDATION AT THE BOUNDARY:
       Pydantic validates incoming JSON against the schema BEFORE the route
       handler runs. If `file_size_bytes` is a string instead of an integer,
       Pydantic returns a 422 Unprocessable Entity with a clear error message
       — the route handler never executes. Plain dicts give you no automatic
       validation; you'd have to write it yourself (and would forget edge cases).

    2. OPENAPI DOCUMENTATION:
       FastAPI reads Pydantic models to generate the OpenAPI spec at /docs.
       The Swagger UI shows the exact shape of every request and response body.
       Using plain dicts produces only "object" in the OpenAPI schema — no
       field names, no types, no documentation.

    3. SERIALIZATION:
       Pydantic handles JSON serialization/deserialization. Python int → JSON
       number, Python str → JSON string, Python None → JSON null. No manual
       json.dumps() or type casting needed.

    4. TYPE SAFETY:
       Route handler function signatures can annotate parameters with these
       models. mypy/pyright knows that `body.file_size_bytes` is an int — not
       Any — so type errors are caught before runtime.

    5. SEPARATION OF CONCERNS:
       Keeping schemas in their own module prevents main.py from growing into
       a 500-line file that mixes routing, validation, and business logic.
       Schemas are pure data shape definitions — they belong in one place.

ARCHITECTURE:
    Browser
        │ POST /uploads/intent
        │ body: {user_email, original_filename, file_size_bytes}
        ▼
    FastAPI route (main.py)
        │ body: UploadIntentRequest (Pydantic validates JSON → Python object)
        │
        │ calls create_job(), generate_presigned_upload_url()
        │
        │ returns: UploadIntentResponse (Pydantic serializes Python → JSON)
        ▼
    Browser
        │ receives: {job_id, upload_url, s3_key}
        │
        │ PUT file bytes directly to S3 using upload_url
        │
        │ POST /uploads/{job_id}/complete
        ▼
    FastAPI route (main.py)
        │ returns: UploadCompleteResponse {job_id, status}
        ▼
    Browser

IMPORTANT CONCEPTS:
    Pydantic v2 BaseModel:
        Pydantic v2 (the version used here) is a complete rewrite of Pydantic v1.
        Models are defined by subclassing BaseModel. Field types are Python type
        annotations. Validation runs automatically on instantiation.
        FastAPI 0.141.x is fully compatible with Pydantic v2.
    Model vs schema:
        "Schema" here means the shape of an API request or response body —
        not a database schema. The naming convention (Request/Response suffix)
        makes the direction of data flow explicit.
    file_size_bytes as client-supplied metadata:
        UploadIntentRequest includes file_size_bytes as a hint to the backend
        (for logging, display). It is NOT trusted for enforcement — the actual
        size is verified via S3 HeadObject in the complete endpoint (AD-27).
        Client-supplied values for security boundaries are always untrusted.

Interview concept:
    "Why validate at the API boundary rather than in business logic?"
    Validating at the boundary (the HTTP layer, via Pydantic) means invalid
    data is rejected before it can contaminate business logic or reach the
    database. This is the "fail fast" principle: catch errors as early as
    possible, close to the source, with a clear error message. The further
    invalid data travels into the system before it's caught, the harder it is
    to trace the original problem.
"""

from pydantic import BaseModel


class UploadIntentRequest(BaseModel):
    """
    Request body for POST /uploads/intent.

    WHAT:
        The three pieces of information the client sends when requesting a
        presigned upload URL. FastAPI uses this model to validate the incoming
        JSON and inject it into the route handler as a typed Python object.

    WHY THESE THREE FIELDS:
        user_email:
            Stored in the jobs table as informational context for admin views.
            NOT used for authorization — ownership is always based on user_id
            from the JWT/header. Per AD-23, email is provided by the frontend
            from the Cognito ID token at upload time.

        original_filename:
            The filename as reported by the browser (e.g., "my_data.csv").
            Stored for display — shown to the user in their job list so they
            can identify which upload each job corresponds to.
            NEVER used to construct file paths (security principle: never trust
            client-supplied filenames for file system operations).

        file_size_bytes:
            The client-reported file size. Used for display and logging only.
            The authoritative size is retrieved from S3 HeadObject in the
            complete endpoint (AD-27). Client-reported size is inherently
            untrusted for enforcement.

    Pydantic v2 validation applied automatically:
        - user_email must be a string (not None, not int)
        - original_filename must be a string
        - file_size_bytes must be an integer
        Invalid JSON → Pydantic raises ValidationError → FastAPI returns 422
    """

    user_email: str
    original_filename: str
    file_size_bytes: int


class UploadIntentResponse(BaseModel):
    """
    Response body for POST /uploads/intent.

    WHAT:
        The three values the client needs to proceed with the upload:
        a job identifier, a presigned S3 URL, and the S3 key.

    WHY THESE THREE FIELDS:
        job_id:
            The UUID of the newly created Job record. The client must save this
            to call POST /uploads/{job_id}/complete after uploading to S3.
            Also useful for the user to track their job's progress.

        upload_url:
            The presigned S3 PUT URL. The browser PUT-requests this URL with
            the file bytes as the request body. The URL:
              - Is valid for 15 minutes (900 seconds).
              - Authorizes exactly one PUT to exactly one S3 key.
              - Carries an embedded content-length-range condition (≤ 50 MiB).
              - Does NOT require AWS credentials from the browser.
            See AD-02 and s3_client.generate_presigned_upload_url() for details.

        s3_key:
            The S3 object key where the file will be stored after the PUT.
            Format: uploads/{user_id}/{job_id}/original.csv
            The client receives this so it can include it in the complete call
            (though the complete endpoint looks it up from the database).
            Also useful for debugging: the client can tell support exactly
            where their file lives in S3.
    """

    job_id: str
    upload_url: str
    s3_key: str


class UploadCompleteResponse(BaseModel):
    """
    Response body for POST /uploads/{job_id}/complete.

    WHAT:
        Confirms that the upload has been verified and the job has been queued
        for processing. Contains the job_id for correlation and the new status.

    WHY THESE TWO FIELDS:
        job_id:
            The UUID of the job that was just queued. Returned for client
            convenience — the client already knows the job_id from the intent
            response, but including it here makes the complete response
            self-contained and easier to log/debug.

        status:
            The new job status after a successful complete call. This will
            always be 'queued' on a 200 response (the job is now in the queue
            waiting for the Lambda worker). The client can display this to the
            user: "Your file has been queued for processing."
            If the job was not successfully queued (wrong status, file too large,
            etc.), the endpoint returns a 4xx error instead of this response.
    """

    job_id: str
    status: str
