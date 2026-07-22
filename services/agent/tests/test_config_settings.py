"""Phase 2 budget configuration tests."""

from __future__ import annotations

import pytest

from codeguard_agent.config import Settings
from codeguard_agent import config as config_module
from codeguard_agent.models.tasks import ReviewBudget
from codeguard_agent.pipeline import orchestrator as orchestrator_module
from codeguard_agent.pipeline.graph import ReviewState


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


def test_phase2_budget_defaults(monkeypatch):
    monkeypatch.delenv("CODEGUARD_MAX_REVIEW_TASKS", raising=False)
    monkeypatch.delenv("CODEGUARD_MAX_TASKS_PER_FILE", raising=False)
    monkeypatch.delenv("CODEGUARD_MAX_REACT_TASKS", raising=False)

    settings = Settings.from_env()

    assert settings.max_review_tasks == 100
    assert settings.max_tasks_per_file == 10
    assert settings.max_react_tasks == 20


def test_phase2_budget_env_override(monkeypatch):
    monkeypatch.setenv("CODEGUARD_MAX_REVIEW_TASKS", "17")
    monkeypatch.setenv("CODEGUARD_MAX_TASKS_PER_FILE", "3")
    monkeypatch.setenv("CODEGUARD_MAX_REACT_TASKS", "30")

    settings = Settings.from_env()

    assert settings.max_review_tasks == 17
    assert settings.max_tasks_per_file == 3
    assert settings.max_react_tasks == 30


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
        ("CODEGUARD_MAX_REACT_TASKS", "0"),
        ("CODEGUARD_MAX_REACT_TASKS", "-1"),
        ("CODEGUARD_MAX_REACT_TASKS", "many"),
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
    assert "review_budget" in ReviewState.__annotations__
