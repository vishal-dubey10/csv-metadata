# ─────────────────────────────────────────────────────────────
# CSVForge — API Dockerfile
#
# WHAT:
#   A single-stage Docker image that runs the FastAPI application
#   under Uvicorn with hot-reload enabled for local development.
#
# WHY SINGLE-STAGE:
#   Multi-stage builds are used when you want to compile/build in one
#   image and copy only the output artifacts to a smaller runtime image
#   (common for Go, Rust, or compiled Java). Python has no compilation
#   step — the source is the artifact — so a single stage is sufficient.
#
# PRODUCTION DIFFERENCE:
#   In production, the API runs inside AWS Lambda wrapped by Mangum
#   (see AD-06 in docs/DECISIONS.md). There is no uvicorn process —
#   Lambda invokes the Mangum handler directly. The Lambda deployment
#   package is a zip file, not a Docker image. This Dockerfile is for
#   local development only.
#
# LAYER CACHING STRATEGY:
#   Docker builds images in layers. Each instruction (FROM, COPY, RUN, etc.)
#   creates a new layer. Layers are cached: if a layer's inputs haven't
#   changed since the last build, Docker reuses the cached layer instead
#   of re-running the instruction.
#
#   KEY RULE: copy files that change infrequently BEFORE files that change often.
#
#   This Dockerfile copies requirements.txt FIRST, then runs pip install,
#   then copies source code. Why?
#     - requirements.txt changes rarely (only when you add/remove packages)
#     - source code changes frequently (every edit)
#   Result: `docker compose build` after editing source code skips the
#   pip install layer entirely — the cached layer is reused. A full rebuild
#   (including pip install) only happens when requirements.txt changes.
#
#   If we copied source code first and then installed requirements, every
#   source code edit would invalidate the pip install cache and force a
#   full re-download of all packages. Slow.
# ─────────────────────────────────────────────────────────────

# FROM: choose the base image.
#
# python:3.14-slim is the official Python image with a minimal Debian OS.
# "slim" = the OS contains only what Python needs to run — no build tools,
# compilers, documentation, or extra utilities. This keeps the image small.
#
# WHY python:3.14-slim and not python:3.14?
#   python:3.14 (without -slim) includes GCC, make, and other build tools.
#   Useful if you have packages that compile C extensions (some packages do).
#   psycopg[binary] uses a pre-compiled binary wheel so it doesn't need GCC.
#   Slim is enough and produces a smaller image.
#
#   SIZE COMPARISON:
#     python:3.14        ~900MB  (full Debian with compiler toolchain)
#     python:3.14-slim   ~130MB  (minimal Debian, Python runtime only)
#
#   DOWNSIDE of slim: if a pip package needs to compile C extensions at
#   install time, the build will fail because GCC and build headers are
#   absent. You'd need to add `RUN apt-get install -y build-essential` before
#   the pip install step. All packages in requirements.txt use pre-compiled
#   binary wheels (the `[binary]` suffix on psycopg is a key example), so
#   slim works here without any extra OS packages.
#
# WHY NOT Alpine Linux (python:3.14-alpine)?
#   Alpine uses musl libc instead of glibc. Many Python binary wheels are
#   compiled against glibc and are not compatible with Alpine, forcing
#   compilation from source. This often causes more problems than it solves.
#   Slim (Debian-based) is the better production-safe choice.
FROM python:3.14-slim

# WORKDIR: set the working directory inside the container.
#
# All subsequent instructions (COPY, RUN, CMD) are relative to this path.
# It's also the current directory when the container starts.
#
# WHY /app?
#   Convention. It's short, clearly named, and won't conflict with OS paths.
#   Using the root / or /home would be unconventional and error-prone.
#   WORKDIR creates the directory automatically if it doesn't exist.
WORKDIR /app

# ── Layer 1: Install dependencies ────────────────────────────
# COPY requirements.txt BEFORE copying source code.
# This is the layer caching strategy described above:
#   - requirements.txt rarely changes → this COPY + RUN layer is cached
#   - Source code changes frequently → only the later COPY . . layer rebuilds
#
# If requirements.txt is unchanged since the last build, Docker skips
# the RUN pip install step entirely (uses the cached layer). Saving
# potentially minutes of package download time on every code change.
COPY backend/requirements.txt requirements.txt

# pip install --no-cache-dir:
#   --no-cache-dir tells pip NOT to store downloaded packages in pip's local
#   cache directory (~/.cache/pip). Normally pip caches downloaded wheel files
#   so re-installs are faster. But in a Docker build:
#     - The cache would be written into the image layer (increasing image size)
#     - The next `docker build` starts a fresh layer — the cache is never reused
#   Skipping the cache saves image size with no downside in a Docker context.
#
# Why NOT separate `pip install` calls per package?
#   One RUN instruction = one Docker layer. Fewer layers = smaller image.
#   All packages installed in a single RUN keep the image lean.
RUN pip install --no-cache-dir -r requirements.txt

