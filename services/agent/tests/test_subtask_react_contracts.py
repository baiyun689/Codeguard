from __future__ import annotations
from types import SimpleNamespace
import pytest
from codeguard_agent.models.tasks import (
    InvestigationFinding,
    InvestigationObservation,
    InvestigationResult,
    SubtaskInstruction,
)
from codeguard_agent.pipeline.orchestration.graph import _allocate_subtask_budgets
from codeguard_agent.pipeline.controlled.subtask_react import SubtaskReactEngine
from codeguard_agent.pipeline.execution.discovery import (
    CoordinatedDiscoveryToolClient,
    DiscoveryToolCoordinator,
)
from codeguard_agent.tools.tool_client import ToolResponse


def test_counter_role_is_rejected_by_observation_and_evidence_contracts():
    from pydantic import ValidationError
    from codeguard_agent.models.schemas import EvidenceRefSelection

    with pytest.raises(ValidationError):
        InvestigationObservation(observation_id="T01", role="counter")
    with pytest.raises(ValidationError):
        EvidenceRefSelection(alias="T01", role="counter")


@pytest.mark.parametrize(
    "observation_role,evidence_role",
    [("relation", "reachability"), ("mechanism", "mechanism"),
     ("impact", "impact"), ("location", "location")],
)
def test_supported_evidence_purposes_remain_valid(observation_role, evidence_role):
    from codeguard_agent.models.schemas import EvidenceRefSelection

    observation = InvestigationObservation(observation_id="T01", role=observation_role)
    selection = EvidenceRefSelection(alias=observation.observation_id, role=evidence_role)
    assert selection.alias == "T01"
    assert selection.role.value == evidence_role


@pytest.mark.parametrize("close_tools,expected_calls", [(True, 2), (False, 4)])
def test_real_agent_allows_only_one_conclusion_turn_when_tools_close(
    close_tools, expected_calls
):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class Client:
        no_progress_exhausted = False
        budget_exhausted = False

        def read_symbol(self, *args, **kwargs):
            self.no_progress_exhausted = close_tools
            return ToolResponse(success=True, result="source fact")

    class RepeatingModel(BaseChatModel):
        calls: int = 0

        @property
        def _llm_type(self):
            return "repeating-test"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.calls += 1
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "LlmInvestigationDecision",
                                    "args": {
                                        "status": "need_evidence",
                                        "assessment": "Need branch source",
                                        "queries": [
                                            {
                                                "tool": "read_symbol",
                                                "expected_symbol_name": "run",
                                                "arguments": {
                                                    "symbol_id": "S01",
                                                    "fact_question": "Which branch executes?",
                                                },
                                            }
                                        ],
                                    },
                                    "id": str(self.calls),
                                    "type": "tool_call",
                                }
                            ],
                        )
                    )
                ]
            )

    model = RepeatingModel()
    engine = SubtaskReactEngine(Client(), max_rounds=3)
    instruction = SubtaskInstruction(
        subtask_id="stop-test",
        objective="check source",
        initial_symbol_ids=("java:A#run()",),
        allowed_tools=("read_symbol",),
    )
    engine._run_agent(model, "check source", instruction, "function_calling")
    assert model.calls == expected_calls


def test_investigation_result_can_confirm_only_with_local_observation_ids():
    result = InvestigationResult(
        subtask_id="subtask-1",
        outcome="findings",
        findings=(
            InvestigationFinding(
                claim="调用方继续修改返回对象，导致返回行为不兼容",
                mechanism="返回对象的使用方式与变更后的实现不一致",
                location_file="src/A.java",
                location_line=12,
                observations=(
                    InvestigationObservation(observation_id="T01", role="relation"),
                    InvestigationObservation(observation_id="T02", role="mechanism"),
                ),
            ),
        ),
    )
    assert result.outcome == "findings"
    assert [item.observation_id for item in result.findings[0].observations] == [
        "T01",
        "T02",
    ]


