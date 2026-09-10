"""Phase 2 budget configuration tests."""

from __future__ import annotations
import pytest
from codeguard_agent.config import Settings
from codeguard_agent import config as config_module


def _settings(**overrides) -> Settings:
    values = {
        "provider": "mock",
        "model": "",
        "api_key": "",
        "api_base_url": "",
        "max_retries": 3,
        "structured_method": "function_calling",
        "disable_thinking": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_default_settings_has_no_evidence_round_config():
    settings = _settings()
    assert not hasattr(settings, "max_evidence_rounds")


def test_tool_server_token_is_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("CODEGUARD_TOOL_SERVER_TOKEN", "tool-token")
    monkeypatch.setattr(config_module, "_load_dotenv", lambda: None)
    assert Settings.from_env().tool_server_token == "tool-token"


def test_evidence_mode_defaults_to_full(monkeypatch):
    monkeypatch.delenv("CODEGUARD_EVIDENCE_MODE", raising=False)
    monkeypatch.setattr(config_module, "_load_dotenv", lambda: None)
    assert Settings.from_env().evidence_mode == "full"


def test_evidence_mode_off(monkeypatch):
    monkeypatch.setenv("CODEGUARD_EVIDENCE_MODE", "off")
    monkeypatch.setattr(config_module, "_load_dotenv", lambda: None)
    assert Settings.from_env().evidence_mode == "off"


def test_evidence_mode_invalid_falls_back_to_full(monkeypatch):
    monkeypatch.setenv("CODEGUARD_EVIDENCE_MODE", "no-gate")
    monkeypatch.setattr(config_module, "_load_dotenv", lambda: None)
    assert Settings.from_env().evidence_mode == "full"


def test_discovery_mode_defaults_to_controlled(monkeypatch):
    monkeypatch.delenv("CODEGUARD_DISCOVERY_MODE", raising=False)
    monkeypatch.setattr(config_module, "_load_dotenv", lambda: None)
    assert Settings.from_env().discovery_mode == "controlled"


def test_controlled_discovery_mode_and_budgets_are_configurable(monkeypatch):
    monkeypatch.setattr(config_module, "_load_dotenv", lambda: None)
    monkeypatch.setenv("CODEGUARD_DISCOVERY_MODE", "controlled")
    monkeypatch.setenv("CODEGUARD_CONTROLLED_INITIAL_TOOL_BUDGET", "9")
    monkeypatch.setenv("CODEGUARD_CONTROLLED_DELTA_TOOL_BUDGET", "0")
    monkeypatch.setenv("CODEGUARD_CONTROLLED_MAX_PATH_DEPTH", "2")
    settings = Settings.from_env()
    assert settings.discovery_mode == "controlled"
    assert settings.controlled_max_path_depth == 2


def test_unknown_discovery_mode_falls_back_to_controlled(monkeypatch):
    monkeypatch.setattr(config_module, "_load_dotenv", lambda: None)
    monkeypatch.setenv("CODEGUARD_DISCOVERY_MODE", "anything")
    assert Settings.from_env().discovery_mode == "controlled"


def test_phase2_budget_defaults(monkeypatch):
    monkeypatch.delenv("CODEGUARD_MAX_REVIEW_TASKS", raising=False)
    monkeypatch.delenv("CODEGUARD_MAX_TASKS_PER_FILE", raising=False)
    monkeypatch.delenv("CODEGUARD_GRAPH_BUILD_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("CODEGUARD_CONTROLLED_SUBTASK_MAX_TOOL_CALLS", raising=False)
    monkeypatch.setattr(config_module, "_load_dotenv", lambda: None)
    settings = Settings.from_env()
    assert settings.max_review_tasks == 100
    assert settings.max_tasks_per_file == 10
    assert settings.graph_build_timeout_seconds == 120
    assert settings.controlled_max_subtasks_per_task == 8
    assert settings.controlled_subtask_max_tool_calls == 10


def test_phase2_budget_env_override(monkeypatch):
    monkeypatch.setenv("CODEGUARD_MAX_REVIEW_TASKS", "17")
    monkeypatch.setenv("CODEGUARD_MAX_TASKS_PER_FILE", "3")
    monkeypatch.setenv("CODEGUARD_GRAPH_BUILD_TIMEOUT_SECONDS", "240")
    settings = Settings.from_env()
    assert settings.max_review_tasks == 17
    assert settings.max_tasks_per_file == 3
    assert settings.graph_build_timeout_seconds == 240


def test_controlled_execute_concurrency_is_configurable(monkeypatch):
    monkeypatch.setattr(config_module, "_load_dotenv", lambda: None)
    monkeypatch.setenv("CODEGUARD_CONTROLLED_EXECUTE_CONCURRENCY", "5")
    assert Settings.from_env().controlled_execute_concurrency == 5


def test_subtask_react_budgets_are_configurable(monkeypatch):
    monkeypatch.setattr(config_module, "_load_dotenv", lambda: None)
    monkeypatch.setenv("CODEGUARD_CONTROLLED_EXECUTION_MODE", "subtask_react")
    monkeypatch.setenv("CODEGUARD_CONTROLLED_SUBTASK_MAX_TOOL_CALLS", "7")
    monkeypatch.setenv("CODEGUARD_CONTROLLED_SUBTASK_MAX_ROUNDS", "5")
    monkeypatch.setenv("CODEGUARD_CONTROLLED_SUBTASK_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("CODEGUARD_CONTROLLED_TASK_MAX_TOOL_CALLS", "30")
    monkeypatch.setenv("CODEGUARD_CONTROLLED_MAX_SUBTASKS_PER_REVIEWER", "3")
    monkeypatch.setenv("CODEGUARD_CONTROLLED_MAX_SUBTASKS_PER_TASK", "8")
    settings = Settings.from_env()
    assert settings.controlled_subtask_max_tool_calls == 7
    assert settings.controlled_subtask_max_rounds == 5
    assert settings.controlled_subtask_timeout_seconds == 90
    assert settings.controlled_task_max_tool_calls == 30
    assert settings.controlled_max_subtasks_per_task == 8


def test_local_html_trace_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("CODEGUARD_TRACE_ENABLED", raising=False)
    monkeypatch.setattr(config_module, "_load_dotenv", lambda: None)
    assert _settings().trace_enabled is False
    assert Settings.from_env().trace_enabled is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_local_html_trace_can_be_explicitly_enabled(monkeypatch, value):
    monkeypatch.setenv("CODEGUARD_TRACE_ENABLED", value)
    monkeypatch.setattr(config_module, "_load_dotenv", lambda: None)
    assert Settings.from_env().trace_enabled is True


@pytest.mark.parametrize(
    "name,value",
    [
        ("CODEGUARD_MAX_REVIEW_TASKS", "0"),
        ("CODEGUARD_MAX_REVIEW_TASKS", "-1"),
        ("CODEGUARD_MAX_REVIEW_TASKS", "many"),
        ("CODEGUARD_MAX_TASKS_PER_FILE", "0"),
        ("CODEGUARD_MAX_TASKS_PER_FILE", "-1"),
        ("CODEGUARD_MAX_TASKS_PER_FILE", "many"),
        ("CODEGUARD_GRAPH_BUILD_TIMEOUT_SECONDS", "0"),
        ("CODEGUARD_GRAPH_BUILD_TIMEOUT_SECONDS", "many"),
    ],
)
def test_phase2_budget_rejects_invalid_values(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings.from_env()
