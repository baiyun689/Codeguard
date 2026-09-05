"""Phase 2 budget configuration tests."""

from __future__ import annotations

import pytest

from codeguard_agent.config import Settings
from codeguard_agent import config as config_module
from codeguard_agent.models.tasks import ReviewBudget
from codeguard_agent.pipeline.orchestration import orchestrator as orchestrator_module
from codeguard_agent.models.state import ReviewState


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
    assert settings.controlled_initial_tool_budget == 9
    assert settings.controlled_delta_tool_budget == 0
    assert settings.controlled_max_path_depth == 2


def test_controlled_max_seed_and_topic_limits_can_disable_optional_work(monkeypatch):
    monkeypatch.setattr(config_module, "_load_dotenv", lambda: None)
    for name in (
        "CODEGUARD_CONTROLLED_MAX_SEEDS_PER_CHANGE_UNIT",
        "CODEGUARD_CONTROLLED_MAX_SEEDS_PER_REVIEWER",
        "CODEGUARD_CONTROLLED_MAX_SEEDS_PER_TASK",
        "CODEGUARD_CONTROLLED_MAX_KNOWLEDGE_TOPICS",
    ):
        monkeypatch.setenv(name, "0")
    settings = Settings.from_env()
    assert settings.controlled_max_seeds_per_change_unit == 0
    assert settings.controlled_max_seeds_per_reviewer == 0
    assert settings.controlled_max_seeds_per_task == 0
    assert settings.controlled_max_knowledge_topics == 0


def test_unknown_discovery_mode_falls_back_to_controlled(monkeypatch):
    monkeypatch.setattr(config_module, "_load_dotenv", lambda: None)
    monkeypatch.setenv("CODEGUARD_DISCOVERY_MODE", "anything")
    assert Settings.from_env().discovery_mode == "controlled"


def test_phase2_budget_defaults(monkeypatch):
    monkeypatch.delenv("CODEGUARD_MAX_REVIEW_TASKS", raising=False)
    monkeypatch.delenv("CODEGUARD_MAX_TASKS_PER_FILE", raising=False)

    settings = Settings.from_env()

    assert settings.max_review_tasks == 100
    assert settings.max_tasks_per_file == 10
    assert settings.graph_build_timeout_seconds == 120
    assert settings.controlled_max_seeds_per_change_unit == 4


def test_phase2_budget_env_override(monkeypatch):
    monkeypatch.setenv("CODEGUARD_MAX_REVIEW_TASKS", "17")
    monkeypatch.setenv("CODEGUARD_MAX_TASKS_PER_FILE", "3")
    monkeypatch.setenv("CODEGUARD_GRAPH_BUILD_TIMEOUT_SECONDS", "240")

    settings = Settings.from_env()

    assert settings.max_review_tasks == 17
    assert settings.max_tasks_per_file == 3
    assert settings.graph_build_timeout_seconds == 240


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


def test_orchestrator_passes_budget_through_existing_state_field(monkeypatch):
    captured: dict = {}

    class _Graph:
        def invoke(self, initial, config=None):
            captured.update(initial)
            return {"summary": "", "final_issues": []}

    monkeypatch.setattr(
        orchestrator_module,
        "build_review_graph",
        lambda **_kwargs: _Graph(),
    )
    budget = ReviewBudget(max_tasks_to_review=17, max_tasks_per_file=3)

    orchestrator_module.PipelineOrchestrator(review_budget=budget).run(None, "some diff")

    assert captured["review_budget"] == budget
    assert captured["discovery_mode"] == "controlled"
    assert "review_budget" in ReviewState.__annotations__


def test_direct_discovery_mode_forces_tool_client_off(monkeypatch):
    captured: dict = {}

    class _Graph:
        def invoke(self, initial, config=None):  # noqa: ARG002
            return {"summary": "", "final_issues": []}

    def _build(**kwargs):
        captured.update(kwargs)
        return _Graph()

    monkeypatch.setattr(orchestrator_module, "build_review_graph", _build)
    orchestrator_module.PipelineOrchestrator(discovery_mode="direct").run(
        None,
        "some diff",
        tool_client=object(),
    )

    assert captured["tool_client"] is None
