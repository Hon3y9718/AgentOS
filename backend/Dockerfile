# Builder stage: has uv and build tooling, none of which belongs in the
# runtime image (WHY multi-stage — smaller attack surface and image size).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# Dependencies first, app code second: WHY — this layer only invalidates when
# pyproject.toml/uv.lock change, so editing app code doesn't re-download deps.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY app ./app
RUN uv sync --frozen --no-dev

# Runtime stage: no uv, no compilers, no lockfile — just the venv and the app.
FROM python:3.12-slim-bookworm AS runtime

# WHY a non-root user: the default root user in a container can write
# anywhere in its own filesystem and, combined with a container-escape bug,
# anywhere on the host. Running as an unprivileged user limits the blast
# radius of a dependency RCE to what appuser can touch, which is nothing.
RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/app /app/app

ENV PATH="/app/.venv/bin:${PATH}"
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
