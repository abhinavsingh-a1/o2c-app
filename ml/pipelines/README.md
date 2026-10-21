# Kubeflow pipeline: quantity anomaly model

`anomaly_pipeline.py` trains, evaluates and (maybe) promotes the quantity anomaly model
(ADR-006, ADR-007):

```
load_history -> train -> evaluate -> register_and_promote
```

| Step | Does | Output |
|---|---|---|
| load_history | completed ERP orders + labels from `poc/data/history_outliers.json` | history CSV |
| train | IsolationForest, per customer-material statistics, 0.8 calibration | model (pickle) |
| evaluate | precision / recall / F1 on the history outliers; false-alarm rate on `poc/data` and `ml/data/holdout` | evaluation JSON, KFP metrics |
| register_and_promote | new MLflow version of `o2c-quantity-anomaly`; `@champion` moves only if recall >= 80% **and** the PO false-alarm rate is lower than the champion's | promotion result (JSON) |

All steps run in the image `o2c-ml:dev` and call the functions in `ml/train_anomaly.py`,
so `uv run --group ml python ml/train_anomaly.py` and the pipeline do exactly the same.
Caching is off: every run reads the current ERP data and registry.

Configuration: the pipeline parameters `erp_database_url` and `mlflow_tracking_uri`.
`run_local.py` and `run_kfp.py` take them from `ERP_DATABASE_URL` and `MLFLOW_TRACKING_URI`
(environment or `.env`) and replace `localhost` with `host.docker.internal`, because the
steps run in containers (Docker Desktop and kind both resolve that name to the host).

## Prerequisites
- `make up` (Postgres with the ERP history, MLflow), history seeded
  (`uv run python -m erp_mock.seed_history`).
- `make ml-image` builds `o2c-ml:dev` (docker/ml.Dockerfile). Rebuild after changing
  `ml/`, `libs/`, `services/agent/` or `poc/data/history_outliers.json` (the labels are in
  the image, so they must match the history in the database).

## Run locally (no Kubernetes)
```
make ml-pipeline-local     # kfp.local + DockerRunner, outputs in ml/pipelines/local_outputs/
make ml-pipeline-compile   # writes ml/pipelines/anomaly_pipeline.yaml (commit it)
```
`run_local.py` mounts the outputs folder at `/kfp-outputs` in each container and translates
the Windows host paths in the task commands, because kfp's DockerRunner would otherwise
mount `G:\...` into a Linux container. Finished containers are removed.

## Run on Kubeflow Pipelines (kind cluster "o2c")
KFP standalone, pinned to `KFP_VERSION` (2.17.2) in the Makefile. The SDK is kfp 2.13.0: the
newest that fits next to `mlflow-skinny==2.17.2` (kfp >= 2.17 needs protobuf 6, MLflow 2.17
needs protobuf < 6). It compiles the v2 pipeline spec that the 2.17 backend runs.

```
make kfp-install   # cluster-scoped resources, then env/platform-agnostic; waits until all
                   # pods in namespace kubeflow are Ready; loads o2c-ml:dev into kind
make kfp-ui        # kubectl port-forward -n kubeflow svc/ml-pipeline-ui 18088:80
                   # (keep it running; UI at http://localhost:18088 - 8080 is blocked here)
make kfp-run       # compiles, uploads a new pipeline version, starts a run, waits
```
- After rebuilding the image: `make ml-image kfp-load-image`.
- `kfp-install` deletes the NetworkPolicy `seaweedfs` in namespace `kubeflow`. kind's CNI
  (kindnet) enforces NetworkPolicies, and with this policy it dropped traffic that the policy
  allows: pipeline steps timed out reading and writing artifacts (`dial tcp ...:9000:
  i/o timeout`), while the store answered inside its own pod. Without the policy, any pod
  in the cluster can reach KFP's internal artifact store - acceptable on a local
  single-node cluster, not on a shared one.
- Runs go to the experiment `o2c-ml`; each upload is a new version of the pipeline
  `o2c-quantity-anomaly`.
- The database URL, including its password, is a run parameter and is visible in the KFP UI.
  That is acceptable for the local kind cluster; a shared cluster should take it from a
  Kubernetes secret (kfp-kubernetes `use_secret_as_env`) instead.

### Remove KFP again (frees several GB of memory)
```
make kfp-uninstall
# = kubectl delete -k "github.com/kubeflow/pipelines/manifests/kustomize/env/platform-agnostic?ref=2.17.2"
#   kubectl delete -k "github.com/kubeflow/pipelines/manifests/kustomize/cluster-scoped-resources?ref=2.17.2"
```
The MLflow registry is not affected: models and versions live in MLflow, not in KFP.

## After `@champion` moves
The agent loads `models:/o2c-quantity-anomaly@champion` once and keeps it in memory. To
pick up a new champion, restart it:
```
docker compose restart agent
# on Kubernetes later: kubectl rollout restart deployment/agent
```
Until the restart the agent keeps scoring with the previous champion. If MLflow is
unreachable at startup, it scores 0.0 and retries the load at most every 5 minutes.
