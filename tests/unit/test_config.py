from __future__ import annotations

from pytest import MonkeyPatch

from novel_agent.config import AppEnvironment, AppSettings


def test_settings_defaults() -> None:
    settings = AppSettings(_env_file=None)

    assert settings.environment is AppEnvironment.DEVELOPMENT
    assert settings.log_level == "INFO"


def test_environment_overrides_defaults(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("NOVEL_AGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("NOVEL_AGENT_LOG_LEVEL", "WARNING")

    settings = AppSettings(_env_file=None)

    assert settings.environment is AppEnvironment.TEST
    assert settings.log_level == "WARNING"
