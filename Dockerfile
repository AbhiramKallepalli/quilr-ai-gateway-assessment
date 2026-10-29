# One image for every HTTP service in this repo; docker-compose.yml picks the
# module each container runs. The stdio MCP server (Task 1) is not containerised:
# it talks over stdin/stdout, so an MCP client launches it directly.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN pip install --no-cache-dir uv \
    && useradd --create-home --uid 10001 app

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev \
    && mkdir -p /app/data && chown app:app /app/data

USER app
ENV PATH="/app/.venv/bin:$PATH"
