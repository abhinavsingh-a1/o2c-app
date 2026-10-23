import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from publish_models import (
    ARTIFACT_TYPE,
    MODELS,
    SOURCE,
    Oras,
    champion_version,
    download_version,
    main,
    publish,
    repository,
    write_lock,
)


class FakeRun:
    """subprocess.run for oras: tags in `existing` resolve, push returns a digest."""

    def __init__(self, existing: dict[str, str] | None = None, push_fails=False):
        self.existing = existing or {}
        self.push_fails = push_fails
        self.calls = []

    def __call__(self, cmd, input, capture_output, text, cwd):
        self.calls.append(SimpleNamespace(cmd=cmd, input=input, cwd=cwd))
        verb, ref = cmd[1], cmd[2]
        if verb == "resolve":
            digest = self.existing.get(ref)
            return subprocess.CompletedProcess(
                cmd, 0 if digest else 1, digest or "", ""
            )
        if self.push_fails:
            return subprocess.CompletedProcess(cmd, 1, "", "denied")
        out = json.dumps({"reference": ref, "digest": "sha256:new"})
        return subprocess.CompletedProcess(cmd, 0, out, "")


class Client:
    def __init__(self, versions: dict[str, int]):
        self.versions = versions

    def get_model_version_by_alias(self, name, alias):
        assert alias == "champion"
        return SimpleNamespace(version=self.versions[name])


def fake_download(name, version, workdir):
    path = workdir / name
    path.mkdir()
    (path / "MLmodel").write_text("flavors: {}\n")
    return path


def test_repository_drops_the_o2c_prefix():
    assert (
        repository("ghcr.io/abhinavsingh-a1", "o2c-doc-classifier")
        == "ghcr.io/abhinavsingh-a1/o2c-model-doc-classifier"
    )


def test_champion_version_is_a_string():
    assert champion_version(Client({"m": 7}), "m") == "7"


def test_publish_pushes_a_new_version_with_annotations():
    run = FakeRun()
    entry, pushed = publish(
        "o2c-doc-classifier", "3", "reg/o", Oras(run=run), download=fake_download
    )

    assert pushed
    assert entry == {
        "name": "o2c-doc-classifier",
        "version": "3",
        "repository": "reg/o/o2c-model-doc-classifier",
        "tag": "v3",
        "digest": "sha256:new",
    }
    push = run.calls[1]
    assert push.cmd[:4] == [
        "oras",
        "push",
        "reg/o/o2c-model-doc-classifier:v3",
        "o2c-doc-classifier/",
    ]
    assert ["--artifact-type", ARTIFACT_TYPE] == push.cmd[4:6]
    assert f"org.opencontainers.image.source={SOURCE}" in push.cmd
    assert "org.opencontainers.image.version=3" in push.cmd


def test_publish_keeps_an_existing_version_without_downloading():
    run = FakeRun({"reg/o/o2c-model-quantity-anomaly:v5": "sha256:old"})

    def no_download(*args):
        raise AssertionError("an existing version is not downloaded again")

    entry, pushed = publish(
        "o2c-quantity-anomaly", "5", "reg/o", Oras(run=run), download=no_download
    )

    assert not pushed
    assert entry["digest"] == "sha256:old"
    assert [c.cmd[1] for c in run.calls] == ["resolve"]


def test_a_failed_push_raises():
    with pytest.raises(RuntimeError, match="denied"):
        publish(
            "o2c-doc-classifier",
            "3",
            "reg/o",
            Oras(run=FakeRun(push_fails=True)),
            download=fake_download,
        )


def test_the_token_goes_to_stdin_never_to_the_command_line():
    run = FakeRun()
    Oras(username="owner", password="secret-token", run=run).resolve("reg/o/m:v1")

    call = run.calls[0]
    assert "secret-token" not in call.cmd
    assert call.input == "secret-token"
    assert call.cmd[-3:] == ["--username", "owner", "--password-stdin"]


def test_plain_http_uses_no_login():
    run = FakeRun()
    Oras(plain_http=True, run=run).resolve("localhost:5001/m:v1")

    assert run.calls[0].cmd[-1] == "--plain-http"
    assert "--password-stdin" not in run.calls[0].cmd


def test_download_version_pins_the_version_not_the_alias(monkeypatch, tmp_path):
    import mlflow

    def download_artifacts(artifact_uri, dst_path):
        assert artifact_uri == "models:/o2c-doc-classifier/3"
        path = Path(dst_path)
        path.mkdir(parents=True)
        (path / "MLmodel").write_text("flavors: {}\n")
        return str(path)

    monkeypatch.setattr(mlflow.artifacts, "download_artifacts", download_artifacts)

    path = download_version("o2c-doc-classifier", "3", tmp_path)

    assert path == tmp_path / "o2c-doc-classifier"
    assert (path / "MLmodel").is_file()


def test_write_lock_is_sorted_and_stable(tmp_path):
    lock = tmp_path / "models.lock.json"
    write_lock(lock, [{"name": "b", "version": "1"}, {"name": "a", "version": "2"}])

    assert json.loads(lock.read_text()) == {
        "models": [{"name": "a", "version": "2"}, {"name": "b", "version": "1"}]
    }
    assert lock.read_text().endswith("\n")


def test_main_publishes_both_champions_and_writes_the_lock(tmp_path, capsys):
    existing = {
        f"{repository('reg/o', name)}:v{version}": f"sha256:{name}"
        for name, version in [("o2c-doc-classifier", 4), ("o2c-quantity-anomaly", 2)]
    }
    lock = tmp_path / "models.lock.json"

    code = main(
        ["--registry", "reg/o", "--lock", str(lock)],
        client=Client({"o2c-doc-classifier": 4, "o2c-quantity-anomaly": 2}),
        oras=Oras(run=FakeRun(existing)),
    )

    assert code == 0
    models = json.loads(lock.read_text())["models"]
    assert [m["name"] for m in models] == sorted(MODELS)
    assert models[0]["digest"] == "sha256:o2c-doc-classifier"
    assert "already published" in capsys.readouterr().out
