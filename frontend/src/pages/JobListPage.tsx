/**
 * CSVForge — Job list page (home page for authenticated users).
 *
 * WHAT:
 *   Shows all jobs belonging to the authenticated user, newest first.
 *   Polls GET /jobs every 5 seconds while any job is in a non-terminal state
 *   (queued or processing), so users see status updates without manual refresh.
 *
 * POLLING DESIGN:
 *   - Interval: 5 seconds (balances freshness vs. API load for an MVP).
 *   - Only polls while the page is mounted and there are active jobs.
 *   - Stops polling when all visible jobs are terminal (completed/failed).
 *   - Uses useEffect cleanup to cancel the interval on unmount (prevents
 *     state updates after navigation away).
 *   - Does NOT poll on every render — the setInterval is set up once and
 *     cleared on unmount or when polling is no longer needed.
 *
 * EMPTY STATE:
 *   If the user has no jobs, shows a call-to-action to upload their first file.
 *
 * ERROR STATE:
 *   If the initial load fails (e.g. network error, 401), shows an error
 *   message with a retry button. Polling errors are silenced after the first
 *   successful load — a transient poll failure shouldn't wipe out the list.
 *
 * MUI v9 API CHANGES:
 *   MUI v9 removed many shorthand HTML-attribute props from Typography and Stack
 *   (fontWeight, display, textAlign, noWrap, flexWrap). These must now be passed
 *   inside the sx prop, which routes them through the MUI style engine:
 *     v8: <Typography fontWeight={600}>    → v9: <Typography sx={{ fontWeight: 600 }}>
 *     v8: <Typography display="block">    → v9: <Typography sx={{ display: 'block' }}>
 *   This is a deliberate MUI v9 breaking change — putting layout styles in sx
 *   keeps component props focused on semantics, not CSS. See AD-FE-01.
 *
 * INTERVIEW CONCEPT:
 *   "Why polling instead of WebSockets for status updates?"
 *   WebSockets maintain a persistent TCP connection that stays open while
 *   the user watches their job. Lambda functions are stateless and short-lived
 *   — a persistent connection cannot be maintained across Lambda invocations.
 *   API Gateway WebSocket APIs are possible but add significant infrastructure
 *   complexity (connection management, session routing). For an async
 *   background job that takes 5–60 seconds, polling every 5s is simple,
 *   reliable, and imposes negligible load. The trade-off: up to 5 seconds
 *   of latency between job completion and the user seeing the result.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Divider,
  IconButton,
  Paper,
  Skeleton,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from '@mui/material';
import RefreshIcon from '@mui/icons-material/Refresh';
import AddIcon from '@mui/icons-material/Add';
import { Link as RouterLink } from 'react-router-dom';
import { listJobs, ApiClientError } from '../api/client';
import JobStatusChip from '../components/JobStatusChip';
import type { Job } from '../types/api';

// ── Constants ─────────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 5000;
const TERMINAL_STATUSES = new Set(['completed', 'failed']);

// ── Helpers ───────────────────────────────────────────────────────────────────

function hasActiveJobs(jobs: Job[]): boolean {
  return jobs.some((j) => !TERMINAL_STATUSES.has(j.status));
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatRelativeTime(isoString: string): string {
  const date = new Date(isoString);
  const now = new Date();
  const diffSeconds = Math.floor((now.getTime() - date.getTime()) / 1000);
  if (diffSeconds < 60) return 'just now';
  if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)}m ago`;
  if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)}h ago`;
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function JobListPage(): React.ReactElement {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  /**
   * pollingRef stores the setInterval ID.
   *
   * WHY useRef INSTEAD OF useState:
   *   The interval ID is infrastructure state — changing it should NOT
   *   trigger a re-render. useRef stores a mutable value that persists across
   *   renders without causing them. If we used useState, every time we set
   *   or clear the interval, React would re-render the component unnecessarily.
   */
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Fetch jobs ──────────────────────────────────────────────────────────────

  /**
   * fetchJobs — load the job list from the API.
   *
   * @param silent  If true, skips the loading spinner and silently ignores
   *                errors. Used for background polling — we don't want a
   *                transient network hiccup to wipe out the displayed list.
   *
   * WHY useCallback:
   *   fetchJobs is declared inside the component so it can close over state
   *   setters. Without useCallback it would be recreated on every render,
   *   which would cause the useEffect below (which depends on fetchJobs)
   *   to re-run on every render — creating an infinite polling loop.
   *   useCallback memoizes the function so it's only recreated when its
   *   dependencies change (empty here — it never needs to change).
   */
  const fetchJobs = useCallback(async (silent = false): Promise<void> => {
    if (!silent) setIsLoading(true);
    try {
      const response = await listJobs();
      setJobs(response.jobs);
      setError(null);
    } catch (err) {
      if (!silent) {
        // Only show errors on the initial (non-silent) load.
        // A polling failure while the list is already showing should not
        // replace the list with an error message.
        const message =
          err instanceof ApiClientError
            ? err.message
            : 'Failed to load jobs. Please try again.';
        setError(message);
      }
    } finally {
      if (!silent) setIsLoading(false);
    }
  }, []);

  // ── Initial load ────────────────────────────────────────────────────────────

  useEffect(() => { fetchJobs(); }, [fetchJobs]);

  // ── Polling control ─────────────────────────────────────────────────────────
  /**
   * Start or stop polling based on whether there are active jobs.
   *
   * LIFECYCLE:
   *   - After fetchJobs() succeeds, the jobs state updates.
   *   - This effect runs, checks if any jobs are non-terminal.
   *   - If yes and polling is not already running → start it.
   *   - If no active jobs and polling is running → clear it.
   *   - On unmount (user navigates away) → cleanup clears any running interval.
   *
   * WHY TWO SEPARATE useEffects (initial load + polling control):
   *   Combining them would couple "initial fetch" logic with "polling" logic
   *   inside one complex effect. Separate effects have separate responsibilities
   *   and are easier to reason about.
   */
  useEffect(() => {
    if (hasActiveJobs(jobs)) {
      if (!pollingRef.current) {
        pollingRef.current = setInterval(
          () => fetchJobs(true), // silent — don't show spinner during poll
          POLL_INTERVAL_MS,
        );
      }
    } else {
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
    }

    // Cleanup: cancel the interval when this effect re-runs or component unmounts.
    return () => {
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
    };
  }, [jobs, fetchJobs]);

  const isPolling = pollingRef.current !== null;

  // ── Loading skeleton ────────────────────────────────────────────────────────
  /**
   * Skeleton loading state — shown on first load before any data arrives.
   *
   * WHY SKELETONS (not a spinner):
   *   Skeletons show the approximate shape of the content that's about to
   *   appear. This reduces "layout shift" — the page doesn't jump around when
   *   data loads because the skeleton preserves the approximate vertical space.
   *   A spinner gives no hint about what's coming; a skeleton does.
   */
  if (isLoading) {
    return (
      <Box>
        <Box sx={{ display: 'flex', justifyContent: 'space-between', mb: 3 }}>
          <Typography variant="h5" sx={{ fontWeight: 600 }}>Your jobs</Typography>
        </Box>
        <Paper elevation={1}>
          {[1, 2, 3].map((i) => (
            <Box key={i} sx={{ p: 2 }}>
              <Skeleton variant="text" width="60%" height={24} />
              <Skeleton variant="text" width="30%" height={20} />
              {i < 3 && <Divider sx={{ mt: 1 }} />}
            </Box>
          ))}
        </Paper>
      </Box>
    );
  }

  // ── Error state ─────────────────────────────────────────────────────────────

  if (error) {
    return (
      <Box>
        <Typography variant="h5" sx={{ fontWeight: 600 }} gutterBottom>
          Your jobs
        </Typography>
        <Alert
          severity="error"
          role="alert"
          action={
            <Button color="inherit" size="small" onClick={() => fetchJobs()}>
              Retry
            </Button>
          }
        >
          {error}
        </Alert>
      </Box>
    );
  }

  // ── Empty state ─────────────────────────────────────────────────────────────

  if (jobs.length === 0) {
    return (
      <Box>
        <Typography variant="h5" sx={{ fontWeight: 600 }} gutterBottom>
          Your jobs
        </Typography>
        <Paper
          elevation={0}
          sx={{
            p: 6,
            textAlign: 'center',
            border: '1px dashed',
            borderColor: 'grey.300',
            borderRadius: 2,
          }}
        >
          <Typography variant="h6" gutterBottom>No jobs yet</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
            Upload a CSV file to get started. CSVForge will inspect, clean,
            and prepare a processed copy for download.
          </Typography>
          <Button
            component={RouterLink}
            to="/upload"
            variant="contained"
            startIcon={<AddIcon />}
            size="large"
          >
            Upload your first CSV
          </Button>
        </Paper>
      </Box>
    );
  }

  // ── Job table ───────────────────────────────────────────────────────────────

  return (
    <Box>
      {/* Header row: title + polling indicator + action buttons */}
      <Box
        sx={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          mb: 3,
        }}
      >
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <Typography variant="h5" sx={{ fontWeight: 600 }}>
            Your jobs
          </Typography>
          {/*
            Spinning indicator while polling — communicates to the user that
            the page is "alive" and checking for updates automatically.
          */}
          {isPolling && (
            <Tooltip title="Auto-refreshing while jobs are active">
              <CircularProgress size={16} thickness={5} />
            </Tooltip>
          )}
        </Box>
        <Box sx={{ display: 'flex', gap: 1 }}>
          <Tooltip title="Refresh">
            <IconButton
              onClick={() => fetchJobs()}
              aria-label="Refresh job list"
              size="small"
            >
              <RefreshIcon />
            </IconButton>
          </Tooltip>
          <Button
            component={RouterLink}
            to="/upload"
            variant="contained"
            startIcon={<AddIcon />}
            size="small"
          >
            New upload
          </Button>
        </Box>
      </Box>

      {/* Job table */}
      <TableContainer component={Paper} elevation={1}>
        <Table aria-label="Job list">
          <TableHead>
            <TableRow>
              <TableCell>File</TableCell>
              <TableCell>Status</TableCell>
              {/* Rows: hidden on mobile (xs), visible on sm+ */}
              <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
                Rows
              </TableCell>
              {/* Uploaded: hidden on xs/sm, visible on md+ */}
              <TableCell sx={{ display: { xs: 'none', md: 'table-cell' } }}>
                Uploaded
              </TableCell>
              <TableCell align="right">Action</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {jobs.map((job) => (
              <TableRow
                key={job.job_id}
                hover
                sx={{ '&:last-child td': { borderBottom: 0 } }}
              >
                {/* Filename + size */}
                <TableCell>
                  <Typography
                    variant="body2"
                    sx={{
                      fontWeight: 500,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      maxWidth: 200,
                    }}
                  >
                    {job.original_filename}
                  </Typography>
                  {job.file_size_bytes !== null && (
                    <Typography variant="caption" color="text.secondary">
                      {formatBytes(job.file_size_bytes)}
                    </Typography>
                  )}
                </TableCell>

                {/* Status chip */}
                <TableCell>
                  <JobStatusChip status={job.status} />
                </TableCell>

                {/* Row count (hidden on mobile) */}
                <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
                  <Typography variant="body2">
                    {job.row_count !== null
                      ? job.row_count.toLocaleString()
                      : '—'}
                  </Typography>
                </TableCell>

                {/* Created at (hidden on small screens) */}
                <TableCell sx={{ display: { xs: 'none', md: 'table-cell' } }}>
                  <Typography variant="body2" color="text.secondary">
                    {job.created_at ? formatRelativeTime(job.created_at) : '—'}
                  </Typography>
                </TableCell>

                {/* View / Download action */}
                <TableCell align="right">
                  <Button
                    component={RouterLink}
                    to={`/jobs/${job.job_id}`}
                    size="small"
                    variant={job.status === 'completed' ? 'contained' : 'text'}
                  >
                    {job.status === 'completed' ? 'Download' : 'View'}
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>

      <Typography
        variant="caption"
        color="text.secondary"
        sx={{ mt: 1, display: 'block' }}
      >
        {jobs.length} job{jobs.length !== 1 ? 's' : ''}
        {isPolling && ' · Auto-refreshing'}
      </Typography>
    </Box>
  );
}
