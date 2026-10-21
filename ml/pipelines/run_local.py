"""Run the anomaly pipeline locally: kfp.local with the DockerRunner (image o2c-ml:dev).

Usage: uv run --group ml python ml/pipelines/run_local.py   (make ml-pipeline-local)

Needs the image (make ml-image) and the local stack (make up): Postgres with the ERP
history and MLflow. ERP_DATABASE_URL and MLFLOW_TRACKING_URI come from the environment or
.env; localhost is replaced by host.docker.internal, because the steps run in containers.
Task outputs are written to ml/pipelines/local_outputs/.

kfp.local mounts the pipeline root into each container at the same path and passes host
paths in the task command. A Windows path such as G:\\...\\local_outputs is no path inside
a Linux container, so this script mounts the folder at /kfp-outputs and translates every
host path in the command (also inside the executor-input JSON) to that container path.
"""

import json
import re
import sys
from pathlib import Path

from kfp import local
from kfp.local import docker_task_handler, status

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]  # anomaly_pipeline, train_anomaly

from anomaly_pipeline import anomaly_pipeline
from train_anomaly import Settings

OUTPUTS = HERE / "local_outputs"
CONTAINER_ROOT = "/kfp-outputs"
LOCAL_HOST = re.compile(r"(?<=[@/])(localhost|127\.0\.0\.1)(?=[:/]|$)")


def container_url(url: str) -> str:
    """postgresql+psycopg://u:p@localhost:5432/erp -> ...@host.docker.internal:5432/erp"""
    return LOCAL_HOST.sub("host.docker.internal", url)


def _normalise(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").lower()


def to_container_path(
    value: str, host_root: str, container_root: str = CONTAINER_ROOT
) -> str:
    """A host path under host_root as the same path under container_root; else unchanged."""
    root = _normalise(host_root)
    candidate = value.replace("\\", "/")
    if candidate.lower() == root or candidate.lower().startswith(root + "/"):
        return container_root + candidate[len(root) :]
    return value


def translate_command(
    command: list[str], host_root: str, container_root: str = CONTAINER_ROOT
) -> list[str]:
    """Translate host paths in a task command, including JSON arguments (executor input)."""

    def walk(value):
        if isinstance(value, str):
            return to_container_path(value, host_root, container_root)
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        return value

    translated = []
    for arg in command:
        try:
            parsed = json.loads(arg)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict | list):
            translated.append(json.dumps(walk(parsed)))
        else:
            translated.append(walk(arg))
    return translated


class ContainerPathDockerTaskHandler(docker_task_handler.DockerTaskHandler):
    """DockerTaskHandler that mounts the pipeline root at CONTAINER_ROOT."""

    def __init__(self, image, full_command, pipeline_root, runner):
        super().__init__(
            image, translate_command(full_command, pipeline_root), pipeline_root, runner
        )

    def get_volumes_to_mount(self) -> dict:
        return {self.pipeline_root: {"bind": CONTAINER_ROOT, "mode": "rw"}}

    def run(self):
        """As DockerTaskHandler.run, but the finished container is removed."""
        import docker

        client = docker.from_env()
        try:
            container = client.containers.run(
                image=self.image,
                command=self.full_command,
                detach=True,
                volumes=self.get_volumes_to_mount(),
            )
            for line in container.logs(stream=True):
                print(line.decode(errors="replace"), end="")
            code = container.wait()["StatusCode"]
            container.remove()
        finally:
            client.close()
        return status.Status.SUCCESS if code == 0 else status.Status.FAILURE


def main() -> dict:
    # Container logs contain non-ASCII (e.g. MLflow's emoji); a Windows console is cp1252.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = Settings()
    arguments = {
        "erp_database_url": container_url(settings.erp_database_url),
        "mlflow_tracking_uri": container_url(settings.mlflow_tracking_uri),
    }
    print(f"MLflow: {arguments['mlflow_tracking_uri']}; outputs: {OUTPUTS}")

    OUTPUTS.mkdir(parents=True, exist_ok=True)
    docker_task_handler.DockerTaskHandler = ContainerPathDockerTaskHandler
    local.init(runner=local.DockerRunner(), pipeline_root=str(OUTPUTS))
    run = anomaly_pipeline(**arguments)
    result = json.loads(run.output)
    print(
        f"\nv{result['version']}: {'promoted to @champion' if result['promoted'] else 'not promoted'}"
        f" ({result['reason']}); @champion is v{result['champion_version']}"
    )
    return result


if __name__ == "__main__":
    main()
