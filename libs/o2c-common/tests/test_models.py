from pathlib import Path

from o2c_common import models


def test_models_dir_defaults_to_app_models(monkeypatch):
    monkeypatch.delenv(models.MODELS_DIR_ENV, raising=False)
    assert models.models_dir() == Path("/app/models")


def test_models_dir_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(models.MODELS_DIR_ENV, str(tmp_path))
    assert models.models_dir() == tmp_path


def test_baked_model_is_a_directory_with_an_mlmodel_file(monkeypatch, tmp_path):
    monkeypatch.setenv(models.MODELS_DIR_ENV, str(tmp_path))
    (tmp_path / "o2c-doc-classifier").mkdir()
    (tmp_path / "o2c-doc-classifier" / "MLmodel").write_text("flavors: {}\n")

    assert models.baked_model("o2c-doc-classifier") == tmp_path / "o2c-doc-classifier"


def test_no_baked_model_without_mlmodel(monkeypatch, tmp_path):
    monkeypatch.setenv(models.MODELS_DIR_ENV, str(tmp_path))
    (tmp_path / "empty").mkdir()

    assert models.baked_model("empty") is None
    assert models.baked_model("missing") is None