# ── Layer 2: Copy source code ─────────────────────────────────
# Copy the backend Python source code into /app/backend/ inside the container.
# This happens AFTER pip install so source code changes don't bust the
# pip cache (see LAYER CACHING STRATEGY above).
#
# NOTE: docker-compose.yml mounts ./backend as a volume at /app/backend.
# This means during `docker compose up`, the container's /app/backend is
# the host's ./backend directory — edits on the host are instantly visible
# inside the container without rebuilding. The COPY here ensures the image
# is self-contained if run without docker-compose.
COPY backend/ ./backend/

# Alembic config lives at the project root and references backend/alembic/
# (script_location = backend/alembic in alembic.ini).
# Copy it so alembic commands work inside the container.
COPY alembic.ini ./alembic.ini

# ── Environment variables ─────────────────────────────────────
# ENV PYTHONPATH=/app tells Python to search /app when resolving imports.
#
# WHY is this needed?
#   The application imports backend modules as `from backend.xxx import yyy`.
#   For Python to find `backend`, the directory CONTAINING `backend/` must
#   be on sys.path. PYTHONPATH=/app adds /app to sys.path.
#   Without this: `from backend.db.connection import get_db` would fail with
#   ModuleNotFoundError: No module named 'backend'
#   because Python looks in the current directory (/app/backend) and standard
#   library paths, but not in /app (the parent of backend/).
#   PYTHONPATH=/app makes `backend.xxx` imports work correctly.
#
# Note: docker-compose.yml also injects runtime env vars (DATABASE_URL,
# ENVIRONMENT) which are NOT baked into the image — they are provided at
# container start time and can vary between environments.
ENV PYTHONPATH=/app

# ── CMD: the command that runs when the container starts ───────
# CMD provides the default command. It can be overridden at `docker run` time.
#
# CMD vs ENTRYPOINT:
#   ENTRYPOINT defines the command that ALWAYS runs — it cannot be overridden
#   by `docker run` arguments (only by --entrypoint flag). CMD provides default
#   arguments that CAN be replaced. Using CMD here means:
#     `docker run myimage bash`     → runs bash instead of uvicorn (useful for debugging)
#     `docker run myimage`          → runs uvicorn with all the default args
#
#   The split pattern — ENTRYPOINT ["uvicorn"] + CMD ["backend.main:app"] —
#   separates the executable from its arguments, allowing:
#     `docker run myimage backend.other:app`  → run a different app with the same uvicorn
#   This is useful for multi-app images. Using CMD with the full command is
#   the simpler approach for a single-purpose image like this one.
#
# Uvicorn is an ASGI server — it handles HTTP connections and passes requests
# to the FastAPI application. Think of it as the web server that sits in front
# of your Python code, the same way Nginx sits in front of a PHP application.
#
# --host 0.0.0.0
#   Listen on ALL network interfaces, not just 127.0.0.1 (localhost).
#   Why? Inside Docker, 127.0.0.1 means "this container's loopback."
#   Connecting from the host machine is not loopback from the container's
#   perspective. 0.0.0.0 binds to all interfaces, including the container's
#   virtual network interface — so host → container connections work.
#   Without this, the API would be unreachable from outside the container.
#
# --port 8000
#   Listen on port 8000. docker-compose.yml maps host:8000 → container:8000.
#
# --reload
#   Uvicorn watches source files for changes using the Watchfiles library.
#   When a .py file changes on disk, Watchfiles detects the inotify event and
#   signals uvicorn to restart the application automatically.
#   Combined with the `./backend:/app/backend` bind mount in docker-compose.yml,
#   this creates the dev loop: edit on host → bind mount reflects the change
#   inside the container → Watchfiles detects it → uvicorn restarts → new code
#   is live. No image rebuild required.
#
#   ONLY used in development. In production (Lambda), there is no uvicorn at all.
#   Lambda invokes the Mangum handler directly from the deployment zip package —
#   there is no live filesystem to watch.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ── WHY NO TEST DEPENDENCIES ──────────────────────────────────
# requirements-dev.txt (pytest, moto, httpx2) is NOT installed in this image.
# Two reasons:
#   1. Smaller image: test tools add ~100MB+ to the image size.
#   2. Production parity: the image should be as close as possible to
#      what would run in production (Lambda). Test tools don't belong there.
# Tests run on the host machine (or in a separate CI image) using the
# .venv virtual environment, not inside this Docker container.
