/**
 * Tests for JobStatusChip component.
 *
 * WHAT:
 *   Verifies that the chip renders the correct label and colour for each
 *   job status value. These are the status values the backend returns.
 *
 * TESTING PHILOSOPHY:
 *   We test behaviour the user sees (rendered text, accessible label),
 *   not implementation details (CSS classes, internal state). If the
 *   component is refactored to use a different MUI component internally,
 *   these tests should still pass without changes.
 *
 * WHY THESE TESTS MATTER:
 *   JobStatusChip is used on both the job list and the job detail page.
 *   A wrong label or missing status case would give users incorrect
 *   information about their job state. Testing each status exhaustively
 *   catches any accidental omission or typo in the STATUS_CONFIG mapping.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import JobStatusChip from '../components/JobStatusChip';
import type { JobStatus } from '../types/api';

// Each tuple is [status, expected display label]
const STATUS_CASES: [JobStatus, string][] = [
  ['pending_upload', 'Pending upload'],
  ['queued',         'Queued'],
  ['processing',     'Processing'],
  ['completed',      'Completed'],
  ['failed',         'Failed'],
];

describe('JobStatusChip', () => {
  it.each(STATUS_CASES)(
    'renders label "%s" for status %s',
    (status, expectedLabel) => {
      render(<JobStatusChip status={status} />);
      expect(screen.getByText(expectedLabel)).toBeInTheDocument();
    },
  );

  it.each(STATUS_CASES)(
    'has an accessible aria-label for status %s',
    (status, expectedLabel) => {
      render(<JobStatusChip status={status} />);
      /**
       * MUI v9 Chip without an onClick renders as a <div> (not a <button>).
       * The aria-label is still present on the div, so we query by it directly.
       * getByLabelText finds any element with an aria-label matching the string.
       *
       * WHY THIS MATTERS:
       *   Screen readers announce the aria-label when the user focuses the element.
       *   Testing that it's present verifies the chip is accessible to AT users
       *   who navigate by label rather than by visual position.
       */
      const chip = screen.getByLabelText(`Job status: ${expectedLabel}`);
      expect(chip).toBeInTheDocument();
    },
  );

  it('uses default size "small" when no size prop is provided', () => {
    const { container } = render(<JobStatusChip status="completed" />);
    // MUI adds MuiChip-sizeSmall class for small chips
    expect(container.querySelector('.MuiChip-sizeSmall')).toBeInTheDocument();
  });

  it('respects the size prop', () => {
    const { container } = render(
      <JobStatusChip status="completed" size="medium" />,
    );
    expect(container.querySelector('.MuiChip-sizeMedium')).toBeInTheDocument();
  });
});
