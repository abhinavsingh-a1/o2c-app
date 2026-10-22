# ADR-015: CI gates on pull requests

Date: 2026-10-22
Status: Accepted

## Context
Every rule so far was checked by hand: lint, tests, `make eval`, `make dbt-build`. A PR
could merge with a failing test, a vulnerable dependency, a broken dbt model or a values
file that ArgoCD cannot render. The three repos need gates that run on every PR, cost
little, and need no cloud credentials.

## Decision

### o2c-app (`.github/workflows/pr.yml`, PRs to main)
A new push cancels the older run of the same PR. The workflow token can only read
(`contents: read`); only the `changes` job may also read the PR's file list.

| Job | What | Blocks |
|---|---|---|
| lint | `pre-commit run --all-files`: whitespace, YAML, gitleaks, ruff, ruff format, sqlfluff (eslint runs in ui) | style drift, committed secrets, invalid SQL/YAML |
| python | `uv sync --all-packages --all-groups`, `uv run pytest` (uv cache) | any failing unit test (recorded LLM responses only) |
| ui | `pnpm install --frozen-lockfile`, lint (oxlint), test (Vitest), build | broken UI, lockfile drift |
| security | see the table below | known vulnerabilities, insecure code, misconfigured images |
| images | every Dockerfile built with buildx (no push), then a Trivy image scan | an image that does not build; a CRITICAL vulnerability with a fix |
| data | Postgres service container; schemas from the services' own code; dbt build --empty; Alembic | dbt models that do not compile or run against the real schema; erp-mock migrations out of step with the models |
| eval | only when `prompts/**`, `services/extractor/**` or `libs/o2c-common/**` change and `OPENAI_API_KEY` is available | an extraction or injection-detector regression below the quick thresholds |

The images job builds every Dockerfile: the six Python services through
`docker/python-service.Dockerfile`, plus erp-mock, hello, ui, ml and mlflow.

The data job:
1. creates `raw`/`analytics` with `analytics_sink.db.setup` and the review tables with
   `agent.reviews.ReviewStore` (`analytics/ci_create_schemas.py`);
2. runs `dbt build --empty` without unit tests;
3. runs the dbt unit tests separately, because dbt 1.8's `--empty` also empties their
   fixtures;
4. runs `alembic upgrade head` and `alembic check` for erp-mock.

Security gates:

| Gate | Configuration | Fails on |
|---|---|---|
| Semgrep 1.182.1 (container) | `p/python`, `p/typescript`, `p/secrets` | any ERROR finding |
| pip-audit 2.10.1 | `uv export` of all packages and groups | any vulnerability not in `.pip-audit-ignore` |
| pnpm audit | lockfile | high and critical |
| Trivy 0.76.1 fs | lock files and secrets | CRITICAL not in `.trivyignore` |
| Checkov 3.3.30 | Dockerfiles, secrets in all files incl. `docker-compose.yml` | any failed check not skipped in `.checkov.yaml` |

- **Semgrep** found `text(f"CREATE SCHEMA ...")` in the analytics sink. Its names are
  constants, but it now uses SQLAlchemy's `CreateSchema`.
- **Checkov has no Docker Compose policies.** For `docker-compose.yml` it runs the
  secrets checks; the Kubernetes manifests that replace Compose in the cloud are checked
  in o2c-infra (Helm).

### Ignores and skips: one finding per line, each with a reason
- **`.checkov.yaml`:** only `CKV_DOCKER_2` (HEALTHCHECK). Kubernetes ignores it and uses
  the chart's probes.
  - The missing `USER` in the ui and mlflow images was fixed instead of skipped.
- **`.pip-audit-ignore`:** 35 vulnerability IDs, each with its package, version and
  reason. All trace to two pins:
  - kfp 2.13 (protobuf < 5), which holds dbt-core at 1.8.7 and with it dbt-common,
    deepdiff and sqlparse;
  - `transformers<4.47` for MLflow 2.17.2's transformers flavour.

  pip-audit has no expiry, so the file says "review by 2026-12-31". Any new
  vulnerability fails the PR.
