from decimal import Decimal
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    erp_base_url: str = "http://localhost:8100"
    kafka_bootstrap: str = "localhost:9092"
    litellm_base_url: str = "http://localhost:4000"
    litellm_master_key: str
    events_database_url: (
        str  # o2c database: processed events, checkpoints, contract vectors
    )
    s3_endpoint_url: str = "http://localhost:8333"
    s3_access_key: str
    s3_secret_key: str
    prompts_dir: Path = REPO_ROOT / "prompts"
    contracts_dir: Path = REPO_ROOT / "poc" / "data" / "contracts"
    auto_post_limit: Decimal = Decimal(10000)
    auth_jwt_secret: str
    auth_users_file: Path = REPO_ROOT / "local" / "users.json"
