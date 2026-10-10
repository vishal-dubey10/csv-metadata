/**
 * JobStatusChip — colour-coded status badge.
 *
 * Reused on the job list and job detail page.
 * Maps JobStatus values to MUI Chip colours and display labels.
 */

import React from 'react';
import { Chip } from '@mui/material';
import type { ChipProps } from '@mui/material';
import type { JobStatus } from '../types/api';

interface JobStatusChipProps {
  status: JobStatus;
  size?: ChipProps['size'];
}

const STATUS_CONFIG: Record<
  JobStatus,
  { label: string; color: ChipProps['color'] }
> = {
  pending_upload: { label: 'Pending upload', color: 'default' },
  queued:         { label: 'Queued',         color: 'info'    },
  processing:     { label: 'Processing',     color: 'warning' },
  completed:      { label: 'Completed',      color: 'success' },
  failed:         { label: 'Failed',         color: 'error'   },
};

export default function JobStatusChip({
  status,
  size = 'small',
}: JobStatusChipProps): React.ReactElement {
  const config = STATUS_CONFIG[status] ?? {
    label: status,
    color: 'default' as const,
  };
  return (
    <Chip
      label={config.label}
      color={config.color}
      size={size}
      aria-label={`Job status: ${config.label}`}
    />
  );
}
