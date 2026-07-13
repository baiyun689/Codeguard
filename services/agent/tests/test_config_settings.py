"""Phase 2 budget configuration tests."""

from __future__ import annotations

import pytest

from codeguard_agent.config import Settings
from codeguard_agent.models.council import Verdict
from codeguard_agent.models.tasks import ReviewBudget
from codeguard_agent.pipeline import graph as graph_module
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


def test_evidence_rounds_default_to_two_in_dataclass_and_environment(monkeypatch):
    monkeypatch.delenv("CODEGUARD_MAX_EVIDENCE_ROUNDS", raising=False)

    assert _settings().max_evidence_rounds == 2
    assert Settings.from_env().max_evidence_rounds == 2


@pytest.mark.parametrize("value", ["1", "2"])
def test_evidence_rounds_accept_only_supported_values(monkeypatch, value):
    monkeypatch.setenv("CODEGUARD_MAX_EVIDENCE_ROUNDS", value)

    assert Settings.from_env().max_evidence_rounds == int(value)


@pytest.mark.parametrize("value", ["0", "-1", "many", "3"])
def test_evidence_rounds_reject_invalid_values_with_variable_name(monkeypatch, value):
    monkeypatch.setenv("CODEGUARD_MAX_EVIDENCE_ROUNDS", value)

    with pytest.raises(ValueError, match="CODEGUARD_MAX_EVIDENCE_ROUNDS"):
        Settings.from_env()


def test_default_settings_replans_severity_once_then_stops_after_round_two():
    settings = _settings()
    verdict = Verdict(
        candidate_id="candidate-1",
        action="needs_more_evidence",
        reason_code="need_severity",
        requested_purpose="severity",
    )
    state: ReviewState = {
        "council_verdicts": [verdict],
        "evidence_round": 1,
        "max_evidence_rounds": settings.max_evidence_rounds,
    }

    assert graph_module._route_after_council_judge(state) == "evidence_planner"

    state["evidence_round"] = 2
    assert graph_module._route_after_council_judge(state) == "END"


def test_phase2_budget_defaults(monkeypatch):
    monkeypatch.delenv("CODEGUARD_MAX_REVIEW_TASKS", raising=False)
    monkeypatch.delenv("CODEGUARD_MAX_TASKS_PER_FILE", raising=False)

    settings = Settings.from_env()

    assert settings.max_review_tasks == 100
    assert settings.max_tasks_per_file == 10


def test_phase2_budget_env_override(monkeypatch):
    monkeypatch.setenv("CODEGUARD_MAX_REVIEW_TASKS", "17")
    monkeypatch.setenv("CODEGUARD_MAX_TASKS_PER_FILE", "3")

    settings = Settings.from_env()

    assert settings.max_review_tasks == 17
    assert settings.max_tasks_per_file == 3


@pytest.mark.parametrize(
    "name,value",
    [
        ("CODEGUARD_MAX_REVIEW_TASKS", "0"),
        ("CODEGUARD_MAX_REVIEW_TASKS", "-1"),
        ("CODEGUARD_MAX_REVIEW_TASKS", "many"),
        ("CODEGUARD_MAX_TASKS_PER_FILE", "0"),
        ("CODEGUARD_MAX_TASKS_PER_FILE", "-1"),
        ("CODEGUARD_MAX_TASKS_PER_FILE", "many"),
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
