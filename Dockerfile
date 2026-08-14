# Stage 1: Build virtual environment using uv
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy dependency definition files first for optimal layer caching
COPY pyproject.toml uv.lock .python-version ./

# Install production dependencies into /app/.venv using uv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Copy application source code
COPY app ./app

# Stage 2: Runtime image using python:3.12-slim
FROM python:3.12-slim AS runtime
WORKDIR /app

# Create non-root system user and group for security
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/bash -m appuser

# Copy virtual environment and app code from builder stage
COPY --from=builder --chown=appuser:appgroup /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appgroup /app/app /app/app

# Set virtual environment in PATH
ENV PATH="/app/.venv/bin:$PATH"

# Switch to non-root user
USER appuser:appgroup

EXPOSE 8000

ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
