# Minimal container for trying the harness or running it in CI. Not a
# production/hosted deployment image -- see docs/containers.md.
#
# Base: the middle of the range pyproject.toml declares support for
# (requires-python = ">=3.11", classifiers list 3.11/3.12/3.13). Bump this
# alongside pyproject.toml, not ahead of it.
FROM python:3.12-slim

# Keeps pip from writing __pycache__ into the image and makes failures show
# up immediately in build logs instead of buffered at the end.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Config env, not secrets: these tell the package where its project root and
# state tree are inside the container. See src/coordharness/config.py for
# what each one overrides and docs/containers.md for the container-specific
# rationale.
ENV COORD_PROJECT_ROOT=/workspace \
    COORD_HOME=/var/lib/coordharness \
    COORD_DEPLOYMENT_PROFILE=generic

WORKDIR /workspace

# The full checkout, filtered by .dockerignore (no .git, .venv, local
# databases, or build output). The package has no pinned lockfile, so this
# copies pyproject.toml and src/ together rather than trying to cache a
# dependency-only layer -- `dependencies = []` in pyproject.toml means there
# is nothing to prefetch before the source lands anyway.
COPY . /workspace

# mcp: the coord-mcp entry point needs it to run at all. dev: pytest/ruff,
# so the same image can run the test suite in CI without a second install
# step.
RUN pip install --no-cache-dir '.[mcp,dev]'

# Container-local state directory, created before anything writes to it
# (config.py resolves the path but deliberately never creates it -- see the
# state_dir() docstring). Owned by a non-root user for the same reason any
# long-running network service should not run as root.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin coordharness \
    && mkdir -p "$COORD_HOME" \
    && chown -R coordharness:coordharness /workspace "$COORD_HOME"

USER coordharness

# Seed the demo board at build time so the container is useful the moment it
# starts, with no first-run step the person opening it has to remember. The
# seeder is deterministic (a fixed synthetic clock, see src/coordharness/demo.py)
# so this produces the same board on every build.
RUN coord demo --quiet

# The board's default port (src/coordharness/board/server.py DEFAULT_PORT).
# docs/containers.md documents where this constant lives so the two cannot
# silently drift.
EXPOSE 7870

# Bind 0.0.0.0, not the loopback default: a process bound to 127.0.0.1
# inside the container is unreachable through Docker's published-port NAT,
# which connects to the container's own address, not its loopback. This is a
# container-only relaxation of the board's loopback-by-default posture -- see
# docs/containers.md for why that is still safe here and not a general
# recommendation.
CMD ["coord-board", "--host", "0.0.0.0", "--allow-remote", \
     "--allowed-host", "localhost", "--allowed-host", "127.0.0.1"]
