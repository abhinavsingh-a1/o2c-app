"""Upload the compiled anomaly pipeline to Kubeflow Pipelines and start a run.

Usage: uv run --group ml python ml/pipelines/run_kfp.py [--no-wait]   (make kfp-run)

Needs KFP on the kind cluster (make kfp-install), the image in kind (make ml-image, then
kind load docker-image o2c-ml:dev --name o2c) and the UI port-forward (make kfp-ui):
KFP_HOST defaults to http://localhost:18088. ERP_DATABASE_URL and MLFLOW_TRACKING_URI
come from the environment or .env; localhost is replaced by host.docker.internal, which
pods in kind can reach. The pipeline YAML is compiled fresh and uploaded as a new version
of the pipeline "o2c-quantity-anomaly"; the run goes to the experiment "o2c-ml".
"""

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import kfp

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

from anomaly_pipeline import PIPELINE_NAME, compile_pipeline
from run_local import container_url
from train_anomaly import Settings

EXPERIMENT = "o2c-ml"
DEFAULT_HOST = (
    "http://localhost:18088"  # kubectl port-forward svc/ml-pipeline-ui 18088:80
)


def pipeline_arguments(settings: Settings) -> dict[str, str]:
    return {
        "erp_database_url": container_url(settings.erp_database_url),
        "mlflow_tracking_uri": container_url(settings.mlflow_tracking_uri),
    }


def upload(client: kfp.Client, yaml_path: Path, version_name: str) -> tuple[str, str]:
    """(pipeline id, version id): a new version of the pipeline, created on first use."""
    pipeline_id = client.get_pipeline_id(PIPELINE_NAME)
    if pipeline_id is None:
        pipeline = client.upload_pipeline(str(yaml_path), pipeline_name=PIPELINE_NAME)
        pipeline_id = pipeline.pipeline_id
    version = client.upload_pipeline_version(
        str(yaml_path), pipeline_version_name=version_name, pipeline_id=pipeline_id
    )
    return pipeline_id, version.pipeline_version_id


def main(argv: list[str] | None = None) -> str:
    parser = argparse.ArgumentParser(
        description="Run the anomaly pipeline on Kubeflow."
    )
    parser.add_argument("--no-wait", action="store_true", help="start the run and exit")
    args = parser.parse_args(argv)

    host = os.environ.get("KFP_HOST", DEFAULT_HOST)
    yaml_path = compile_pipeline()
    client = kfp.Client(host=host)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    pipeline_id, version_id = upload(client, yaml_path, f"{PIPELINE_NAME}-{stamp}")
    experiment = client.create_experiment(EXPERIMENT)  # returns the existing one
    run = client.run_pipeline(
        experiment_id=experiment.experiment_id,
        job_name=f"{PIPELINE_NAME}-{stamp}",
        params=pipeline_arguments(Settings()),
        pipeline_id=pipeline_id,
        version_id=version_id,
        enable_caching=False,
    )
    print(f"run {run.run_id}: {host}/#/runs/details/{run.run_id}")
    if args.no_wait:
        return run.run_id
    detail = client.wait_for_run_completion(run.run_id, timeout=1800)
    print(f"run {run.run_id} finished: {detail.state}")
    if detail.state != "SUCCEEDED":
        sys.exit(1)
    return run.run_id


if __name__ == "__main__":
    main()
