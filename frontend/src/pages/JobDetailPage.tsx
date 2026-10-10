/**
 * CSVForge — Job detail page.
 *
 * WHAT:
 *   Shows the full state of a single job: status, CSV metadata, processing
 *   results, and a download button when the job is completed. Polls for
 *   status changes while the job is active (queued/processing).
 *
 * URL: /jobs/:jobId
 *   useParams() extracts the jobId from the URL path. React Router v7
 *   guarantees the param matches the route pattern defined in App.tsx:
 *     { path: 'jobs/:jobId', element: <JobDetailPage /> }
 *
 * DOWNLOAD FLOW:
 *   When the user clicks "Download processed CSV":
 *   1. GET /jobs/{jobId}/download → {download_url, filename, expires_in_seconds}
 *   2. Create an invisible <a> with the download attribute and the presigned URL.
 *   3. Programmatically click it to trigger the browser's native download.
 *   4. The file is served directly from S3 — no Lambda involved.
 *
 * WHY AN ANCHOR CLICK (not window.open):
 *   window.open(url) opens in a new tab, which the user must close after
 *   the download starts — poor UX. An anchor with the download attribute
 *   triggers the browser's download manager directly: no new tab, no extra
 *   close step. The filename in the anchor's download attribute suggests
 *   the Save As name (derived from the original filename with _processed suffix).
 *
 * POLLING:
 *   Same strategy as JobListPage — polls every 5s while status is non-terminal.
 *   Stops automatically when status becomes completed or failed.
 *   Cleans up the interval on unmount.
 *
 * MUI v9 API CHANGES:
 *   - Typography: fontWeight, display, textAlign → must be in sx.
 *   - Stack: flexWrap → must be in sx. The Stack component lost HTML layout
 *     shorthand props in v9 for the same reason as Typography — sx is the
 *     single authoritative channel for CSS properties.
 *
 * INTERVIEW CONCEPT:
 *   "Why use a presigned URL instead of streaming the file through Lambda?"
 *   Lambda has a 6 MB synchronous response payload limit for API Gateway
 *   HTTP API (and Lambda itself has a 10 MB response limit for sync invocations).
 *   A 50 MB CSV would exceed both limits. A presigned URL offloads the delivery
 *   to S3's HTTP infrastructure — no Lambda execution time, no API Gateway
 *   limits, and S3 handles concurrent downloads efficiently.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  Paper,
  Skeleton,
  Stack,
  Tooltip,
  Typography,
} from '@mui/material';
import ArrowBackIcon from '@mui/icons-material/ArrowBack';
import DownloadIcon from '@mui/icons-material/Download';
import RefreshIcon from '@mui/icons-material/Refresh';
import { Link as RouterLink, useParams } from 'react-router-dom';
import { getJob, getDownloadUrl, ApiClientError } from '../api/client';
import JobStatusChip from '../components/JobStatusChip';
import type { Job } from '../types/api';

// ── Constants ─────────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 5000;
const TERMINAL_STATUSES = new Set(['completed', 'failed']);

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function JobDetailPage(): React.ReactElement {
  const { jobId } = useParams<{ jobId: string }>();
  const [job, setJob] = useState<Job | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [isDownloading, setIsDownloading] = useState(false);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Fetch job ───────────────────────────────────────────────────────────────

  const fetchJob = useCallback(
    async (silent = false): Promise<void> => {
      if (!jobId) return;
      if (!silent) setIsLoading(true);
      try {
        const fetched = await getJob(jobId);
        setJob(fetched);
        setFetchError(null);
        // Stop polling once terminal — no more updates expected.
        if (TERMINAL_STATUSES.has(fetched.status) && pollingRef.current) {
          clearInterval(pollingRef.current);
          pollingRef.current = null;
        }
      } catch (err) {
        if (!silent) {
          const message =
            err instanceof ApiClientError
              ? err.message
              : 'Failed to load job. Please try again.';
          setFetchError(message);
        }
      } finally {
        if (!silent) setIsLoading(false);
      }
    },
    [jobId],
  );

  useEffect(() => { fetchJob(); }, [fetchJob]);

  // Start polling once job is loaded and non-terminal
  useEffect(() => {
    if (!job) return;
    if (!TERMINAL_STATUSES.has(job.status)) {
      if (!pollingRef.current) {
        pollingRef.current = setInterval(
          () => fetchJob(true),
          POLL_INTERVAL_MS,
        );
      }
    } else {
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
    }
    return () => {
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
    };
  }, [job, fetchJob]);

  // ── Download ────────────────────────────────────────────────────────────────

  const handleDownload = async (): Promise<void> => {
    if (!jobId) return;
    setDownloadError(null);
    setIsDownloading(true);

    try {
      const { download_url, filename } = await getDownloadUrl(jobId);

      /**
       * Trigger browser download without opening a new tab.
       *
       * HOW:
       *   1. Create an invisible <a> element.
       *   2. Set href to the presigned S3 GET URL.
       *   3. Set download attribute to the suggested filename.
       *   4. Append to the DOM (required by some browsers for .click() to work).
       *   5. Programmatically click it — browser starts the download.
       *   6. Remove the element immediately (it's no longer needed).
       *
       * WHY APPEND TO DOM:
       *   Firefox and Safari require an anchor to be in the document before
       *   a programmatic .click() fires the download. Chrome works without it,
       *   but appending is the cross-browser safe approach.
       *
       * PRESIGNED URL SECURITY:
       *   The URL is a time-limited bearer token (valid for 15 minutes).
       *   Anyone with the URL can download the file. The frontend initiates
       *   the download immediately on click — the 15-minute window is generous.
       */
      const anchor = document.createElement('a');
      anchor.href = download_url;
      anchor.download = filename; // suggests filename in Save As dialog
      anchor.style.display = 'none';
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
    } catch (err) {
      const message =
        err instanceof ApiClientError
          ? err.message
          : 'Failed to start download. Please try again.';
      setDownloadError(message);
    } finally {
      setIsDownloading(false);
    }
  };

  // ── Loading ─────────────────────────────────────────────────────────────────

  if (isLoading) {
    return (
      <Box>
        <Skeleton variant="text" width={120} height={32} sx={{ mb: 3 }} />
        <Paper elevation={1} sx={{ p: 3 }}>
          <Skeleton variant="text" width="50%" height={32} sx={{ mb: 2 }} />
          <Skeleton variant="text" width="30%" height={24} sx={{ mb: 1 }} />
          <Skeleton variant="rectangular" height={120} />
        </Paper>
      </Box>
    );
  }

  // ── Error ───────────────────────────────────────────────────────────────────

  if (fetchError) {
    return (
      <Box>
        <Button
          component={RouterLink}
          to="/"
          startIcon={<ArrowBackIcon />}
          sx={{ mb: 2 }}
        >
          Back to jobs
        </Button>
        <Alert
          severity="error"
          role="alert"
          action={
            <Button color="inherit" size="small" onClick={() => fetchJob()}>
              Retry
            </Button>
          }
        >
          {fetchError}
        </Alert>
      </Box>
    );
  }

  if (!job) return <></>;

  const isPolling = pollingRef.current !== null;

  // ── Job detail ──────────────────────────────────────────────────────────────

  return (
    <Box>
      {/* Back navigation */}
      <Button
        component={RouterLink}
        to="/"
        startIcon={<ArrowBackIcon />}
        sx={{ mb: 2 }}
      >
        Back to jobs
      </Button>

      {/* Header: filename + status + action buttons */}
      <Box
        sx={{
          display: 'flex',
          alignItems: 'flex-start',
          justifyContent: 'space-between',
          mb: 3,
          flexWrap: 'wrap',
          gap: 2,
        }}
      >
        <Box>
          <Typography variant="h5" sx={{ fontWeight: 600 }} gutterBottom>
            {job.original_filename}
          </Typography>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <JobStatusChip status={job.status} size="medium" />
            {isPolling && (
              <Tooltip title="Checking for updates…">
                <CircularProgress size={16} thickness={5} />
              </Tooltip>
            )}
          </Box>
        </Box>

        <Box sx={{ display: 'flex', gap: 1 }}>
          <Tooltip title="Refresh">
            <Button
              variant="outlined"
              size="small"
              startIcon={<RefreshIcon />}
              onClick={() => fetchJob()}
            >
              Refresh
            </Button>
          </Tooltip>

          {job.status === 'completed' && (
            <Button
              variant="contained"
              size="small"
              startIcon={
                isDownloading ? (
                  <CircularProgress size={14} color="inherit" />
                ) : (
                  <DownloadIcon />
                )
              }
              onClick={handleDownload}
              disabled={isDownloading}
            >
              {isDownloading ? 'Preparing…' : 'Download processed CSV'}
            </Button>
          )}
        </Box>
      </Box>

      {/* Download error */}
      {downloadError && (
        <Alert severity="error" role="alert" sx={{ mb: 3 }}>
          {downloadError}
        </Alert>
      )}

      {/* Active job processing message */}
      {(job.status === 'queued' || job.status === 'processing') && (
        <Alert severity="info" sx={{ mb: 3 }}>
          {job.status === 'queued'
            ? 'Your file is queued for processing. This page will update automatically.'
            : 'Your file is being processed. This page will update automatically.'}
        </Alert>
      )}

      {/* Failed job reason */}
      {job.status === 'failed' && job.failure_reason && (
        <Alert severity="error" sx={{ mb: 3 }}>
          <Typography variant="body2" sx={{ fontWeight: 500 }} gutterBottom>
            Processing failed
          </Typography>
          <Typography variant="body2">{job.failure_reason}</Typography>
          <Typography variant="body2" sx={{ mt: 1 }} color="text.secondary">
            Please check your CSV file and{' '}
            <RouterLink to="/upload" style={{ color: 'inherit' }}>
              upload a corrected version
            </RouterLink>
            .
          </Typography>
        </Alert>
      )}

      {/* File information panel */}
      <Paper elevation={1} sx={{ mb: 3 }}>
        <Box sx={{ p: 3 }}>
          <Typography variant="subtitle2" color="text.secondary" gutterBottom>
            File information
          </Typography>
          {/*
            Stack with flexWrap + useFlexGap — wraps metrics onto new lines on
            narrow screens. flexWrap must be in sx in MUI v9 (no longer a direct prop).
            useFlexGap enables gap-based spacing (modern CSS gap) instead of the
            legacy margin-based approach. Gap works correctly with flex wrapping;
            margin-based spacing creates extra space at the edges on the last row.
          */}
          <Stack
            direction="row"
            divider={<Divider orientation="vertical" flexItem />}
            spacing={3}
            useFlexGap
            sx={{ flexWrap: 'wrap' }}
          >
            <MetricItem label="Filename" value={job.original_filename} />
            {job.file_size_bytes !== null && (
              <MetricItem label="Size" value={formatBytes(job.file_size_bytes)} />
            )}
            {job.created_at && (
              <MetricItem
                label="Uploaded"
                value={new Date(job.created_at).toLocaleString()}
              />
            )}
          </Stack>
        </Box>

        {/* Processing results — only shown when completed */}
        {job.status === 'completed' && (
          <>
            <Divider />
            <Box sx={{ p: 3 }}>
              <Typography variant="subtitle2" color="text.secondary" gutterBottom>
                Processing results
              </Typography>
              <Stack
                direction="row"
                divider={<Divider orientation="vertical" flexItem />}
                spacing={3}
                useFlexGap
                sx={{ flexWrap: 'wrap' }}
              >
                {job.row_count !== null && (
                  <MetricItem
                    label="Rows"
                    value={job.row_count.toLocaleString()}
                  />
                )}
                {job.column_count !== null && (
                  <MetricItem
                    label="Columns"
                    value={job.column_count.toString()}
                  />
                )}
                {job.duplicate_rows_removed !== null && (
                  <MetricItem
                    label="Duplicates removed"
                    value={job.duplicate_rows_removed.toLocaleString()}
                    highlight={job.duplicate_rows_removed > 0}
                  />
                )}
              </Stack>

              {/* Column names as chips */}
              {job.column_names && job.column_names.length > 0 && (
                <Box sx={{ mt: 2 }}>
                  <Typography
                    variant="caption"
                    color="text.secondary"
                    sx={{ display: 'block' }}
                    gutterBottom
                  >
                    Columns
                  </Typography>
                  <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
                    {job.column_names.map((col) => (
                      <Chip
                        key={col}
                        label={col}
                        size="small"
                        variant="outlined"
                      />
                    ))}
                  </Box>
                </Box>
              )}
            </Box>
          </>
        )}
      </Paper>

      {/* Job ID for reference (useful for debugging and support) */}
      <Typography variant="caption" color="text.disabled">
        Job ID: {job.job_id}
      </Typography>
    </Box>
  );
}

// ── MetricItem sub-component ──────────────────────────────────────────────────

interface MetricItemProps {
  label: string;
  value: string;
  /**
   * highlight: when true, renders the value in warning colour with bold weight.
   * Used to draw attention to non-zero duplicate counts.
   */
  highlight?: boolean;
}

function MetricItem({
  label,
  value,
  highlight = false,
}: MetricItemProps): React.ReactElement {
  return (
    <Box>
      <Typography
        variant="caption"
        color="text.secondary"
        sx={{ display: 'block' }}
      >
        {label}
      </Typography>
      <Typography
        variant="body1"
        color={highlight ? 'warning.main' : 'text.primary'}
        sx={{ fontWeight: highlight ? 600 : 400 }}
      >
        {value}
      </Typography>
    </Box>
  );
}
