# ML image for the Kubeflow pipelines (ml/pipelines): Python 3.12, uv, the workspace with
# the "ml" dependency group (torch from the CPU-only index), ml/ and the data the steps read.
# Build: make ml-image   (docker build -f docker/ml.Dockerfile -t o2c-ml:dev .)
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv

ENV UV_HTTP_TIMEOUT=300 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services ./services
COPY prompts ./prompts

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-packages --group ml

# Pipeline code and data: master data, labelled history outliers, the golden POs
# (poc/data/ground_truth) and ml/data/holdout for the false-alarm rate.
COPY ml ./ml
COPY poc/data/master_data.json poc/data/history_outliers.json poc/data/customer_profiles.json ./poc/data/
COPY poc/data/ground_truth ./poc/data/ground_truth

# KFP v2 stages artifacts under /minio, /gcs and /s3 inside the container; a non-root
# user cannot create them, so they exist up front.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /minio /gcs /s3 \
    && chown 10001 /minio /gcs /s3

USER 10001
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/ml" \
    GIT_PYTHON_REFRESH=quiet
