# Sheet 1 — React Concepts in CSVForge

> Every concept is shown exactly as it appears in this codebase.
> File paths and function names are real.

---

## 1. JSX — Writing HTML inside JavaScript

JSX looks like HTML but it compiles to `React.createElement()` calls.
The browser never sees JSX — Vite (the build tool) transforms it to plain JS.

```tsx
// File: frontend/src/components/AppLayout.tsx

// This JSX:
<Typography variant="h6" component={RouterLink} to="/">
  CSVForge
</Typography>

// Compiles to:
React.createElement(Typography, { variant: "h6", component: RouterLink, to: "/" }, "CSVForge")
```

**Rule:** Every JSX expression must return ONE root element.
Wrap siblings in `<>...</>` (a Fragment — renders nothing in the DOM).

---

## 2. Component — A Function That Returns JSX

Every piece of UI is a component: a plain function that takes **props** and
returns JSX. React calls the function and renders its output.

```tsx
// File: frontend/src/components/AppLayout.tsx

export default function AppLayout(): React.ReactElement {
  const { user, logout } = useAuth()   // read from context

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      <AppBar position="static">
        <Toolbar>
          <Typography component={RouterLink} to="/">CSVForge</Typography>
          <Button onClick={logout}>Sign out</Button>
        </Toolbar>
      </AppBar>

      {/* Outlet renders the matched child route HERE */}
      <Outlet />
    </Box>
  )
}
```

`AppLayout` is the shell. Every authenticated page renders inside it via
`<Outlet />` (explained in §6).

---

## 3. Props — Data Flowing Into a Component

Props are arguments to a component function. They flow **down** (parent → child),
never up. A child can never modify its own props.

```tsx
// File: frontend/src/components/JobStatusChip.tsx

// The component DECLARES what props it needs:
interface JobStatusChipProps {
  status: string   // e.g. "queued", "completed", "failed"
}

export default function JobStatusChip({ status }: JobStatusChipProps) {
  // ... renders a coloured Chip based on status
}

// The PARENT passes the prop value:
// File: frontend/src/pages/JobListPage.tsx
<JobStatusChip status={job.status} />
//             ^^^^^^^^^^^^^^^^^^^
//             parent sends the value; child reads it
```

---

## 4. State — Data That Changes and Triggers Re-renders

`useState` stores a value that, when changed, causes React to re-render the
component. Think of it as React's "memory" for a component.

```tsx
// File: frontend/src/pages/UploadPage.tsx

// Declare state: [currentValue, setterFunction]
const [selectedFile, setSelectedFile] = useState<File | null>(null)
const [uploadState, setUploadState] = useState<UploadState>({
  step: 'idle',
  progress: 0,
  jobId: null,
  error: null,
})

// Reading state: just use the variable
<Typography>{selectedFile?.name}</Typography>

// Writing state: call the setter — React re-renders automatically
function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
  const file = e.target.files?.[0] ?? null
  setSelectedFile(file)             // React re-renders UploadPage
  setUploadState({ step: 'selected', progress: 0, ... })
}
```

**Key rule:** Never mutate state directly (`selectedFile.name = "new"` does
NOT work). Always call the setter.

---

## 5. useEffect — Running Code After Render

`useEffect` runs side effects (API calls, timers, subscriptions) AFTER the
component renders. The dependency array `[...]` controls WHEN it re-runs.

```tsx
// File: frontend/src/pages/JobListPage.tsx

// Run once on mount (empty dependency array [])
useEffect(() => {
  fetchJobs()     // load job list from API when page first opens
}, [fetchJobs])

// Run every time `jobs` changes
useEffect(() => {
  if (hasActiveJobs(jobs)) {
    // Start a 5-second poll interval
    pollingRef.current = setInterval(() => fetchJobs(true), 5000)
  } else {
    clearInterval(pollingRef.current)   // stop polling when all jobs are done
    pollingRef.current = null
  }

  // CLEANUP FUNCTION — runs before the next effect AND on unmount
  return () => {
    clearInterval(pollingRef.current)   // cancel the interval when user navigates away
    pollingRef.current = null
  }
}, [jobs, fetchJobs])
//  ^^^^^^^^^^^^^^^ re-run this effect whenever `jobs` or `fetchJobs` changes
```

**Three forms:**
```tsx
useEffect(() => { ... })          // runs after EVERY render (dangerous — usually wrong)
useEffect(() => { ... }, [])      // runs ONCE after first render (mount)
useEffect(() => { ... }, [x, y]) // runs when x or y changes
```

---

## 6. React Router — Navigation Without Page Reloads

React Router maps URL paths to components. The browser never does a full
page load — React swaps components in and out.

```tsx
// File: frontend/src/App.tsx

const router = createBrowserRouter([
  // Public route — no auth required
  { path: '/login', element: <LoginPage /> },

  // Protected routes — wrapped in RequireAuth
  {
    element: (
      <RequireAuth>
        <AppLayout />       {/* shell: nav bar + Outlet */}
      </RequireAuth>
    ),
    children: [
      { index: true, element: <JobListPage /> },        // matches "/"
      { path: 'upload', element: <UploadPage /> },      // matches "/upload"
      { path: 'jobs/:jobId', element: <JobDetailPage /> }, // matches "/jobs/abc-123"
    ],
  },
])
```

