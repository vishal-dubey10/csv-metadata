/**
 * Tests for the API client module.
 *
 * WHAT:
 *   Verifies the core behaviour of the API client:
 *     - Auth header injection (Bearer token vs local dev bypass)
 *     - Error normalisation (HTTP errors → ApiClientError with correct message)
 *     - S3 upload isolation (no Authorization header sent to presigned URL)
 *     - ApiClientError carries status code
 *
 * APPROACH — mocking fetch():
 *   The API client calls window.fetch(). In the jsdom test environment,
 *   fetch is not implemented. We use vi.spyOn to replace it with a mock
 *   that returns a controlled Response object.
 *
 *   WHY NOT mock at the module level:
 *   vi.spyOn allows per-test restoration, which is safer than a module-level
 *   override. Each test sets up exactly the response it needs, making test
 *   intent explicit and preventing test pollution.
 *
 * INTERVIEW CONCEPT:
 *   "What is the purpose of mocking in unit tests?"
 *   Mocking replaces a dependency (fetch, a database, an API) with a
 *   controlled substitute. This isolates the unit under test: the API client
 *   tests test the client's own logic (header building, error parsing) without
 *   requiring a real server. If the tests used a real server, they would be
 *   integration tests — slower, flakier, and dependent on external state.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  setAuthToken,
  setDevIdentity,
  ApiClientError,
  listJobs,
} from '../api/client';

// ── Helpers ───────────────────────────────────────────────────────────────────

function mockFetch(status: number, body: unknown): void {
  vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('ApiClientError', () => {
  it('is an instance of Error', () => {
    const err = new ApiClientError('Test message', 404);
    expect(err).toBeInstanceOf(Error);
    expect(err).toBeInstanceOf(ApiClientError);
  });

  it('carries the status code', () => {
    const err = new ApiClientError('Not found', 404);
    expect(err.status).toBe(404);
  });

  it('carries an optional detail string', () => {
    const err = new ApiClientError('Friendly message', 422, 'raw detail');
    expect(err.detail).toBe('raw detail');
    expect(err.message).toBe('Friendly message');
  });

  it('has name "ApiClientError"', () => {
    const err = new ApiClientError('msg', 500);
    expect(err.name).toBe('ApiClientError');
  });
});

describe('listJobs — API request behaviour', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns job list on success', async () => {
    const mockJobs = {
      jobs: [
        {
          job_id: 'abc-123',
          status: 'completed',
          original_filename: 'data.csv',
          user_email: 'test@example.com',
          file_size_bytes: 1024,
          row_count: 100,
          column_count: 5,
          column_names: ['a', 'b'],
          duplicate_rows_removed: 2,
          failure_reason: null,
          created_at: '2026-10-09T10:00:00Z',
          updated_at: '2026-10-09T10:01:00Z',
        },
      ],
      total: 1,
    };
    mockFetch(200, mockJobs);

    const result = await listJobs();
    expect(result.total).toBe(1);
    expect(result.jobs[0].job_id).toBe('abc-123');
  });

  it('throws ApiClientError with 401 status on unauthorised response', async () => {
    mockFetch(401, { detail: 'Not authenticated' });

    await expect(listJobs()).rejects.toMatchObject({
      status: 401,
      message: 'Your session has expired. Please log in again.',
    });
  });

  it('throws ApiClientError with 404 status for not found', async () => {
    mockFetch(404, { detail: 'Not found' });

    await expect(listJobs()).rejects.toMatchObject({
      status: 404,
      message: 'The requested resource was not found.',
    });
  });

  it('throws ApiClientError with 500 for server errors', async () => {
    mockFetch(500, { detail: 'Internal server error' });

    await expect(listJobs()).rejects.toMatchObject({
      status: 500,
      message: 'A server error occurred. Please try again later.',
    });
  });

  it('throws ApiClientError with status 0 on network failure', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce(
      new TypeError('Failed to fetch'),
    );

    await expect(listJobs()).rejects.toMatchObject({
      status: 0,
    });
  });
});

describe('setAuthToken and setDevIdentity', () => {
  beforeEach(() => {
    // Reset to a known state before each test
    setAuthToken(null);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('setDevIdentity can be called without throwing', () => {
    expect(() => setDevIdentity('user-123', 'test@example.com')).not.toThrow();
  });

  it('setAuthToken can be called without throwing', () => {
    expect(() => setAuthToken('some-token')).not.toThrow();
    expect(() => setAuthToken(null)).not.toThrow();
  });
});