- **`.trivyignore`:** the six CVEs of the MLflow 2.17.2 server, which are fixed only in
  MLflow 3.x, each with `exp:2026-12-31`, after which they fail again.
- **Fixed instead of ignored:**
  - certifi upgraded (critical);
  - pillow 13 and pypdf 6.21.2;
  - the ui base image moved to nginx-unprivileged 1.30;
  - the mlflow image rebuilt on python:3.12-slim (with `sqlalchemy<2.1`, which MLflow
    2.17.2 needs);
  - the uv image in the Dockerfiles pinned to 0.13.1 instead of `latest`.

### Pinning and updates
- **Pinning:** every action is pinned to a full commit SHA with its version in a comment
  (in all three repos; `pr-title.yml` was pinned too). Tools run at fixed versions
  (`uvx --from tool==x.y.z`), and container images by version and digest.
- **`.github/dependabot.yml`:** weekly updates with minor and patch grouped into one PR
  per ecosystem. It covers uv (root), npm (pnpm lockfile at the root, covering ui),
  docker (every Dockerfile directory), docker-compose and github-actions.
- **o2c-infra** has Dependabot for terraform and github-actions; **o2c-gitops** for
  github-actions.

### The eval job and what runs in Jenkins
- **The CI eval** (`eval_gate.py --ci`) runs the extraction on the first 10 golden POs of
  poc/data and the injection false-positive check, with the `poc_data_quick` and
  `injection` thresholds of `eval/thresholds.yaml`. It costs about one cent.
- **It runs against LiteLLM in a container.** That is the image pinned by digest, with
  the repo's model list (`ml/ci_litellm_config.py` drops only the database and the
  Langfuse/Prometheus callbacks).
- **It runs only when prompts, the extractor or the shared library change,** and only
  with the `OPENAI_API_KEY` secret. PRs from forks and repos without the secret skip it
  with a notice, so a missing key never blocks a PR.
- **The red team is not in CI:** it needs the agent's stack (ERP mock, contract index,
  MLflow).
- **Jenkins runs the full `make eval`** against a running environment: extraction on
  poc/data and the holdout, the hybrid classifier with MLflow, the judge, the red team
  and injection. It is a gate before a release and before a model migration (ADR-008),
  where cost (about $0.20) and run time (about 10 minutes) are acceptable. The same
  pipeline holds the Terraform plan and apply with credentials (E14).

### o2c-infra and o2c-gitops
- **o2c-infra** (`pr.yml`):
  - `terraform fmt -check` and `terraform validate` for every directory with `.tf` files
    (`init -backend=false`, no credentials);
  - `helm lint --strict` and `helm template` for every chart;
  - Checkov on Terraform and Helm.
- **Chart hardening** (chart 0.2.0), done instead of skipping the Kubernetes checks:
  - non-root user and seccomp `RuntimeDefault`;
  - no privilege escalation, a read-only root filesystem, all capabilities dropped;
  - no service-account token, and an emptyDir for `/tmp`.
- **Remaining Kubernetes skips** are annotations with reasons on the resources: the
  default namespace, image digests and pull policy. The NetworkPolicy check is skipped in
  `.checkov.yaml` until the cluster's namespaces exist.
- **Audit module:** each Terraform skip sits next to its resource with the reason (cost
  and destroyability, ADR-014). The archive bucket got a lifecycle rule instead of a
  skip.
- **o2c-gitops** (`pr.yml`): yamllint, then `helm template` of every `envs/*/*.yaml`
  with the chart from o2c-infra main. A broken values file fails the PR, not the ArgoCD
  sync.

## Consequences
- **Run time:** about 10–20 minutes per PR, with the image matrix and the full Python
  sync the longest. The uv, buildx (GHA) and pre-commit caches keep reruns short.
- **Every ignore expires or is reviewed by 2026-12-31.** The MLflow/kfp upgrade is the
  one change that removes most of them.
- **Branch protection on main** must require these checks; that is a repository setting
  and is not in Git.
- **Verified only on GitHub:** the workflows' behaviour on GitHub runners. That covers
  the service container, the GHA cache, the paths filter, secret availability for
  forks, and Dependabot accepting the configuration. Everything else was run locally
  first (see the PR).
