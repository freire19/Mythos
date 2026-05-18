# Mythos — Docker image (originally Alpha_Code H3 #15, ported to Mythos).
#
# Two-stage build:
#   1. `builder` produces a wheel from the current source tree.
#   2. `runtime` (slim) installs the wheel + minimal system deps.
#
# Why a wheel build instead of `pip install -e .`:
#   - Editable installs would require the source tree at runtime, defeating
#     the point of a deployable image.
#   - The wheel exercises the same code path users hit with `pipx install`
#     so any package-data drift (H3 #13) surfaces here too.
#
# Usage:
#   docker build -t mythos .
#   docker run --rm -it \
#     -v "$PWD:/workspace" \
#     -e DEEPSEEK_API_KEY \
#     mythos "audit this codebase for vulns"
#
# Mounting the host workspace into `/workspace` lets Alpha read/write
# the user's project. The container's own filesystem is ephemeral
# scratch — anything Alpha writes outside the mount disappears at exit.

# ─── Stage 1: build the wheel ──────────────────────────────────────

FROM python:3.12-slim AS builder

WORKDIR /build

# Install `build` (PEP 517 frontend). Pinned major version: 1.x is the
# current stable line; 2.x doesn't exist yet but the cap keeps a future
# release-with-breaking-changes from silently breaking image builds.
RUN pip install --no-cache-dir 'build>=1.0,<2.0'

# Copy only what the build needs. `.dockerignore` keeps tests, .git,
# .venv, etc. out so the build context stays small.
COPY pyproject.toml README.md LICENSE ./
COPY alpha/ ./alpha/
COPY main.py ./

RUN python -m build --wheel --outdir /build/dist


# ─── Stage 2: runtime ──────────────────────────────────────────────

FROM python:3.12-slim AS runtime

# Minimal system deps:
# - git: alpha's git_operation tool and the project_context loader
# - ca-certificates: TLS for outbound HTTPS (LLM providers)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Non-root user so the agent can't trivially write to system paths
# even if a tool slips past the approval gate.
RUN useradd --create-home --shell /bin/bash --uid 1000 mythos

WORKDIR /workspace
RUN chown mythos:mythos /workspace

COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

USER mythos

# Disable color when stdout isn't a TTY so piped output stays grep-friendly.
# Mythos already checks `sys.stdout.isatty()` via supports_color, this is
# just a belt-and-suspenders default for non-interactive `docker run`.
ENV PYTHONUNBUFFERED=1 \
    NO_COLOR=

ENTRYPOINT ["mythos"]
CMD ["--help"]
