/**
 * CSVForge — Upload wizard page.
 *
 * WHAT:
 *   A three-step upload flow:
 *     Step 1 — File selection (idle/selected)
 *     Step 2 — Upload in progress (creating → uploading → confirming)
 *     Step 3 — Success or error
 *
 * THE UPLOAD FLOW (matches backend contract exactly):
 *   1. POST /jobs          → {job_id, upload_url, s3_key}       (creates DB record)
 *   2. PUT <upload_url>    → S3 direct upload (no auth header)  (stores file in S3)
 *   3. POST /uploads/{id}/complete  → {job_id, status: "queued"} (validates + enqueues)
 *   4. Navigate to /jobs/{job_id} to watch status
 *
 * MUI v9 API NOTES:
 *   - Typography does not accept fontWeight/display/textAlign/noWrap as direct props.
 *     These must go inside the sx prop.
 *   - TextField.inputProps is removed; use slotProps={{ htmlInput: {...} }} instead.
 *   - Stack.flexWrap must be in sx, not a direct prop.
 *
 * IMPORTANT CONSTRAINTS:
 *   - Only .csv files accepted (validated client-side AND server-side)
 *   - Max file size: 50 MiB enforced by the presigned URL content-length-range
 *     condition (server-side) and shown as an error client-side early
 *   - No Authorization header on the S3 PUT (AD-FE-04)
 *   - user_email sent in the request body is informational — authorization uses
 *     the JWT sub claim on the backend (AD-23)
 */

import React, { useCallback, useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  LinearProgress,
  Paper,
  Step,
  StepLabel,
  Stepper,
  Typography,
} from '@mui/material';
import UploadFileIcon from '@mui/icons-material/UploadFile';
import CheckCircleOutlinedIcon from '@mui/icons-material/CheckCircleOutlined';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import {
  createJob,
  uploadFileToS3,
  confirmUpload,
  ApiClientError,
} from '../api/client';
import type { UploadState } from '../types/api';

// ── Constants ─────────────────────────────────────────────────────────────────

const MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024; // 50 MiB — matches backend AD-27
const ACCEPTED_EXTENSIONS = ['.csv'];

// Stepper labels
const STEPS = ['Select file', 'Upload', 'Processing'];

// ── Component ─────────────────────────────────────────────────────────────────