**`<Outlet />`** in `AppLayout` is a placeholder where the matched child renders:

```
URL = /upload
  → AppLayout renders (nav bar visible)
     → Outlet renders UploadPage (the child that matches /upload)
```

**Navigating programmatically:**

```tsx
// File: frontend/src/pages/UploadPage.tsx

const navigate = useNavigate()

// After upload succeeds, go to the job detail page
setTimeout(() => navigate(`/jobs/${jobId}`), 800)
```

**URL parameters:**

```tsx
// File: frontend/src/pages/JobDetailPage.tsx

const { jobId } = useParams()   // reads "abc-123" from /jobs/abc-123
```

---

## 7. Context — Global State Without Prop Drilling

Context solves the problem of passing data through many component levels
("prop drilling"). Auth state needs to be accessible by every component —
Context makes it available anywhere in the tree without passing props.

```tsx
// File: frontend/src/auth/AuthContext.tsx

// Step 1: Create the context (a named slot that holds a value)
const AuthContext = createContext<AuthContextValue | null>(null)

// Step 2: Provide the value at the top of the tree
export function AuthProvider({ children }) {
  const [isAuthenticated, setIsAuthenticated] = useState(false)
  const [user, setUser] = useState<AuthUser | null>(null)

  const login = useCallback(async (email, password) => {
    // ... calls Cognito, sets tokens, updates state
  }, [])

  const logout = useCallback(() => {
    setAuthToken(null)
    setUser(null)
    setIsAuthenticated(false)
  }, [])

  const value = useMemo(
    () => ({ isAuthenticated, user, login, logout, isLoading }),
    [isAuthenticated, user, login, logout, isLoading],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

// Step 3: Consume from any component
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within <AuthProvider>')
  return ctx
}
```

**Used in AppLayout:**

```tsx
// File: frontend/src/components/AppLayout.tsx

const { user, logout } = useAuth()
// user.email and logout come from AuthContext — no props needed
```

**Without Context, you'd have to do:**

```tsx
// 😱 Prop drilling — passing auth through every level
<App user={user} logout={logout}>
  <AppLayout user={user} logout={logout}>
    <JobListPage user={user} logout={logout}>
      <SomeDeepComponent user={user} logout={logout} />
    </JobListPage>
  </AppLayout>
</App>
```

---

## 8. useCallback — Memoizing Functions

`useCallback` returns a memoized version of a function. Without it, the
function is recreated on every render, which can cause `useEffect` to
re-run unnecessarily.

```tsx
// File: frontend/src/pages/JobListPage.tsx

// WITHOUT useCallback:
//   fetchJobs is a new function object on every render.
//   useEffect depends on fetchJobs → fires on every render → infinite loop.

// WITH useCallback:
//   fetchJobs is the SAME function object between renders (unless deps change).
//   useEffect only re-fires when fetchJobs actually changes.

const fetchJobs = useCallback(async (silent = false): Promise<void> => {
  if (!silent) setIsLoading(true)
  try {
    const response = await listJobs()
    setJobs(response.jobs)
  } catch (err) {
    if (!silent) setError(err.message)
  } finally {
    if (!silent) setIsLoading(false)
  }
}, [])  // empty deps: never recreated
```

---

## 9. useRef — Mutable Values That Don't Trigger Re-renders

`useRef` stores a mutable value that persists across renders but does NOT
cause a re-render when it changes. Perfect for timers, DOM elements, and
infrastructure state.

```tsx
// File: frontend/src/pages/JobListPage.tsx

// useRef for the polling interval ID
const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null)

// Set the interval (no re-render)
pollingRef.current = setInterval(() => fetchJobs(true), 5000)

// Clear the interval (no re-render)
clearInterval(pollingRef.current)
pollingRef.current = null
```

```tsx
// File: frontend/src/pages/UploadPage.tsx

// useRef for the hidden file input element
const fileInputRef = useRef<HTMLInputElement>(null)

// Programmatically open the file picker
fileInputRef.current?.click()

// In JSX, attach the ref to the DOM element
<input ref={fileInputRef} type="file" style={{ display: 'none' }} />
```

**useState vs useRef:**

| | useState | useRef |
|---|---|---|
| Triggers re-render? | ✅ Yes | ❌ No |
| Use for | UI data (what user sees) | Infrastructure (timers, DOM refs) |
| Access | `value` | `ref.current` |

---

## 10. useMemo — Memoizing Computed Values

`useMemo` caches a computed value. Without it, the value is recomputed on
every render.

