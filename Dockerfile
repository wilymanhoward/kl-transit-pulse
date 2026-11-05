FROM python:3.12-slim

# uv installs the exact versions pinned in uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv

WORKDIR /srv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/srv/.venv/bin:$PATH"

# Dependencies first, as their own layer: code edits don't trigger a reinstall.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

RUN useradd --create-home appuser
USER appuser
