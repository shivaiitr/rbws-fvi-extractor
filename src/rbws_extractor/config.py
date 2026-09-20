"""Application settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "rbws_columns.yaml"

GEMINI_OPENAI_COMPAT_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai/"
)
NVIDIA_OPENAI_COMPAT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
# Vision-capable NIM model (required for scanned FVI PDFs).
DEFAULT_NVIDIA_MODEL = "nvidia/nemotron-nano-12b-v2-vl"


class Settings(BaseSettings):
    """Runtime configuration for the RBWS extractor."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # openai | gemini | nvidia | openrouter | azure | custom
    llm_provider: str = Field(default="openai", alias="RBWS_LLM_PROVIDER")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1", alias="OPENAI_MODEL")
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")

    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default=DEFAULT_GEMINI_MODEL, alias="GEMINI_MODEL")

    nvidia_api_key: str = Field(default="", alias="NVIDIA_API_KEY")
    nvidia_model: str = Field(default=DEFAULT_NVIDIA_MODEL, alias="NVIDIA_MODEL")

    log_level: str = Field(default="INFO", alias="RBWS_LOG_LEVEL")
    render_dpi: int = Field(default=200, alias="RBWS_RENDER_DPI")
    max_pages: int = Field(default=0, alias="RBWS_MAX_PAGES")  # 0 = all
    config_path: Path = Field(default=DEFAULT_CONFIG, alias="RBWS_CONFIG_PATH")

    # auto | text | vision
    extraction_mode: str = Field(default="auto", alias="RBWS_EXTRACTION_MODE")
    min_text_chars: int = Field(default=80, alias="RBWS_MIN_TEXT_CHARS")
    min_text_words: int = Field(default=12, alias="RBWS_MIN_TEXT_WORDS")

    request_timeout_s: float = Field(default=180.0, alias="RBWS_REQUEST_TIMEOUT_S")
    max_retries: int = 4

    @model_validator(mode="after")
    def resolve_provider(self) -> "Settings":
        provider = (self.llm_provider or "openai").strip().lower()

        # Auto-detect provider from available keys when still default openai.
        if provider in {"", "openai"}:
            if self.gemini_api_key and not self.openai_api_key:
                provider = "gemini"
            elif self.nvidia_api_key and not self.openai_api_key:
                provider = "nvidia"
        self.llm_provider = provider

        if provider == "gemini":
            if not self.gemini_api_key and not self.openai_api_key:
                raise ValueError(
                    "GEMINI_API_KEY is required when RBWS_LLM_PROVIDER=gemini"
                )
            if not self.openai_api_key:
                self.openai_api_key = self.gemini_api_key
            if not self.openai_base_url:
                self.openai_base_url = GEMINI_OPENAI_COMPAT_BASE_URL
            if self.gemini_model:
                self.openai_model = self.gemini_model

        elif provider == "nvidia":
            if not self.nvidia_api_key and not self.openai_api_key:
                raise ValueError(
                    "NVIDIA_API_KEY is required when RBWS_LLM_PROVIDER=nvidia"
                )
            if not self.openai_api_key:
                self.openai_api_key = self.nvidia_api_key
            if not self.openai_base_url:
                self.openai_base_url = NVIDIA_OPENAI_COMPAT_BASE_URL
            if self.nvidia_model:
                self.openai_model = self.nvidia_model

        return self

    @property
    def resolved_api_key(self) -> str:
        if self.llm_provider == "gemini":
            return self.gemini_api_key or self.openai_api_key
        if self.llm_provider == "nvidia":
            return self.nvidia_api_key or self.openai_api_key
        return self.openai_api_key

    @property
    def resolved_model(self) -> str:
        return self.openai_model

    @property
    def resolved_base_url(self) -> str | None:
        return self.openai_base_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