export default function UploadPage(): React.ReactElement {
  const { user } = useAuth();
  const navigate = useNavigate();

  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [uploadState, setUploadState] = useState<UploadState>({
    step: 'idle',
    progress: 0,
    jobId: null,
    error: null,
  });

  const fileInputRef = useRef<HTMLInputElement>(null);

  // ── File validation ─────────────────────────────────────────────────────────

  function validateFile(file: File): string | null {
    const extension = file.name.split('.').pop()?.toLowerCase();
    if (extension !== 'csv') {
      return `Only CSV files are accepted. "${file.name}" does not have a .csv extension.`;
    }
    if (file.size === 0) {
      return 'The selected file is empty. Please choose a CSV with content.';
    }
    if (file.size > MAX_FILE_SIZE_BYTES) {
      const sizeMb = (file.size / (1024 * 1024)).toFixed(1);
      return `File is too large (${sizeMb} MB). The maximum allowed size is 50 MB.`;
    }
    return null;
  }

  // ── File selection ──────────────────────────────────────────────────────────

  const handleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0] ?? null;
      setFileError(null);
      setUploadState({ step: 'idle', progress: 0, jobId: null, error: null });

      if (!file) {
        setSelectedFile(null);
        return;
      }

      const validationError = validateFile(file);
      if (validationError) {
        setFileError(validationError);
        setSelectedFile(null);
        if (fileInputRef.current) fileInputRef.current.value = '';
        return;
      }

      setSelectedFile(file);
      setUploadState({ step: 'selected', progress: 0, jobId: null, error: null });
    },
    [],
  );

  const handleDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      const file = e.dataTransfer.files[0];
      if (file) {
        setFileError(null);
        setUploadState({ step: 'idle', progress: 0, jobId: null, error: null });
        const validationError = validateFile(file);
        if (validationError) {
          setFileError(validationError);
          setSelectedFile(null);
          return;
        }
        setSelectedFile(file);
        setUploadState({ step: 'selected', progress: 0, jobId: null, error: null });
      }
    },
    [],
  );

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>): void => {
    e.preventDefault();
  };

  // ── Upload orchestration ────────────────────────────────────────────────────

  const handleUpload = useCallback(async (): Promise<void> => {
    if (!selectedFile || !user) return;

    setUploadState({ step: 'creating', progress: 0, jobId: null, error: null });

    let jobId: string;
    let uploadUrl: string;

    try {
      const response = await createJob(
        user.email || 'dev@csvforge.local',
        selectedFile.name,
        selectedFile.size,
      );
      jobId = response.job_id;
      uploadUrl = response.upload_url;
    } catch (err) {
      const message =
        err instanceof ApiClientError
          ? err.message
          : 'Failed to create upload job. Please try again.';
      setUploadState({ step: 'error', progress: 0, jobId: null, error: message });
      return;
    }

    setUploadState({ step: 'uploading', progress: 0, jobId, error: null });

    try {
      await uploadFileToS3(uploadUrl, selectedFile, (percent) => {
        setUploadState((prev) => ({ ...prev, progress: percent }));
      });
    } catch (err) {
      const message =
        err instanceof ApiClientError
          ? err.message
          : 'File upload failed. Please check your connection and try again.';
      setUploadState({ step: 'error', progress: 0, jobId, error: message });
      return;
    }

    setUploadState({ step: 'confirming', progress: 100, jobId, error: null });

    try {
      await confirmUpload(jobId);
    } catch (err) {
      const message =
        err instanceof ApiClientError
          ? err.message
          : 'Failed to confirm upload. Please try again.';
      setUploadState({ step: 'error', progress: 100, jobId, error: message });
      return;
    }

    setUploadState({ step: 'done', progress: 100, jobId, error: null });
    setTimeout(() => navigate(`/jobs/${jobId}`), 800);
  }, [selectedFile, user, navigate]);

  // ── Stepper active step ─────────────────────────────────────────────────────

  function activeStep(): number {
    switch (uploadState.step) {
      case 'idle':
      case 'selected':
        return 0;
      case 'creating':
      case 'uploading':
      case 'confirming':
        return 1;
      case 'done':
      case 'error':
        return 2;
    }
  }

  const isUploading = ['creating', 'uploading', 'confirming'].includes(
    uploadState.step,
  );

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <Box>
      <Typography
        variant="h5"
        component="h1"
        gutterBottom
        sx={{ fontWeight: 600 }}
      >
        Upload CSV file
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 4 }}>
        Upload a CSV to inspect it, remove duplicates and whitespace, and
        download the cleaned result.
      </Typography>

      <Stepper activeStep={activeStep()} sx={{ mb: 4 }}>
        {STEPS.map((label) => (
          <Step key={label}>
            <StepLabel>{label}</StepLabel>
          </Step>
        ))}
      </Stepper>

      <Paper elevation={1} sx={{ p: 4, maxWidth: 600 }}>
        {/* ── Success state ─────────────────────────────────────────────── */}
        {uploadState.step === 'done' && (
          <Box sx={{ textAlign: 'center', py: 2 }}>
            <CheckCircleOutlinedIcon
              color="success"
              sx={{ fontSize: 48, mb: 2 }}
            />
            <Typography variant="h6" gutterBottom>
              File queued for processing
            </Typography>
            <Typography variant="body2" color="text.secondary">
              Redirecting to your job…
            </Typography>
          </Box>
        )}

        {/* ── Error state ───────────────────────────────────────────────── */}
        {uploadState.step === 'error' && uploadState.error && (
          <>
            <Alert severity="error" sx={{ mb: 3 }} role="alert">
              {uploadState.error}
            </Alert>
            <Button
              variant="outlined"
              onClick={() =>
                setUploadState({
                  step: selectedFile ? 'selected' : 'idle',
                  progress: 0,
                  jobId: null,
                  error: null,
                })
              }
            >
              Try again
            </Button>
          </>
        )}

        {/* ── Normal flow ───────────────────────────────────────────────── */}
        {uploadState.step !== 'done' && uploadState.step !== 'error' && (
          <>
            {/* File drop zone */}
            <Box
              onDrop={handleDrop}
              onDragOver={handleDragOver}
              onClick={() => !isUploading && fileInputRef.current?.click()}
              role="button"
              tabIndex={0}
              aria-label="Select or drop a CSV file"
              onKeyDown={(e) => {
                if ((e.key === 'Enter' || e.key === ' ') && !isUploading)
                  fileInputRef.current?.click();
              }}
              sx={{
                border: '2px dashed',
                borderColor: selectedFile
                  ? 'primary.main'
                  : fileError
                    ? 'error.main'
                    : 'grey.400',
                borderRadius: 2,
                p: 4,
                textAlign: 'center',
                cursor: isUploading ? 'default' : 'pointer',
                backgroundColor: selectedFile ? 'primary.50' : 'transparent',
                transition: 'border-color 0.2s, background-color 0.2s',
                '&:hover': {
                  borderColor: isUploading ? undefined : 'primary.main',
                },
                '&:focus-visible': {
                  outline: '2px solid',
                  outlineColor: 'primary.main',
                },
              }}
            >
              <input
                ref={fileInputRef}
                type="file"
                accept={ACCEPTED_EXTENSIONS.join(',')}
                style={{ display: 'none' }}
                onChange={handleFileChange}
                aria-hidden="true"
                disabled={isUploading}
              />

              <UploadFileIcon
                sx={{
                  fontSize: 40,
                  color: selectedFile ? 'primary.main' : 'grey.500',
                  mb: 1,
                }}
              />

              {selectedFile ? (
                <>
                  <Typography variant="body1" sx={{ fontWeight: 500 }}>
                    {selectedFile.name}
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    {formatBytes(selectedFile.size)}
                    {!isUploading && ' · Click to change'}
                  </Typography>
                </>
              ) : (
                <>
                  <Typography variant="body1">
                    Drag and drop a CSV file, or click to browse
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    Maximum file size: 50 MB
                  </Typography>
                </>
              )}
            </Box>

            {fileError && (
              <Alert severity="error" sx={{ mt: 2 }} role="alert">
                {fileError}
              </Alert>
            )}

            {(uploadState.step === 'uploading' ||
              uploadState.step === 'confirming') && (
              <Box sx={{ mt: 3 }}>
                <Box
                  sx={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    mb: 0.5,
                  }}
                >
                  <Typography variant="body2">
                    {uploadState.step === 'confirming'
                      ? 'Verifying upload…'
                      : 'Uploading…'}
                  </Typography>
                  <Typography variant="body2">
                    {uploadState.progress}%
                  </Typography>
                </Box>
                <LinearProgress
                  variant={
                    uploadState.step === 'confirming'
                      ? 'indeterminate'
                      : 'determinate'
                  }
                  value={uploadState.progress}
                  aria-label={`Upload progress: ${uploadState.progress}%`}
                />
              </Box>
            )}

            {uploadState.step === 'creating' && (
              <Box sx={{ mt: 3, display: 'flex', alignItems: 'center', gap: 1 }}>
                <CircularProgress size={16} />
                <Typography variant="body2" color="text.secondary">
                  Preparing upload…
                </Typography>
              </Box>
            )}

            <Box sx={{ mt: 3, display: 'flex', gap: 2 }}>
              <Button
                variant="contained"
                size="large"
                onClick={handleUpload}
                disabled={!selectedFile || isUploading || !!fileError}
                sx={{ minWidth: 140 }}
              >
                {isUploading ? (
                  <>
                    <CircularProgress size={16} sx={{ mr: 1 }} color="inherit" />
                    Uploading…
                  </>
                ) : (
                  'Upload & Process'
                )}
              </Button>

              {selectedFile && !isUploading && (
                <Button
                  variant="text"
                  onClick={() => {
                    setSelectedFile(null);
                    setFileError(null);
                    setUploadState({
                      step: 'idle',
                      progress: 0,
                      jobId: null,
                      error: null,
                    });
                    if (fileInputRef.current) fileInputRef.current.value = '';
                  }}
                >
                  Clear
                </Button>
              )}
            </Box>
          </>
        )}
      </Paper>
    </Box>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
