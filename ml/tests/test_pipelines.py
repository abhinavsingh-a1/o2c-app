"""Kubeflow pipeline: compiled spec, local Docker path translation, KFP upload (no Docker,
no cluster)."""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("kfp")
import anomaly_pipeline
import run_kfp
import run_local
import yaml
from anomaly_pipeline import IMAGE, YAML_PATH, compile_pipeline

TASKS = ["load-history", "train", "evaluate", "register-and-promote"]


@pytest.fixture(scope="module")
def spec(tmp_path_factory):
    path = compile_pipeline(tmp_path_factory.mktemp("kfp") / "pipeline.yaml")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_pipeline_has_the_four_steps_in_order(spec):
    tasks = spec["root"]["dag"]["tasks"]
    assert sorted(tasks) == sorted(TASKS)
    assert tasks["train"]["dependentTasks"] == ["load-history"]
    assert sorted(tasks["evaluate"]["dependentTasks"]) == ["load-history", "train"]
    assert sorted(tasks["register-and-promote"]["dependentTasks"]) == [
        "evaluate",
        "load-history",
        "train",
    ]


def test_every_step_runs_in_the_ml_image_without_installing_kfp(spec):
    executors = spec["deploymentSpec"]["executors"]
    assert len(executors) == 4
    for executor in executors.values():
        container = executor["container"]
        assert container["image"] == IMAGE
        assert "pip install" not in " ".join(container["command"])


def test_caching_is_off_for_every_step(spec):
    for task in spec["root"]["dag"]["tasks"].values():
        assert not task.get("cachingOptions", {}).get("enableCache", False)


def test_database_url_is_a_required_parameter_without_default(spec):
    params = spec["root"]["inputDefinitions"]["parameters"]
    assert "defaultValue" not in params["erp_database_url"]
    assert (
        params["mlflow_tracking_uri"]["defaultValue"]
        == "http://host.docker.internal:5000"
    )
    assert (
        spec["root"]["outputDefinitions"]["parameters"]["Output"]["parameterType"]
        == "STRING"
    )


def test_committed_yaml_is_up_to_date(spec):
    committed = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    assert committed == spec, "run: make ml-pipeline-compile"


def test_steps_reuse_train_anomaly():
    for component in (
        anomaly_pipeline.load_history,
        anomaly_pipeline.train,
        anomaly_pipeline.evaluate,
        anomaly_pipeline.register_and_promote,
    ):
        source = component.python_func.__code__.co_names
        assert "train_anomaly" in source


# --- run_local: host.docker.internal and container paths --------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql+psycopg://o2c:secret@localhost:5432/erp",
            "postgresql+psycopg://o2c:secret@host.docker.internal:5432/erp",
        ),
        ("http://localhost:5000", "http://host.docker.internal:5000"),
        ("http://127.0.0.1:5000/", "http://host.docker.internal:5000/"),
        ("http://mlflow:5000", "http://mlflow:5000"),
        ("postgresql://u:localhost@db:5432/x", "postgresql://u:localhost@db:5432/x"),
    ],
)
def test_container_url(url, expected):
    assert run_local.container_url(url) == expected


def test_to_container_path_handles_windows_paths():
    root = r"G:\repo\ml\pipelines\local_outputs"
    assert (
        run_local.to_container_path(root + r"\run-1\train\model", root)
        == "/kfp-outputs/run-1/train/model"
    )
    assert (
        run_local.to_container_path("g:/REPO/ml/pipelines/local_outputs", root)
        == "/kfp-outputs"
    )
    assert run_local.to_container_path(r"G:\repo\ml\other", root) == r"G:\repo\ml\other"
    assert run_local.to_container_path("--executor_input", root) == "--executor_input"


