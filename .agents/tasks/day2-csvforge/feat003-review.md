# Lambda worker handler for CSV processing pipeline

The handler wires together four subsystems — SQS parsing, PostgreSQL job state, S3 file I/O, and csv_processor — in a precise order mandated by two correctness properties: the AD-14 S3-before-DB ordering, and the security invariant that `user_id` is always read from the DB record rather than the SQS message body. All five required invariants are correctly implemented and each is guarded by a dedicated test. The module docstring is comprehensive, covering architecture, pipeline ordering, idempotency, cold-start behaviour, and the permanent-vs-transient failure distinction.

Watch for: (1) `__len__()` called explicitly instead of `len()` on `clean_result.cleaned_rows` — cosmetic but unusual; (2) `import os as _os` buried inside the function body at step 11 rather than at module top; (3) no test for the `db_job is None` defensive branch after a successful claim; (4) the `fail_job` positional-argument assertion in `TestInvalidCSV` checks `call_args[0]` (positional) while the handler passes `(db, job_id, message)` — this is correct as written, but confirmation is warranted.

**Verdict**: APPROVED

---

## High-level view

All five required invariants have clear, confirmed implementations. The `user_id` sourced from `db_job.user_id` appears at step 9 in `_process_record`, and the SQS body's `user_id` is never read. The S3 upload at step 10 precedes `complete_job` at step 11 unconditionally — the only path that reaches `complete_job` has already called `upload_file`. `InvalidCSVError` is caught inside an inner `try` block that returns without re-raising; every other exception falls through to the outer `except Exception` block, which calls `fail_job` and re-raises. The `claim_job_for_processing → False` path returns immediately before any file or CSV operations.

The module docstring is thorough to the point of being an educational document. It covers architecture, pipeline ordering with numbered steps, both invariants with their rationale, idempotency analysis, cold-start behaviour, and the permanent-vs-transient failure distinction. This satisfies the docstring approval criterion.

The test suite is well-structured: `TestHappyPath` exercises the real `parse_and_analyse` and `clean` via moto S3 (no mocking of the csv_processor), which catches real integration bugs that a pure-mock test would miss. The security test in `TestUserIdSource` is explicit about what would break and asserts both the positive (`db-user-id` present) and negative (`sqs-user-id` absent) cases. The `TestTerminalStateJob` class patches `parse_and_analyse`, `clean`, and `write_csv` individually to confirm none are reached, rather than just checking that `complete_job` wasn't called.

Two minor code-quality issues exist and are worth a quick fix before this merges: a buried module-level import and an unusual `.__len__()` call. Neither is a correctness bug.

---

<details>
<summary>Issues (3)</summary>

1. **`import os as _os` inside function body** — `os` is already imported at module top. The redundant in-function import at step 11 (`import os as _os; output_file_size = _os.path.getsize(output_path)`) is dead weight and visually suggests the module-level `os` can't be used here (it can). Replace with `os.path.getsize(output_path)` directly. (confirmed)

2. **`clean_result.cleaned_rows.__len__()`** — `row_count=clean_result.cleaned_rows.__len__()` is an explicit dunder call; the idiomatic form is `len(clean_result.cleaned_rows)`. No behaviour difference, but it reads as unintentional. (confirmed)

3. **`db_job is None` branch is untested** — The defensive guard after `claim_job_for_processing` succeeds (`if db_job is None: return`) has no test coverage. The branch is described as "should be impossible" but it silently skips processing with no `fail_job` call, leaving the job stuck in `processing` state indefinitely. If it's truly unreachable, a `raise AssertionError` would surface bugs faster than a silent return. If it's considered reachable, `fail_job` should be called before returning, and a test should cover it. (confirmed — no test exists, and the silent-return path leaves the job stuck)

</details>

---

<details>
<summary>Details</summary>

### Security invariant: user_id sourced from DB record

The constructed S3 key at step 9 is `f"processed/{db_job.user_id}/{job_id}/result.csv"`. The SQS message is parsed into `message["job_id"]` and `message["s3_key"]`; `message.get("user_id")` is never accessed anywhere in the handler. `TestUserIdSource` asserts both the positive (`db-user-id` in the key) and negative (`sqs-user-id` absent), and the same check appears redundantly in `TestHappyPath.test_processed_s3_key_uses_db_user_id` — belt-and-suspenders coverage on the most sensitive invariant.

### AD-14: S3 upload ordering

`upload_file` (step 10) and `complete_job` (step 11) are sequential in the same `try` block. No test explicitly verifies the upload-before-complete ordering by inspecting call order (e.g., `mock.call_args_list`), but the code structure makes it impossible to violate without also breaking the happy-path test.

### InvalidCSVError: permanent failure path

`InvalidCSVError` is caught in an inner `try/except` wrapping only `download_file` and `parse_and_analyse`. The outer `except Exception` cannot catch it after the inner handler returns. `TestInvalidCSV` uses the real `parse_and_analyse` (no mock) via a moto S3 bucket with an empty body — testing actual integration, not just mock wiring.

The assertion in `test_fail_job_called_and_no_exception_raised` indexes `call_args[0]` (positional) at `[1]` for `job_id` and `[2]` for the reason string, matching the handler's `fail_job(db, job_id, e.message)` call. This is correct, but fragile — `call_args.kwargs` or checking keyword arguments would survive a signature change.

### Transient failure path: re-raise behaviour

The outer `except Exception` block calls `fail_job`, then commits inside its own nested `try/except` — so a DB failure during `fail_job` doesn't suppress the original exception. `raise` is unconditional. `TestTransientFailure` verifies both that the exception propagates and that the error message string reaches `fail_job`.

### Claim guard: early exit on False

`TestTerminalStateJob` has two tests: the first checks that `download_file`, `upload_file`, `complete_job`, and `fail_job` are not called; the second patches `parse_and_analyse`, `clean`, and `write_csv` directly to verify none are reached. Together these cover the full early-return contract.

### Code quality gaps

`import os as _os` appears inside `_process_record` at step 11, after `upload_file` returns. `os` is already imported at the top of the module; the in-function alias adds confusion without purpose. Replace with `os.path.getsize(output_path)`.

`clean_result.cleaned_rows.__len__()` is the equivalent of `len(clean_result.cleaned_rows)` but calls the dunder directly — reads as accidental, possibly a copy-paste artifact.

### Untested defensive branch

After `claim_job_for_processing` returns `True`, the handler calls `get_job_by_id` and guards with `if db_job is None: logger.error(...); return`. This branch returns without calling `fail_job`, which leaves the job in `processing` state with no failure reason recorded — effectively stuck. The docstring acknowledges this is "should be impossible (claim succeeded, so the row exists)" but the defensive guard exists precisely for impossible cases. A silent `return` in this branch is worse than raising: a stuck `processing` job is harder to diagnose than an exception in CloudWatch logs. Worth either converting to `raise AssertionError(...)` or, if this branch is treated as reachable, calling `fail_job` before returning and adding a test for it.

</details>

---

<details>
<summary>File map</summary>

| File | Change |
|---|---|
| `worker/handler.py` | New Lambda handler: full 12-step CSV processing pipeline with module docstring, two env-var accessors, `lambda_handler`, `_process_record`, and local test entry point |
| `tests/unit/test_worker_handler.py` | New unit test module: five test classes covering happy path, invalid CSV, transient failure, terminal-state claim, and user_id security invariant |

Full diff: `git diff main -- worker/handler.py tests/unit/test_worker_handler.py`

</details>
