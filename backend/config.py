from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = Field(..., description="Anthropic API key")
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: str = "./learnskills.db"
    skills_dir: str = "./skills"
    log_level: str = "INFO"

    # Model + agent knobs (centralized so the agent code stays focused on the loop)
    model: str = "claude-opus-4-7"
    max_tokens: int = 32000
    effort: str = "high"
    max_iterations: int = 10

    @property
    def skills_path(self) -> Path:
        return Path(self.skills_dir).resolve()

    @property
    def db_path_resolved(self) -> Path:
        return Path(self.db_path).resolve()


settings = Settings()
