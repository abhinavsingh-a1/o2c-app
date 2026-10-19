from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    erp_database_url: str


settings = Settings()
engine = create_engine(settings.erp_database_url)
SessionLocal = sessionmaker(bind=engine)
