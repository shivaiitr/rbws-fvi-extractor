"""Tests for Gemini / provider settings resolution."""

import os

from rbws_extractor.config import (
    DEFAULT_GEMINI_MODEL,
    GEMINI_OPENAI_COMPAT_BASE_URL,
    Settings,
    get_settings,
)


def test_gemini_provider_resolves_base_url_and_model(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("RBWS_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-flash-latest")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    settings = Settings()
    assert settings.llm_provider == "gemini"
    assert settings.resolved_api_key == "test-gemini-key"
    assert settings.resolved_model == "gemini-flash-latest"
    assert settings.resolved_base_url == GEMINI_OPENAI_COMPAT_BASE_URL


def test_auto_select_gemini_when_only_gemini_key(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("RBWS_LLM_PROVIDER", "openai")
    monkeypatch.setenv("GEMINI_API_KEY", "only-gemini")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    settings = Settings()
    assert settings.llm_provider == "gemini"
    assert settings.resolved_model == DEFAULT_GEMINI_MODEL