```tsx
// File: frontend/src/auth/AuthContext.tsx

// Without useMemo: a NEW object is created on every render.
// Every component that consumes AuthContext re-renders because the value
// reference changed — even if isAuthenticated, user, etc. didn't change.

const value = useMemo<AuthContextValue>(
  () => ({ isAuthenticated, isLoading, user, login, logout }),
  [isAuthenticated, isLoading, user, login, logout],
)
//  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
//  Only create a new object when one of these actually changes.
//  Auth state changes only on login/logout — not on every render.
```

---

## 11. Conditional Rendering — Showing UI Based on State

React renders UI based on JavaScript expressions. Use `&&` for
"show if true", ternaries for "show A or B".

```tsx
// File: frontend/src/components/AppLayout.tsx

// Show Admin link ONLY for admins
{user?.isAdmin && (
  <Button component="a" href="/admin">Admin</Button>
)}

// Show email ONLY if it exists
{user?.email && (
  <Typography>{user.email}</Typography>
)}
```

```tsx
// File: frontend/src/pages/UploadPage.tsx

// Three different states: uploading, done, error
{uploadState.step === 'done' && <SuccessView />}
{uploadState.step === 'error' && <ErrorView error={uploadState.error} />}
{uploadState.step !== 'done' && uploadState.step !== 'error' && <UploadForm />}
```

```tsx
// File: frontend/src/pages/JobListPage.tsx

if (isLoading) return <SkeletonView />    // show skeleton while loading
if (error)     return <ErrorView />       // show error if fetch failed
if (jobs.length === 0) return <EmptyState /> // show empty state
return <JobTable jobs={jobs} />           // show the list
```

---

## 12. Event Handling — Responding to User Actions

```tsx
// File: frontend/src/pages/UploadPage.tsx

// File input change
<input
  type="file"
  onChange={(e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0] ?? null
    setSelectedFile(file)
  }}
/>

// Drag and drop
<Box
  onDrop={(e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault()           // prevent browser default (opening the file)
    const file = e.dataTransfer.files[0]
    setSelectedFile(file)
  }}
  onDragOver={(e) => e.preventDefault()}   // required to allow drop
>
```

```tsx
// File: frontend/src/pages/UploadPage.tsx

// Keyboard accessibility
<Box
  role="button"
  tabIndex={0}
  onKeyDown={(e) => {
    if (e.key === 'Enter' || e.key === ' ') fileInputRef.current?.click()
  }}
>
```

---

## 13. The Upload Flow — All Concepts Working Together

```tsx
// File: frontend/src/pages/UploadPage.tsx

const handleUpload = useCallback(async (): Promise<void> => {
  if (!selectedFile || !user) return

  // --- State update #1 ---
  setUploadState({ step: 'creating', progress: 0, jobId: null, error: null })

  // --- API call #1: POST /jobs ---
  let jobId: string, uploadUrl: string
  try {
    const response = await createJob(user.email, selectedFile.name, selectedFile.size)
    jobId = response.job_id
    uploadUrl = response.upload_url
  } catch (err) {
    setUploadState({ step: 'error', error: err.message, ... })
    return
  }

  // --- State update #2 ---
  setUploadState({ step: 'uploading', progress: 0, jobId, error: null })

  // --- S3 direct upload (XHR, no auth header) ---
  try {
    await uploadFileToS3(uploadUrl, selectedFile, (percent) => {
      setUploadState((prev) => ({ ...prev, progress: percent }))
      //            ^^^^ functional update: based on previous state
    })
  } catch (err) {
    setUploadState({ step: 'error', ... })
    return
  }

  // --- API call #2: POST /uploads/{jobId}/complete ---
  await confirmUpload(jobId)

  // --- Navigate to the job detail page ---
  setUploadState({ step: 'done', progress: 100, jobId, error: null })
  setTimeout(() => navigate(`/jobs/${jobId}`), 800)
}, [selectedFile, user, navigate])
```

The stepper UI automatically tracks `uploadState.step`:
```tsx
function activeStep(): number {
  switch (uploadState.step) {
    case 'idle': case 'selected': return 0   // step 1 active
    case 'creating': case 'uploading': return 1  // step 2 active
    case 'done': case 'error': return 2      // step 3 active
  }
}
```

---

## Concept Quick Reference

| Concept | Hook/API | Use in CSVForge |
|---|---|---|
| **Component** | `function Foo()` | Every page and UI element |
| **State** | `useState` | `selectedFile`, `uploadState`, `jobs`, `isLoading` |
| **Side effects** | `useEffect` | Initial data fetch, polling interval |
| **Memoized fn** | `useCallback` | `fetchJobs`, `handleUpload`, `login`, `logout` |
| **Memoized value** | `useMemo` | AuthContext value object |
| **DOM/timer ref** | `useRef` | `pollingRef`, `fileInputRef` |
| **Global state** | `useContext` | Auth state via `useAuth()` |
| **Routing** | `createBrowserRouter` | Pages mapped to URL paths |
| **URL params** | `useParams` | `jobId` from `/jobs/:jobId` |
| **Navigation** | `useNavigate` | Redirect after successful upload |
| **Conditional render** | `&&`, ternary, early return | Loading/error/empty/data states |
