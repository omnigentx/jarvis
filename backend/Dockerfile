# ─── Stage 1: System base (prebuilt image from Docker Hub) ───
# System deps (apt + npm + uv + gitnexus) live in the public base image so CI
# doesn't re-run apt-get / npm install on every build. Rebuild + bump the tag
# only when Dockerfile.base changes — see Dockerfile.base header for steps.
ARG BASE_IMAGE=felixnguyen95/jarvis-backend-base:1.1.0
FROM ${BASE_IMAGE} AS base

# ─── Stage 2: Python dependencies (cached unless pyproject/lock/fast-agent change) ───
FROM base AS deps
WORKDIR /app

# Copy fast-agent submodule first (rarely changes)
COPY fast-agent /app/fast-agent

# Copy dependency files
COPY pyproject.toml uv.lock ./

# 1. Strip uv.sources (host-only paths) so uv can resolve from PyPI
# 2. Remove fast-agent-mcp from deps (we install our fork instead)
# 3. Sync using lockfile, then install our fork editable
RUN sed -i '/\[tool\.uv\.sources\]/,/^$/d' pyproject.toml \
    && sed -i '/fast-agent-mcp/d' pyproject.toml \
    && uv sync --frozen --no-dev \
    && uv pip install -e ./fast-agent

# ─── Stage 3: Final runtime image ───
FROM base AS final
WORKDIR /app

COPY --from=deps /app/fast-agent /app/fast-agent
COPY --from=deps /app/.venv /app/.venv
RUN /app/.venv/bin/scrapling install

COPY . .

ENV PYTHONUNBUFFERED=1
ENV LOG_CONSOLE_LEVEL=INFO
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
