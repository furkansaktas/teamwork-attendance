from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./attendance.db"
    jwt_secret: str = "development-jwt-secret-change-me"
    code_secret: str = "development-code-secret-change-me"
    access_token_minutes: int = 30
    qr_ttl_seconds: int = 30
    attempt_window_minutes: int = 10
    attempt_limit_per_student: int = 5
    attempt_limit_per_ip: int = 30
    attempt_limit_per_device: int = 30

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
