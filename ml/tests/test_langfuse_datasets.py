import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import langfuse_common
import pytest
from langfuse_common import git_short_sha, setting, trace_id
from langfuse_datasets import DATASETS, dataset_items, item_id, upload
from sync_prompts import LOCAL, prompt_files, sync

POC_DATA = Path(__file__).resolve().parents[2] / "poc" / "data"


@pytest.fixture
def dataset(tmp_path) -> Path:
    root = tmp_path / "data"
    for folder, suffix in [("pos", "pdf"), ("ground_truth", "json")]:
        (root / folder).mkdir(parents=True)
        for po_id in ["PO-0001", "PO-0002"]:
            shutil.copy(POC_DATA / folder / f"{po_id}.{suffix}", root / folder)
    return root


class FakeLangfuse:
    """Langfuse client with an in-memory dataset store (upsert by item id)."""

    def __init__(self):
        self.datasets: dict[str, dict[str, SimpleNamespace]] = {}
        self.item_writes = 0

    def get_dataset(self, name):
        if name not in self.datasets:
            raise RuntimeError("not found")
        return SimpleNamespace(items=list(self.datasets[name].values()))

    def create_dataset(self, name, description=None):
        self.datasets.setdefault(name, {})

    def create_dataset_item(self, dataset_name, id, input, expected_output, metadata):
        self.item_writes += 1
        self.datasets[dataset_name][id] = SimpleNamespace(
            id=id, input=input, expected_output=expected_output, metadata=metadata
        )


def test_dataset_items(dataset):
    items = dataset_items("golden", dataset)

    assert [item["id"] for item in items] == ["golden-PO-0001", "golden-PO-0002"]
    first = items[0]
    assert first["input"]["filename"] == "PO-0001.pdf"
    assert "Salzach Kunststofftechnik GmbH" in first["input"]["text"]
    truth = json.loads((dataset / "ground_truth" / "PO-0001.json").read_text("utf-8"))
    assert first["expected_output"] == truth
    assert first["metadata"] == {"po_id": "PO-0001", "source": "data"}


def test_source_is_repository_relative():
    assert dataset_items("holdout", DATASETS["holdout"])[0]["metadata"]["source"] == (
        "ml/data/holdout"
    )


def test_upload_is_idempotent(dataset):
    client = FakeLangfuse()
    items = dataset_items("golden", dataset)

    assert upload(client, "golden", items) == {
        "created": 2,
        "updated": 0,
        "unchanged": 0,
    }
    assert upload(client, "golden", items) == {
        "created": 0,
        "updated": 0,
        "unchanged": 2,
    }
    assert len(client.datasets["golden"]) == 2
    assert client.item_writes == 2  # the second run sent nothing


def test_upload_updates_a_changed_item_in_place(dataset):
    client = FakeLangfuse()
    items = dataset_items("golden", dataset)
    upload(client, "golden", items)

    items[1]["expected_output"] = {**items[1]["expected_output"], "po_number": "X"}
    counts = upload(client, "golden", items)

    assert counts == {"created": 0, "updated": 1, "unchanged": 1}
    assert len(client.datasets["golden"]) == 2
    assert (
        client.datasets["golden"]["golden-PO-0002"].expected_output["po_number"] == "X"
    )


def test_datasets_and_item_ids():
    assert set(DATASETS) == {"golden", "holdout"}
    assert all((path / "pos").is_dir() for path in DATASETS.values())
    assert item_id("holdout", "PO-0007") == "holdout-PO-0007"


# --- langfuse_common ---


def test_trace_id_is_deterministic_32_hex():
    first = trace_id("run", "nonce", "PO-0001")

    assert first == trace_id("run", "nonce", "PO-0001")
    assert first != trace_id("run", "nonce", "PO-0002")
    assert len(first) == 32 and int(first, 16) >= 0


def test_git_short_sha(tmp_path):
    assert git_short_sha() not in ("", "nogit")
    assert git_short_sha(tmp_path) == "nogit"


def test_setting_prefers_the_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(langfuse_common, "REPO_ROOT", tmp_path)
    (tmp_path / ".env").write_text("LANGFUSE_HOST=http://from-env-file\n")

    assert setting("LANGFUSE_HOST") == "http://from-env-file"
    monkeypatch.setenv("LANGFUSE_HOST", "http://from-environment")
    assert setting("LANGFUSE_HOST") == "http://from-environment"
    assert setting("MISSING_SETTING", "default") == "default"


# --- sync_prompts ---


class FakePrompts:
    """Langfuse prompt store: versions with labels; "local" moves to the newest."""

    def __init__(self):
        self.versions: dict[str, list[SimpleNamespace]] = {}
        self.client = SimpleNamespace(prompts=SimpleNamespace(get=self._get))

    def _get(self, name, label):
        for version in reversed(self.versions.get(name, [])):
            if label in version.labels:
                return version
        raise RuntimeError("not found")

    def create_prompt(self, name, prompt, labels, type, commit_message):
        for version in self.versions.get(name, []):
            version.labels = [x for x in version.labels if x not in labels]
        versions = self.versions.setdefault(name, [])
        versions.append(
            SimpleNamespace(
                prompt=prompt, labels=list(labels), version=len(versions) + 1
            )
        )

    def update_prompt(self, name, version, new_labels):
        self.versions[name][version - 1].labels = list(new_labels)


def test_prompt_files(tmp_path):
    (tmp_path / "a.md").write_text("A", encoding="utf-8")
    (tmp_path / "b.md").write_text("B", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")

    assert prompt_files(tmp_path) == {"a": "A", "b": "B"}


def test_real_prompts_are_found():
    assert {"extract", "classify", "explain_exception", "judge_explanation"} <= set(
        prompt_files()
    )


def test_sync_creates_then_skips_then_labels():
    client = FakePrompts()

    assert sync(client, "extract", "v1", "abc1234") == "created"
    assert sync(client, "extract", "v1", "abc1234") == "unchanged"
    assert sync(client, "extract", "v1", "def5678") == "labelled"
    assert len(client.versions["extract"]) == 1
    assert client.versions["extract"][0].labels == [LOCAL, "abc1234", "def5678"]


def test_sync_creates_a_new_version_when_the_text_changes():
    client = FakePrompts()
    sync(client, "extract", "v1", "abc1234")

    assert sync(client, "extract", "v2", "def5678") == "created"
    first, second = client.versions["extract"]
    assert second.prompt == "v2" and second.labels == [LOCAL, "def5678"]
    assert LOCAL not in first.labels
