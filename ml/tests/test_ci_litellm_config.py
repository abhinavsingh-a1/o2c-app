import ci_litellm_config
import yaml
from ci_litellm_config import CONFIG, ci_config, main


def real_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def test_ci_config_drops_database_and_callbacks_but_keeps_models():
    original = real_config()

    config = ci_config(original)

    assert config["model_list"] == original["model_list"]
    assert config["router_settings"] == original["router_settings"]
    assert "database_url" not in config["general_settings"]
    assert config["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"
    assert config["litellm_settings"] == {"drop_params": True}
    assert "database_url" in original["general_settings"]  # input not changed


def test_main_writes_the_ci_config(tmp_path):
    out = tmp_path / "litellm-ci.yaml"

    assert main([str(out)]) == 0

    written = yaml.safe_load(out.read_text(encoding="utf-8"))
    aliases = {m["model_name"] for m in written["model_list"]}
    assert {"o2c-extract", "o2c-extract-previous", "o2c-embed"} <= aliases
    assert ci_config(real_config()) == written
    assert ci_litellm_config.DROPPED_SETTINGS
