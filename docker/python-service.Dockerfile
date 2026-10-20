FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services ./services
COPY prompts ./prompts

ARG PACKAGE
RUN uv sync --frozen --no-dev --package ${PACKAGE}

RUN useradd --create-home --uid 10001 app
USER 10001
ENV PATH="/app/.venv/bin:$PATH"
