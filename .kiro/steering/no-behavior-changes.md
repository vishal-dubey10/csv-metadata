# No Behavior Changes — CSVForge Learning Codebase

## Context

This is an active learning codebase where **tests are the source of truth**. The
codebase is intentionally stable so that learners can read the code, understand it,
and trust that the tests reflect real behavior — not tests that were written to match
recent refactoring.

All documentation work must leave the runtime behavior **completely unchanged**.

---

## DO NOT CHANGE

### APIs
- Function signatures (parameter names, parameter order, types)
- Return types and return values
- HTTP endpoint paths, methods, and response shapes

### Database schema
- Column names, SQL types, and nullability constraints
- Table names and `__tablename__` values
- Index names and the columns they cover
- `server_default` values in migration files

### SQL and ORM behavior
- SQL queries or their logic
- `WHERE` clause conditions
- `ORDER BY` expressions
- `synchronize_session` values in `update()` calls
- Transaction boundaries (where `flush()`, `commit()`, `rollback()` are called)

### Dependencies
- Package versions in `requirements.txt`, `requirements-dev.txt`, or `worker/requirements.txt`
- Do not add or remove packages from any requirements file

### Test behavior
- Test function names (even if misleading — the name may be intentionally documenting
  surprising behavior; see `test_second_claim_returns_false` in test_job_repository_unit.py)
- Assertion values
- Fixture names and `scope` settings
- Fixture behavior (what they set up and tear down)
- What tests are skipped and why

---

## DO NOT

- Refactor working implementation code
- Rename functions, classes, variables, or files
- Introduce new abstractions, base classes, or helper utilities
- "Clean up" working code (extract methods, simplify conditionals, etc.)
- Add new imports to existing Python files
- Remove existing imports from existing Python files
- Change the order of class attributes or function definitions
- Move code between files

---

## Verification

After **any** change to a Python file, run the unit test suite:

```bash
cd /Users/vishal/csv-metadata && PYTHONPATH=. .venv/bin/pytest tests/unit/ -v
```

**All 53 tests must pass.** If any test fails after a documentation-only change, the
change introduced a Python syntax error or altered something behavioral. Revert and
investigate before continuing.

The test suite is the fastest way to confirm you haven't accidentally changed behavior.
New `.md` files do not affect Python behavior, but verify anyway after any Python edit.

---

## Decision Rule

**When in doubt: add a comment, don't change the code.**

If a function is confusing but correct, add a comment explaining why it is correct.
Do not rewrite it to be clearer — clarity through comments, stability through
unchanged code.
