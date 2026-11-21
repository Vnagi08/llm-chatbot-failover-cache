"""Application configuration, loaded exclusively from environment variables."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM -------------------------------------------------------------
    # Comma-separated list of provider API keys. Order defines key_1, key_2, ...
    llm_api_keys: str = Field(default="", repr=False)
    # LiteLLM model name used for every key.
    llm_model: str = "gemini/gemini-3.5-flash-lite"
    # Optional comma-separated per-key model override (aligned with LLM_API_KEYS).
    # Lets key_2 point at a different provider, e.g. "gemini/gemini-3.5-flash-lite,groq/llama-3.1-8b-instant".
    llm_models: str = ""
    # Optional custom endpoint (OpenAI-compatible gateways, self-hosted models).
    llm_api_base: str = ""
    llm_timeout_seconds: float = 30.0
    llm_max_output_tokens: int | None = None
    default_system_prompt: str = "You are a helpful assistant."
    # Number of previous messages (user + assistant) sent as context.
    max_history_messages: int = 50

    # --- Cache -----------------------------------------------------------
    cache_enabled: bool = True
    cache_ttl_seconds: int = 3600
    cache_max_items: int = 1000

    # --- Storage ---------------------------------------------------------
    database_url: str = "sqlite:////data/app.db"

    # --- HTTP ------------------------------------------------------------
    allowed_origins: str = "http://localhost:3000"
    enable_failure_simulation: bool = False

    log_level: str = "INFO"

    @field_validator("llm_max_output_tokens", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if value in ("", None) else value

    @cached_property
    def api_keys(self) -> list[str]:
        return _split_csv(self.llm_api_keys)

    @cached_property
    def key_models(self) -> list[str]:
        """Model name for each configured key (falls back to LLM_MODEL)."""
        overrides = _split_csv(self.llm_models)
        return [overrides[i] if i < len(overrides) else self.llm_model for i in range(len(self.api_keys))]

    @cached_property
    def origins(self) -> list[str]:
        return _split_csv(self.allowed_origins)

    @cached_property
    def database_path(self) -> Path:
        """Translate an SQLAlchemy-style sqlite URL into a filesystem path.

        sqlite:////data/app.db  -> /data/app.db   (absolute)
        sqlite:///./data/app.db -> ./data/app.db  (relative)
        """
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError("DATABASE_URL must be an sqlite:/// URL")
        raw = self.database_url[len(prefix):]
        if raw in ("", ":memory:"):
            raise ValueError("DATABASE_URL must point to a file")
        return Path(raw)
