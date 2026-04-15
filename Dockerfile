FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for layer caching
COPY pyproject.toml .
RUN uv sync --no-dev --no-install-project

# Copy source code
COPY src/ src/

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "whitelabel_wpp.main:app", "--host", "0.0.0.0", "--port", "8000"]
