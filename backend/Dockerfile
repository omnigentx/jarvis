# ─── Stage 1: System base (prebuilt image from Docker Hub) ───
# System deps (apt + npm + uv + gitnexus) live in the public base image so CI
# doesn't re-run apt-get / npm install on every build. Rebuild + bump the tag
# only when Dockerfile.base changes — see Dockerfile.base header for steps.
ARG BASE_IMAGE=omnigentx/jarvis-backend-base:1.2.0
FROM ${BASE_IMAGE} AS base

# ─── Stage 2: Python dependencies (cached unless pyproject/lock/fast-agent change) ───
FROM base AS deps
WORKDIR /app

# Copy submodules first (rarely change). Voice forks ship our omnigentx
# customisations to RealtimeSTT/RealtimeTTS — installed editable below.
# Submodule directory names are intentionally NOT 'RealtimeSTT' /
# 'RealtimeTTS' so the source tree at cwd doesn't shadow the actual
# Python packages via PEP 420 namespace package resolution.
COPY fast-agent /app/fast-agent
COPY realtimestt_src /app/realtimestt_src
COPY realtimetts_src /app/realtimetts_src

# Copy dependency files
COPY pyproject.toml uv.lock ./

# 1. Strip uv.sources (host-only paths) so uv can resolve everything else
#    from PyPI / the lockfile.
# 2. Remove submodule-driven deps from the constraint list — they're installed
#    editable below so the forks beat any stock PyPI release.
# 3. Sync the remainder using the lockfile, then install our forks editable
#    (extras for realtimetts mirror the dev-time pyproject extras).
RUN sed -i '/\[tool\.uv\.sources\]/,/^$/d' pyproject.toml \
    && sed -i '/fast-agent-mcp/d;/^  "realtimestt/d;/^  "realtimetts/d' pyproject.toml \
    && uv sync --frozen --no-dev \
    && uv pip install -e ./fast-agent \
    && uv pip install -e ./realtimestt_src \
    && uv pip install -e './realtimetts_src[edge,system,azure,elevenlabs,openai]'

# ─── Stage 3: Final runtime image ───
FROM base AS final
WORKDIR /app

COPY --from=deps /app/fast-agent /app/fast-agent
COPY --from=deps /app/realtimestt_src /app/realtimestt_src
COPY --from=deps /app/realtimetts_src /app/realtimetts_src
COPY --from=deps /app/.venv /app/.venv
RUN /app/.venv/bin/scrapling install

COPY . .

ENV PYTHONUNBUFFERED=1
ENV LOG_CONSOLE_LEVEL=INFO
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
