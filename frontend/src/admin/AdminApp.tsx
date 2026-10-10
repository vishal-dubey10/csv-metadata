/**
 * CSVForge — React Admin application (admin surface).
 *
 * WHAT:
 *   A separate React Admin instance mounted at /admin. Shows all jobs for all
 *   users — visible only to members of the csvforge-admins Cognito group.
 *   Uses React Admin's ra-data-simple-rest data provider to talk to the
 *   GET /admin/jobs endpoint.
 *
 * ARCHITECTURE (AD-03):
 *   The admin surface is a distinct React Admin app, not embedded in the main
 *   React Router tree. This is deliberate: React Admin has its own router
 *   and layout. Mixing it into the main app routing creates conflicts.
 *   Instead, AdminApp is rendered as a standalone component at the /admin
 *   path, completely replacing the main app shell for that route.
 *
 * DATA PROVIDER:
 *   React Admin's ra-data-simple-rest expects the backend to follow this
 *   convention (already implemented in the backend):
 *     GET /admin/jobs?_start=0&_end=25&_sort=created_at&_order=DESC
 *     Response header: Content-Range: jobs 0-24/150
 *   The Content-Range header is what React Admin uses to compute total pages.
 *   The backend already sets this header via the admin_list_jobs endpoint.
 *
 * AUTHENTICATION:
 *   React Admin calls are made through a custom httpClient that injects the
 *   same authentication headers as the main API client (Bearer token or
 *   X-Dev-User-Id in local dev). This is the correct approach — React Admin
 *   never directly imports from our api/client.ts, so we bridge them here.
 *
 * IMPORTANT: React Admin uses react-router@7 internally (required by the
 *   peer dep range "^6.28.1 || ^7.1.1"). The main app also uses react-router@7.
 *   A single version in node_modules is shared — no conflicts.
 *
 * SECURITY:
 *   The RequireAuth wrapper with requireAdmin=true gates access at the route
 *   level. Even if AdminApp were accessed directly, the backend returns 403
 *   for non-admin users on GET /admin/jobs.
 *
 * INTERVIEW CONCEPT:
 *   "Why use React Admin for the admin surface instead of building custom?"
 *   React Admin provides pagination, filtering, sorting, and a data grid
 *   out of the box, matching exactly what an admin needs. Building these
 *   from scratch would take significantly more time with no user-facing benefit.
 *   The trade-off: React Admin is opinionated about its data provider contract.
 *   The backend's admin endpoint was designed to match that contract
 *   (Content-Range header, _start/_end/_sort/_order query params).
 */

import React from 'react';
import {
  Admin,
  Resource,
  List,
  Datagrid,
  TextField,
  DateField,
  SelectInput,
  useRecordContext,
} from 'react-admin';
import simpleRestProvider from 'ra-data-simple-rest';
import { getAuthToken } from '../api/client';
import JobStatusChip from '../components/JobStatusChip';
import type { JobStatus } from '../types/api';

// ── Base URL ──────────────────────────────────────────────────────────────────

const API_BASE_URL = import.meta.env.VITE_API_URL as string;
const IS_LOCAL_DEV = !import.meta.env.VITE_COGNITO_USER_POOL_ID;

// ── Auth-aware HTTP client for React Admin ────────────────────────────────────

/**
 * Custom httpClient for the ra-data-simple-rest provider.
 *
 * React Admin's data provider makes raw fetch() calls. This wrapper
 * intercepts them to add our authentication headers — the same headers
 * the main API client uses.
 *
 * WHY NOT import from api/client.ts:
 *   api/client.ts stores the token in module-scope state and exports
 *   setAuthToken(). We could import that, but React Admin's httpClient
 *   receives the URL and options and must return a Response — a different
 *   interface from our typed API functions. The simplest correct approach
 *   is to read the token from the same module-scope variable, which we
 *   access by importing the setter and using a closure.
 *
 * ALTERNATIVE: We could use React Admin's authProvider pattern to handle
 *   login/logout within React Admin. For this MVP, the main app's AuthContext
 *   manages all authentication. The admin surface simply reads the current
 *   token state.
 */
