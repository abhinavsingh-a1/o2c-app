# ADR-007: Kubeflow pipeline for model training

Date: 2026-10-21
Status: Accepted

## Context
The quantity anomaly model (ADR-006) was trained by running `ml/train_anomaly.py` by
hand. The result depended on who ran it, with which code and data, and whether they
remembered to evaluate before moving `@champion`. A model that decides which orders go
to human review needs training that is repeatable and auditable, and a promotion rule
that is enforced, not remembered.

## Decision
- **A Kubeflow pipeline (KFP SDK v2)**, `ml/pipelines/anomaly_pipeline.py`:
  `load_history -> train -> evaluate -> register_and_promote`. Every step is a component
  with typed inputs and outputs (history CSV, model, evaluation JSON, KFP metrics), so each
  run records what went in, what came out, and why the model was or was not promoted.
- **One code path.** The components call the functions in `ml/train_anomaly.py`; the
  script and the pipeline cannot drift apart. All steps run in one image built from the
  repo (`docker/ml.Dockerfile`, `o2c-ml:dev`): Python 3.12, uv, the workspace with the
  "ml" group, torch from the CPU-only index. Caching is off, so every run reads the current
  ERP data and registry.
- **Promotion rule, enforced in code** (`train_anomaly.should_promote`, unit-tested):
  every run registers a new MLflow version of `o2c-quantity-anomaly`; `@champion` moves
  only if recall on the labelled history outliers is at least 80% **and** the PO-level
  false-alarm rate on `poc/data` + `ml/data/holdout` is lower than the current champion's.
  A tie keeps the champion. The decision and its reason are MLflow tags
  (`promoted_to_champion`, `promotion_reason`).
- **Two runners, one pipeline:**
  - Local: `kfp.local` with the DockerRunner (`make ml-pipeline-local`). No Kubernetes
    needed; the default for development. On Windows, `run_local.py` mounts the outputs at
    `/kfp-outputs` and translates host paths, which kfp's DockerRunner does not do.
  - Cluster: KFP standalone 2.17.2 on the local kind cluster (`make kfp-install`,
    `kfp-run`), for the UI, run history and scheduling. It can be removed again
    (`make kfp-uninstall`) to free memory.
  Both read the database and MLflow on the host via `host.docker.internal`.
- **Heavy training stays local.** The pipeline does not move training to the cloud:
  training and KFP run on the developer machine / kind, in line with the AWS cost limits
  in ADR-003 of o2c-infra. The same compiled pipeline can run on a cloud KFP later if
  that decision changes.
- **The agent picks up a new champion on restart** (`docker compose restart agent`, later
  `kubectl rollout restart`); it loads the model once (ADR-006).

## Consequences
- Training is one command, and every promotion is traceable to a pipeline run, an MLflow
  run and a recorded reason.
- The compiled `ml/pipelines/anomaly_pipeline.yaml` is committed; a test fails when it is
  out of date with the code (`make ml-pipeline-compile`).
- The ML image is large (~2.5 GB: the whole workspace plus torch for the classifier) and
  must be rebuilt, and loaded into kind, after code or label changes.
- KFP SDK 2.13.0 is the newest that fits next to `mlflow-skinny==2.17.2` (kfp >= 2.17 needs
  protobuf 6, MLflow 2.17 needs protobuf < 6). Upgrading MLflow (server and client) lifts
  this limit.
- The database URL is a pipeline parameter and visible in the KFP UI; acceptable on the
  local cluster, a shared cluster should use a Kubernetes secret.
- KFP needs several GB of memory on the kind node; it is optional and removable.