def test_translate_command_rewrites_paths_inside_executor_input_json():
    root = r"G:\repo\out"
    executor_input = {
        "outputs": {
            "artifacts": {
                "model": {"artifacts": [{"uri": root + r"\run\train\model"}]}
            },
            "outputFile": root + r"\run\train\executor_output.json",
        },
        "inputs": {"parameterValues": {"erp_database_url": "postgresql://h/erp"}},
    }
    command = [
        "sh",
        "-c",
        "python3 -m kfp.dsl.executor_main",
        "--executor_input",
        json.dumps(executor_input),
    ]

    translated = run_local.translate_command(command, root)

    assert translated[:4] == command[:4]
    data = json.loads(translated[4])
    assert (
        data["outputs"]["artifacts"]["model"]["artifacts"][0]["uri"]
        == "/kfp-outputs/run/train/model"
    )
    assert (
        data["outputs"]["outputFile"] == "/kfp-outputs/run/train/executor_output.json"
    )
    assert data["inputs"] == executor_input["inputs"]


def test_handler_mounts_the_pipeline_root_at_the_container_root():
    root = r"G:\repo\out"
    handler = run_local.ContainerPathDockerTaskHandler(
        image=IMAGE,
        full_command=["--path", root + r"\x"],
        pipeline_root=root,
        runner=None,
    )
    assert handler.get_volumes_to_mount() == {
        root: {"bind": "/kfp-outputs", "mode": "rw"}
    }
    assert handler.full_command == ["--path", "/kfp-outputs/x"]


# --- run_kfp ------------------------------------------------------------------


def test_pipeline_arguments_point_containers_at_the_host():
    settings = SimpleNamespace(
        erp_database_url="postgresql+psycopg://o2c:pw@localhost:5432/erp",
        mlflow_tracking_uri="http://localhost:5000",
    )
    assert run_kfp.pipeline_arguments(settings) == {
        "erp_database_url": "postgresql+psycopg://o2c:pw@host.docker.internal:5432/erp",
        "mlflow_tracking_uri": "http://host.docker.internal:5000",
    }


class FakeClient:
    def __init__(self, existing_id=None):
        self.existing_id = existing_id
        self.uploaded, self.versions = [], []

    def get_pipeline_id(self, name):
        return self.existing_id

    def upload_pipeline(self, path, pipeline_name):
        self.uploaded.append(pipeline_name)
        return SimpleNamespace(pipeline_id="p-new")

    def upload_pipeline_version(self, path, pipeline_version_name, pipeline_id):
        self.versions.append((pipeline_id, pipeline_version_name))
        return SimpleNamespace(pipeline_version_id="v-1")


def test_upload_creates_the_pipeline_once_then_adds_versions(tmp_path):
    first = FakeClient()
    assert run_kfp.upload(first, tmp_path / "p.yaml", "v-a") == ("p-new", "v-1")
    assert first.uploaded == ["o2c-quantity-anomaly"]

    later = FakeClient(existing_id="p-old")
    assert run_kfp.upload(later, tmp_path / "p.yaml", "v-b") == ("p-old", "v-1")
    assert later.uploaded == [] and later.versions == [("p-old", "v-b")]


class FakeContainer:
    def __init__(self, code: int):
        self.code, self.removed = code, False

    def logs(self, stream):
        return iter([b"step ok \xf0\x9f\x8f\x83\n"])  # MLflow logs contain emoji

    def wait(self):
        return {"StatusCode": self.code}

    def remove(self):
        self.removed = True


@pytest.mark.parametrize(("code", "expected"), [(0, "SUCCESS"), (1, "FAILURE")])
def test_handler_run_removes_the_container_and_maps_the_exit_code(
    monkeypatch, code, expected
):
    container, calls = FakeContainer(code), []

    class FakeClient:
        containers = SimpleNamespace(
            run=lambda **kwargs: calls.append(kwargs) or container
        )

        def close(self):
            pass

    monkeypatch.setitem(
        __import__("sys").modules, "docker", SimpleNamespace(from_env=FakeClient)
    )
    handler = run_local.ContainerPathDockerTaskHandler(
        image=IMAGE, full_command=["x"], pipeline_root=r"G:\out", runner=None
    )

    assert handler.run().name == expected
    assert container.removed
    assert calls[0]["volumes"] == {r"G:\out": {"bind": "/kfp-outputs", "mode": "rw"}}
