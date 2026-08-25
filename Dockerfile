FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# uv/uvx so the Alpaca MCP server can be launched from inside the container
RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e '.[all]'

COPY config ./config
COPY scripts ./scripts

RUN mkdir -p runs data/cache

EXPOSE 8080

# Default: the autonomous loop. Override to serve the dashboard:
#   docker run ... oaa:latest oaa serve
CMD ["oaa", "run"]
