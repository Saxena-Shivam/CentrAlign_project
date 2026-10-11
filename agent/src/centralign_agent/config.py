from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


def _load_project_env() -> None:
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value.")


class Settings(BaseModel):
    app_host: str = Field(default_factory=lambda: os.getenv("APP_HOST", "127.0.0.1"))
    app_port: int = Field(default_factory=lambda: int(os.getenv("APP_PORT", "3000")))
    app_base_url: str = Field(default_factory=lambda: os.getenv("APP_BASE_URL", "http://127.0.0.1:3000"))
    demo_username: str = Field(default_factory=lambda: os.getenv("DEMO_USERNAME", "demo"))
    demo_password: str = Field(default_factory=lambda: os.getenv("DEMO_PASSWORD", "demo"))
    llm_provider: str = Field(
        default_factory=lambda: (os.getenv("LLM_PROVIDER") or os.getenv("CENTRALIGN_MODEL", "auto")).strip().lower()
    )
    llm_base_url: str | None = Field(default_factory=lambda: os.getenv("LLM_BASE_URL") or None)
    llm_api_key: str | None = Field(
        default_factory=lambda: os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or None
    )
    llm_model: str = Field(
        default_factory=lambda: os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    )
    inbox_dir: Path = Field(default_factory=lambda: Path(os.getenv("INBOX_DIR", "./inbox")).resolve())
    agent_max_steps: int = Field(default_factory=lambda: int(os.getenv("AGENT_MAX_STEPS", "16")))
    auto_approve: bool = Field(default_factory=lambda: _env_bool("AUTO_APPROVE"))
    headless: bool = Field(default_factory=lambda: _env_bool("HEADLESS", True))
    browser_action_delay_ms: int = Field(default_factory=lambda: int(os.getenv("BROWSER_ACTION_DELAY_MS", "1200")))
    chaos_mode: str | None = Field(default_factory=lambda: os.getenv("CHAOS_MODE"))
    eval_db_path: Path = Field(default_factory=lambda: Path(os.getenv("EVAL_DB_PATH", "./company_app/data/eval_demo.db")).resolve())

    @field_validator("app_port")
    @classmethod
    def validate_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        return value

    @field_validator("browser_action_delay_ms")
    @classmethod
    def validate_browser_action_delay(cls, value: int) -> int:
        if not 0 <= value <= 60000:
            raise ValueError("BROWSER_ACTION_DELAY_MS must be between 0 and 60000.")
        return value

    @field_validator("llm_provider")
    @classmethod
    def validate_llm_provider(cls, value: str) -> str:
        supported = {"auto", "mock", "openai", "groq", "huggingface"}
        if value not in supported:
            raise ValueError(f"LLM_PROVIDER must be one of: {', '.join(sorted(supported))}")
        return value

    @property
    def app_origin(self) -> str:
        return self.app_base_url.rstrip("/")


def get_settings() -> Settings:
    _load_project_env()
    return Settings()
