/**
 * CSVForge frontend — API type definitions.
 *
 * WHAT:
 *   Shared TypeScript types that mirror the Pydantic schemas on the backend.
 *   These are the single source of truth for the frontend API contract.
 *
 * WHY A SINGLE FILE:
 *   If API types were scattered across components, a backend contract change
 *   would require hunting down every reference. Centralising here means one
 *   file to update when the API changes, and TypeScript catches every misuse.
 *
 * KEEPING TYPES IN SYNC:
 *   These types are manually kept in sync with backend/schemas.py.
 *   In a larger project, you would generate them from an OpenAPI spec
 *   (e.g., using openapi-typescript). For this MVP, manual sync is
 *   sufficient and more educational.
 *
 * INTERVIEW CONCEPT:
 *   "Why define types for API responses?"
 *   Without types, every API response is `any` — TypeScript cannot catch
 *   mistakes like accessing `response.jobId` when the field is actually
 *   `response.job_id`. Types act as a contract between frontend and backend:
 *   if the backend changes a field name and you update the type here,
 *   TypeScript will show every component that uses the old name.
 */

// ── Job status ─────────────────────────────────────────────────────────────
// Mirrors backend/models/job.py JobStatus enum.
// The string literal union gives TypeScript exhaustiveness checking.
export type JobStatus =
  | 'pending_upload'
  | 'queued'
  | 'processing'
  | 'completed'
  | 'failed';

// ── Job types ──────────────────────────────────────────────────────────────
// Mirrors backend/schemas.py JobResponse.
// snake_case matches the JSON keys returned by the API (Python convention).
export interface Job {
  job_id: string;
  status: JobStatus;
  original_filename: string;
  user_email: string;
  file_size_bytes: number | null;
  row_count: number | null;
  column_count: number | null;
  column_names: string[] | null;
  duplicate_rows_removed: number | null;
  failure_reason: string | null;
  created_at: string | null;  // ISO-8601 string
  updated_at: string | null;  // ISO-8601 string
}

// Mirrors backend/schemas.py JobListResponse.
export interface JobListResponse {
  jobs: Job[];
  total: number;
}

// Mirrors backend/schemas.py JobDownloadResponse.
export interface JobDownloadResponse {
  job_id: string;
  download_url: string;
  filename: string;
  expires_in_seconds: number;
}

// ── Upload flow types ──────────────────────────────────────────────────────
// Mirrors backend/schemas.py PostJobRequest.
export interface CreateJobRequest {
  user_email: string;
  original_filename: string;
  file_size_bytes: number;
}

// Mirrors backend/schemas.py UploadIntentResponse.
export interface CreateJobResponse {
  job_id: string;
  upload_url: string;
  s3_key: string;
}

// Mirrors backend/schemas.py UploadCompleteResponse.
export interface UploadCompleteResponse {
  job_id: string;
  status: JobStatus;
}

// ── Admin types ────────────────────────────────────────────────────────────
// Mirrors backend/schemas.py AdminJobResponse (includes internal S3 keys).
export interface AdminJob {
  job_id: string;
  user_id: string;
  user_email: string;
  status: JobStatus;
  original_filename: string;
  original_s3_key: string | null;
  processed_s3_key: string | null;
  file_size_bytes: number | null;
  row_count: number | null;
  column_count: number | null;
  column_names: string[] | null;
  duplicate_rows_removed: number | null;
  failure_reason: string | null;
  created_at: string | null;
  updated_at: string | null;
}

// ── Error response ─────────────────────────────────────────────────────────
// FastAPI returns errors in this shape: {"detail": "message"}.
// Pydantic validation errors return {"detail": [{"loc": [...], "msg": "..."}]}
// We handle both cases in the API client.
export interface ApiError {
  detail: string | Array<{ loc: string[]; msg: string; type: string }>;
}

// ── Upload progress state ──────────────────────────────────────────────────
// Used by the upload wizard to track the multi-step upload flow.
export type UploadStep =
  | 'idle'          // waiting for file selection
  | 'selected'      // file chosen, not yet submitted
  | 'creating'      // POST /jobs in progress
  | 'uploading'     // PUT to S3 in progress
  | 'confirming'    // POST /uploads/{id}/complete in progress
  | 'done'          // job queued successfully
  | 'error';        // any step failed

export interface UploadState {
  step: UploadStep;
  progress: number;        // 0-100, used during 'uploading'
  jobId: string | null;    // set after POST /jobs succeeds
  error: string | null;    // human-readable error message
}