def test_subtask_react_rejects_findings_outcome_without_evidence_entries():

    class Client:
        trace_records = ()
        tool_calls = 0
        budget_exhausted = False
        no_progress_exhausted = False

    instruction = SubtaskInstruction(
        subtask_id="subtask-invalid-findings",
        objective="检查返回行为",
        initial_symbol_ids=("java:A#run()",),
        allowed_tools=("read_symbol",),
    )
    engine = SubtaskReactEngine(Client(), max_tool_calls=2, max_rounds=2)
    engine._run_agent = lambda *args: {
        "structured_response": InvestigationResult(
            subtask_id=instruction.subtask_id, outcome="findings"
        )
    }
    outcome = engine.run(
        object(),
        task=SimpleNamespace(file="src/A.java", patch="+return run();"),
        symbol_context=SimpleNamespace(symbols=()),
        instruction=instruction,
        structured_method="function_calling",
        max_retries=1,
    )
    assert outcome.status == "failed"
    assert outcome.reason == "findings_outcome_without_findings"
    assert outcome.events == ["subtask_protocol_failed"]


def test_discovery_client_rejects_invalid_allowed_path_kind():
    with pytest.raises(ValueError, match="allowed_path_kind"):
        CoordinatedDiscoveryToolClient(
            object(), DiscoveryToolCoordinator(), allowed_path_kind="not-a-path-kind"
        )


def test_subtask_budget_split_uses_remainder_without_zero_budget_tasks():
    assert _allocate_subtask_budgets(5, total_budget=16, per_subtask_limit=4) == (
        4,
        3,
        3,
        3,
        3,
    )
    assert _allocate_subtask_budgets(8, total_budget=3, per_subtask_limit=4) == (
        1,
        1,
        1,
        0,
        0,
        0,
        0,
        0,
    )


def test_graph_recursion_is_reported_as_inconclusive_not_task_failure():

    class GraphRecursionError(Exception):
        pass

    class Client:
        trace_records = ()

    instruction = SubtaskInstruction(
        subtask_id="subtask-1",
        objective="检查返回行为",
        initial_symbol_ids=("java:A#run()",),
        allowed_tools=("read_symbol",),
    )
    engine = SubtaskReactEngine(Client(), max_tool_calls=2, max_rounds=2)
    engine._run_agent = lambda *args: (_ for _ in ()).throw(GraphRecursionError())
    outcome = engine.run(
        object(),
        task=SimpleNamespace(file="src/A.java", patch="+return run();"),
        symbol_context=SimpleNamespace(symbols=()),
        instruction=instruction,
        structured_method="function_calling",
        max_retries=1,
    )
    assert outcome.status == "inconclusive"
    assert outcome.reason == "subtask_recursion_limit"
    assert outcome.events == ["subtask_inconclusive"]


def test_connection_failure_records_cause_types_without_secrets():
    class OpenAIConnectionError(Exception):
        pass

    def fail(*args):
        raise OpenAIConnectionError("secret-token") from ConnectionResetError("secret-token")

    engine = SubtaskReactEngine(SimpleNamespace(trace_records=()), max_tool_calls=2, max_rounds=2)
    engine._run_agent = fail
    outcome = engine.run(
        object(), task=SimpleNamespace(file="src/A.java", patch="+return run();"),
        symbol_context=SimpleNamespace(symbols=()),
        instruction=SubtaskInstruction(
            subtask_id="s1", objective="检查返回行为",
            initial_symbol_ids=("java:A#run()",), allowed_tools=("read_symbol",),
        ),
        structured_method="function_calling", max_retries=1,
    )
    assert outcome.status == "failed"
    assert outcome.reason == "OpenAIConnectionError:ConnectionResetError"
    assert "secret-token" not in repr(outcome)


def test_graph_recursion_after_budget_rejection_is_labeled_as_budget_gap():

    class GraphRecursionError(Exception):
        pass

    class Client:
        trace_records = ()
        budget_exhausted = True

    instruction = SubtaskInstruction(
        subtask_id="subtask-budget-recursion",
        objective="检查返回行为",
        initial_symbol_ids=("java:A#run()",),
        allowed_tools=("read_symbol",),
    )
    engine = SubtaskReactEngine(Client(), max_tool_calls=2, max_rounds=2)
    engine._run_agent = lambda *args: (_ for _ in ()).throw(GraphRecursionError())
    outcome = engine.run(
        object(),
        task=SimpleNamespace(file="src/A.java", patch="+return run();"),
        symbol_context=SimpleNamespace(symbols=()),
        instruction=instruction,
        structured_method="function_calling",
        max_retries=1,
    )
    assert outcome.status == "inconclusive"
    assert outcome.reason == "tool_budget_exceeded"
    assert outcome.events == ["subtask_tool_budget_exceeded"]


