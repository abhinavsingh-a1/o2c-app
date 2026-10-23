# syntax=docker/dockerfile:1
# All Python services (PACKAGE build-arg). The classifier and the agent get their model
# baked in (ADR-016): MODEL_REF is the pinned artifact of models.lock.json
# (repository@digest), pulled with oras into /app/models/<model name>. Without MODEL_REF
# (the other services, local Compose and PR builds) /app/models stays empty and the
# services fall back to MLflow.

FROM ghcr.io/oras-project/oras:v1.3.4@sha256:f7bc056d54d97baa399414ed5048ecc67c3371b750d4bbce1d871827a5758179 AS model
ARG MODEL_REF=""
# Optional registry token as a BuildKit secret: passed to oras on stdin, never stored
# in a layer or the build cache. Public packages need none.
RUN --mount=type=secret,id=registry_token,required=false \
    set -eu; mkdir -p /models; \
    if [ -n "$MODEL_REF" ]; then \
      case "$MODEL_REF" in *@sha256:*) ;; *) echo "MODEL_REF must be pinned by digest" >&2; exit 1;; esac; \
      if [ -s /run/secrets/registry_token ]; then \
        oras pull --no-tty --username oauth2 --password-stdin -o /models "$MODEL_REF" < /run/secrets/registry_token; \
      else \
        oras pull --no-tty -o /models "$MODEL_REF"; \
      fi; \
      test -n "$(find /models -mindepth 2 -maxdepth 2 -name MLmodel)" || { echo "no MLflow model in $MODEL_REF" >&2; exit 1; }; \
    fi

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv

ENV UV_HTTP_TIMEOUT=120 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services ./services
COPY prompts ./prompts

ARG PACKAGE
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package ${PACKAGE}

RUN useradd --create-home --uid 10001 app
# Read-only for the service user (root-owned); empty unless MODEL_REF was set
COPY --from=model /models /app/models
USER 10001
ENV PATH="/app/.venv/bin:$PATH"
