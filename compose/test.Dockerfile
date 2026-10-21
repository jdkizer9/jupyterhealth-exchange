# Test-runner image for docker-compose.test.yml: the app's locked dependencies plus the
# dev group (pytest, pytest-django, coverage...). Dev-only; production uses ../Dockerfile.
#
# Keep the Python version and system packages in step with ../Dockerfile, which installs
# with --no-dev and so can't run the tests itself.
ARG PYTHON_VERSION=3.11-slim-trixie

FROM python:${PYTHON_VERSION}

RUN apt-get -y update \
 && apt-get -y install --no-install-recommends postgresql-client git \
 && rm -rf /var/lib/apt/lists/*

# Same uv as ../Dockerfile. uvx is used by scripts/dev/check.sh to run ruff.
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /usr/local/bin/

# The repo is bind-mounted at /code, so a venv at /code/.venv would be hidden (or swapped
# for your host's). UV_PROJECT_ENVIRONMENT keeps the container's venv outside the mount.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /code

# Only the lock files: the code comes from the bind mount at run time, so this layer
# rebuilds only when dependencies change.
COPY pyproject.toml uv.lock /code/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project
