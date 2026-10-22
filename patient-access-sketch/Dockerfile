# One image for every sketch service; docker-compose.yml overrides the command.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /code

# Install dependencies first so code edits don't bust the layer. The package is installed
# editable; compose bind-mounts the directory over /code, so edits take effect without a rebuild.
COPY pyproject.toml README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -e ".[django,s3,dramatiq,dev]"

COPY . .

CMD ["python", "manage.py", "runserver", "0.0.0.0:8080"]
