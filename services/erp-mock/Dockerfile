FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services ./services
RUN uv sync --frozen --no-dev --package erp-mock

RUN useradd --create-home --uid 10001 app
USER 10001
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8100
CMD ["sh", "-c", "alembic -c services/erp-mock/alembic.ini upgrade head && uvicorn erp_mock.api:app --host 0.0.0.0 --port 8100"]
