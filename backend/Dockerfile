FROM ghcr.io/astral-sh/uv:0.10.2 AS uv
FROM python:3.12.14-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    KEYSTONE_HOST=0.0.0.0
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
RUN uv sync --locked --no-dev --no-editable \
    && useradd --create-home --uid 10001 keystone \
    && mkdir -p /data/files && chown 10001:10001 /data/files
USER keystone
EXPOSE 8000
CMD ["keystone-api"]
