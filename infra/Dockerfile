# Tailwind is compiled at image-build time. No Tailwind JavaScript is shipped
# to the browser or required at runtime.
FROM node:22.14.0-alpine3.21 AS tailwind

WORKDIR /build
COPY frontend/tailwind.config.js frontend/tailwind.config.js
COPY frontend/src frontend/src
COPY app app

# Keep the CLI version explicit so CSS output is reproducible across builds.
RUN npm install --no-save --ignore-scripts tailwindcss@3.4.17 \
    && mkdir -p app/static \
    && ./node_modules/.bin/tailwindcss \
      --config frontend/tailwind.config.js \
      --input frontend/src/input.css \
      --output app/static/app.css \
      --minify \
    && cp frontend/src/htmx.min.js app/static/htmx.min.js

FROM python:3.12.8-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends tesseract-ocr=5.3.0-2 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.12.6

# Install locked application dependencies before copying source for better
# layer reuse. uv.lock is produced alongside pyproject.toml in Phase 0.
COPY pyproject.toml uv.lock alembic.ini ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY --from=tailwind /build/app/static ./app/static
COPY infra/idle-worker.sh /usr/local/bin/lifeagent-idle-worker
COPY infra/run-api.sh /usr/local/bin/lifeagent-run-api
RUN chmod 0755 /usr/local/bin/lifeagent-idle-worker /usr/local/bin/lifeagent-run-api

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
