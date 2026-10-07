# Documentation Style — CSVForge Learning Codebase

## Context

This is a **learning codebase**. Every AI-generated change must include educational
comments. The primary audience is a developer who already knows Python but is learning
SQLAlchemy, FastAPI, PostgreSQL patterns, and AWS serverless architecture.

Comments are not optional polish — they are the core deliverable.

---

## Comment Philosophy: Explain WHY, Not WHAT

The code already says **what** it does. Comments should explain **why** the code is
written this way, what problem it solves, and what would happen if you did it differently.

### BAD — restating what the syntax says

```python
# Create engine
engine = create_engine(DATABASE_URL)

# Return result
return result

# Import library
import boto3
```

These comments add zero information. A reader who can read Python already knows what
the line does.

### GOOD — explaining the reasoning

```python
# The Engine is long-lived and created once per process (or Lambda container).
# Creating a new Engine per request would create a NEW connection pool per request,
# defeating the entire point of pooling. A connection pool pre-creates connections
# at startup and reuses them — each request borrows one, runs its queries, returns it.
engine = create_engine(DATABASE_URL, pool_size=1, pool_pre_ping=True)
```

The comment explains WHY the engine is module-level, WHY pool_size is 1, and what
would go wrong if you did it differently.

### The Engine / Session Analogy (concrete example of a GOOD comment)

The restaurant analogy in `connection.py` is a model to follow:
- It gives a mental model (Engine = restaurant manager, Pool = tables, Connection = one
  table occupied, Session = one customer's order).
- It explains the trade-off (one Session per request — why a long-lived Session would
  accumulate stale data).
- It connects to an interview concept (connection pooling, round-trip cost).

Every significant concept in this codebase deserves that same treatment.

---

## Required Module Docstring Structure

Every Python module (`.py` file) should have a module docstring with these sections:

```
"""
Module name — one-line description.

WHAT:
    What this module defines and provides.
    Keep to 2-5 bullet points.

WHY:
    Why this module exists as a separate unit.
    What would be worse if this code lived elsewhere.

ARCHITECTURE:
    A short diagram or prose description of where this module sits
    in the stack and what calls it / what it calls.

ANALOGY (optional but encouraged):
    A real-world analogy that helps build an accurate mental model.
    Label it clearly as an analogy, not a literal mapping.

IMPORTANT CONCEPTS:
    Key concepts a learner should know after reading this file.
    Bullet list of terms with one-sentence explanations each.

Interview concept (optional):
    One or two interview questions this module helps answer.
"""
```

---

## Required Function Docstring Structure

For non-trivial functions, use this structure:

```
"""
One-sentence summary.

WHAT:
    What this function does.

WHY:
    Why it does it this way. What problem does this solve?

HOW (execution order):
    Numbered steps describing what happens when the function runs.
    e.g.:
        1. db = SessionLocal() — create a new session
        2. yield db — hand session to the route handler
        3. db.commit() — make all writes durable on success
        4. db.rollback() — undo all writes on exception
        5. db.close() — return connection to the pool (always runs)

Inputs:
    param_name: what it represents, valid values, what None means

Output:
    What the return value means. What None/False/True each signify.

Side effects:
    Database reads, writes, external API calls, or mutations to shared state.

Edge cases:
    What happens in unusual or error conditions.

Analogy (where helpful):
    A real-world parallel to make the concept stick.

Interview connection (where relevant):
    "What concept does this function illustrate?"
"""
```

Not every function needs every section. A simple getter may only need a one-liner.
Use judgment: add depth proportional to the concept's difficulty.

---

## Inline Comment Style

- Use `#` for inline block comments (above or beside a line).
- Use `"""` for module and function docstrings.
- Inline comments should be full sentences (capital first letter, period at end).
- Group related comments together; don't scatter one-liner observations.

### Example of a well-commented inline block

```python
# expire_on_commit=False:
#   SQLAlchemy's default behaviour after db.commit() is to mark every attribute
#   of every loaded ORM object as stale ("expired"). The next attribute access
#   would trigger a SELECT to reload from the DB — which fails if the session
#   is already closed (DetachedInstanceError).
#   Setting this to False keeps in-memory values readable after commit, which
#   is safe in a one-session-per-request pattern (there is no "next request"
#   that could see stale data from this session).
expire_on_commit=False,
```

---

## Forbidden Comments

Do not write these — they restate the syntax and add no information:

| Forbidden | Because |
|-----------|---------|
| `# Create engine` | The function name says create_engine |
| `# Return result` | The reader can see `return result` |
| `# Import library` | The import statement is self-explanatory |
| `# Define class` | Obvious from `class` keyword |
| `# Add to session` | Obvious from `db.add(job)` |
| `# Check if None` | Obvious from `if x is None:` |
| `# Loop through items` | Obvious from `for item in items:` |

---

## Concept Linking

When a concept is fully explained in one file, later files do not need to re-explain
it from scratch — they can cross-reference:

> "See `connection.py` for the full Engine / Session / Pool analogy."

> "See `job_repository.py` for the expire_all() / identity map explanation."

> "See `conftest.py` for why ARRAY(TEXT).with_variant() is needed in tests."

This keeps documentation DRY while ensuring every concept is fully explained somewhere.

---

## Test Assertions

Do **NOT** add comments inside test assertions. Assertions are self-documenting code:

```python
# BAD
assert response.status_code == 200  # check that health endpoint returns 200

# GOOD — the assertion is already readable
assert response.status_code == 200
```

Add comments above test classes and functions to explain **what scenario** is being
tested, **why** it is interesting or non-obvious, and **what the test would catch if
it failed**. The assertions themselves need no narration.