def test_rejected_budget_call_cannot_be_reported_as_no_finding():

    class Client:
        trace_records = ()
        tool_calls = 1
        budget_exhausted = True

    instruction = SubtaskInstruction(
        subtask_id="subtask-budget",
        objective="检查返回行为",
        initial_symbol_ids=("java:A#run()",),
        allowed_tools=("read_symbol",),
    )
    engine = SubtaskReactEngine(Client(), max_tool_calls=2, max_rounds=2)
    engine._run_agent = lambda *args: {
        "structured_response": InvestigationResult(
            subtask_id="subtask-budget", outcome="no_finding"
        )
    }
    outcome = engine.run(
        object(),
        task=SimpleNamespace(file="src/A.java", patch="+return run();"),
        symbol_context=SimpleNamespace(symbols=()),
        instruction=instruction,
        structured_method="function_calling",
        max_retries=1,
    )
    assert outcome.status == "inconclusive"
    assert outcome.reason == "tool_budget_exceeded"
    assert outcome.events == ["subtask_tool_budget_exceeded"]


def test_no_progress_cannot_be_reported_as_no_finding():

    class Client:
        trace_records = ()
        tool_calls = 2
        budget_exhausted = False
        no_progress_exhausted = True

    instruction = SubtaskInstruction(
        subtask_id="subtask-no-progress",
        objective="检查返回行为",
        initial_symbol_ids=("java:A#run()",),
        allowed_tools=("read_symbol",),
    )
    engine = SubtaskReactEngine(Client(), max_tool_calls=2, max_rounds=2)
    engine._run_agent = lambda *args: {
        "structured_response": InvestigationResult(
            subtask_id="subtask-no-progress", outcome="no_finding"
        )
    }
    outcome = engine.run(
        object(),
        task=SimpleNamespace(file="src/A.java", patch="+return run();"),
        symbol_context=SimpleNamespace(symbols=()),
        instruction=instruction,
        structured_method="function_calling",
        max_retries=1,
    )
    assert outcome.status == "inconclusive"
    assert outcome.reason == "no_progress_detected"
    assert outcome.events == ["subtask_no_progress_terminated"]


def test_findings_after_budget_rejection_keep_successful_observations():

    class Client:
        trace_records = ()
        observation_aliases = {"T01": "call-source"}
        tool_calls = 1
        budget_exhausted = True

    instruction = SubtaskInstruction(
        subtask_id="subtask-budget-findings",
        objective="检查返回行为",
        initial_symbol_ids=("java:A#run()",),
        allowed_tools=("read_symbol",),
    )
    engine = SubtaskReactEngine(Client(), max_tool_calls=2, max_rounds=2)
    engine._run_agent = lambda *args: {
        "structured_response": InvestigationResult(
            subtask_id="subtask-budget-findings",
            outcome="findings",
            findings=(
                InvestigationFinding(
                    claim="返回行为改变并影响调用方",
                    mechanism="工具返回的源码显示返回表达式已变化",
                    location_file="src/A.java",
                    location_line=12,
                    observations=(
                        InvestigationObservation(
                            observation_id="T01", role="mechanism"
                        ),
                    ),
                ),
            ),
        )
    }
    outcome = engine.run(
        object(),
        task=SimpleNamespace(file="src/A.java", patch="+return run();"),
        symbol_context=SimpleNamespace(symbols=()),
        instruction=instruction,
        structured_method="function_calling",
        max_retries=1,
    )
    assert outcome.status == "complete"
    assert outcome.reason == "tool_budget_exceeded_after_findings"
    assert outcome.result is not None
    assert outcome.result.outcome == "findings"