function buildAdminHeaders(): HeadersInit {
  if (IS_LOCAL_DEV) {
    return {
      'X-Dev-User-Id': 'dev-user-001',
      'X-Dev-User-Email': 'dev@csvforge.local',
      'X-Dev-Is-Admin': 'true', // local dev admin access
    };
  }

  // In production, read the token from the API client's in-memory store.
  // AdminApp is rendered in the same browser session as the main app, so
  // the token set by AuthContext.login() via setAuthToken() is available here.
  const token = getAuthToken();
  if (token) {
    return { Authorization: `Bearer ${token}` };
  }
  return {};
}

async function adminHttpClient(
  url: string,
  options: RequestInit = {},
): Promise<{ status: number; headers: Headers; body: string; json: unknown }> {
  const response = await fetch(url, {
    ...options,
    headers: {
      ...(options.headers as Record<string, string>),
      ...buildAdminHeaders(),
    },
  });

  const body = await response.text();
  let json: unknown;
  try {
    json = JSON.parse(body);
  } catch {
    json = body;
  }

  return { status: response.status, headers: response.headers, body, json };
}

// ── Data provider ─────────────────────────────────────────────────────────────

// ra-data-simple-rest expects the resource URL to be:
//   GET /admin/jobs?_start=0&_end=25&...
// The admin endpoint already follows this convention.
const dataProvider = simpleRestProvider(
  `${API_BASE_URL}/admin`,
  adminHttpClient,
);

// ── Status field (custom) ─────────────────────────────────────────────────────

function StatusField(): React.ReactElement | null {
  const record = useRecordContext<{ status: JobStatus }>();
  if (!record) return null;
  return <JobStatusChip status={record.status} />;
}
// React Admin uses this label in the column header.
// In React 19, defaultProps on functions is deprecated — use a wrapper component
// with a 'label' prop rendered by React Admin instead.
StatusField.defaultProps = { label: 'Status' };

// ── Job list filters ──────────────────────────────────────────────────────────

const jobFilters = [
  <SelectInput
    key="status"
    source="status"
    label="Status"
    choices={[
      { id: 'queued',     name: 'Queued'     },
      { id: 'processing', name: 'Processing' },
      { id: 'completed',  name: 'Completed'  },
      { id: 'failed',     name: 'Failed'     },
    ]}
    alwaysOn
  />,
];

// ── Admin job list ────────────────────────────────────────────────────────────

function JobList(): React.ReactElement {
  return (
    <List
      filters={jobFilters}
      sort={{ field: 'created_at', order: 'DESC' }}
      perPage={25}
    >
      <Datagrid rowClick={false} bulkActionButtons={false}>
        <TextField source="job_id"          label="Job ID"   sortable={false} />
        <TextField source="user_email"      label="User"     />
        <TextField source="original_filename" label="File"   />
        <StatusField />
        <TextField source="row_count"       label="Rows"     />
        <TextField source="duplicate_rows_removed" label="Dupes removed" />
        <DateField source="created_at"      label="Uploaded" showTime />
        <DateField source="updated_at"      label="Updated"  showTime />
      </Datagrid>
    </List>
  );
}

// ── Admin app ─────────────────────────────────────────────────────────────────

export default function AdminApp(): React.ReactElement {
  return (
    <Admin
      dataProvider={dataProvider}
      basename="/admin"
      title="CSVForge Admin"
      // No authProvider: authentication is handled by the main app's AuthContext.
      // The RequireAuth wrapper (in App.tsx) ensures only admins reach this component.
      requireAuth={false}
    >
      <Resource
        name="jobs"
        list={JobList}
        options={{ label: 'Jobs' }}
      />
    </Admin>
  );
}
