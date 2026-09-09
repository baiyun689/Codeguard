"""Real agent protocol tests with scripted local models; no provider requests."""

import json
from types import SimpleNamespace
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from codeguard_agent.models.tasks import (
    ReviewerKind,
    SubtaskInstruction,
    TaskSymbolContext,
)
from codeguard_agent.models.tasks.symbols import ResolvedReference, ResolvedSymbol
from codeguard_agent.pipeline.controlled.subtask_react import SubtaskReactEngine
from codeguard_agent.pipeline.execution.discovery import (
    CoordinatedDiscoveryToolClient,
    DiscoveryToolCoordinator,
)
from codeguard_agent.tools.tool_client import ToolResponse


@pytest.mark.parametrize(
    "rounds,tool_budget,early", [(1, 8, False), (6, 1, False), (6, 8, True)]
)
@pytest.mark.parametrize("terminal_outcome", ["findings", "no_finding", "inconclusive"])
def test_agent_consumes_last_observation_and_finishes_in_same_history(
    rounds, tool_budget, early, terminal_outcome
):
    backend_calls = []

    class Client:
        def read_symbol(self, symbol_id, **kwargs):
            backend_calls.append(symbol_id)
            return ToolResponse(
                success=True,
                result=f"symbol_id: {symbol_id}\nkind: METHOD\nfile: A.java\nlines: 1-1\ntruncated: false\n\nvoid run() {{ changed(); }} // decisive-source",
            )

    class Model(BaseChatModel):
        calls: int = 0
        offered_tools: list = []
        tool_choices: list = []
        histories: list = []

        @property
        def _llm_type(self):
            return "offline-investigation"

        def bind_tools(self, tools, **kwargs):
            self.offered_tools.append(
                [
                    tool["function"]["name"] if isinstance(tool, dict) else tool.name
                    for tool in tools
                ]
            )
            self.tool_choices.append(kwargs.get("tool_choice"))
            return self

        def _generate(self, messages, **kwargs):
            self.histories.append(messages)
            self.calls += 1
            if self.calls == 1:
                call = {
                    "name": "LlmInvestigationDecision",
                    "id": "read",
                    "args": {
                        "status": "need_evidence",
                        "assessment": "Need changed branch source",
                        "queries": [
                            {
                                "tool": "read_symbol",
                                "expected_symbol_name": "run",
                                "arguments": {
                                    "symbol_id": "S01",
                                    "fact_question": "Which branch invokes changed?",
                                },
                            }
                        ],
                    },
                }
            else:
                observation = next(
                    (
                        message
                        for message in messages
                        if isinstance(message, ToolMessage)
                    )
                )
                assert "decisive-source" in observation.content
                assert any(
                    ("earlier-question" in str(message.content) for message in messages)
                )
                observation_id = "T01"
                call = {
                    "name": "LlmInvestigationResult",
                    "id": "finish",
                    "args": {
                        "subtask_id": "investigation",
                        "outcome": terminal_outcome,
                        "findings": [
                            {
                                "claim": "Changed behavior",
                                "mechanism": "Decisive source",
                                "location_file": "A.java",
                                "location_line": 1,
                                "observations": [
                                    {
                                        "observation_id": observation_id,
                                        "role": "mechanism",
                                    }
                                ],
                            }
                        ]
                        if terminal_outcome == "findings"
                        else [],
                    },
                }
                call = {
                    "name": "LlmInvestigationDecision",
                    "id": "finish",
                    "args": {
                        "status": "blocked"
                        if terminal_outcome == "inconclusive"
                        else "ready",
                        "assessment": "Source establishes the bounded result",
                        "observation_refs": ["T01"],
                        "result": call["args"],
                    },
                }
            return ChatResult(
                generations=[
                    ChatGeneration(message=AIMessage(content="", tool_calls=[call]))
                ]
            )

    client = CoordinatedDiscoveryToolClient(
        Client(),
        DiscoveryToolCoordinator(),
        max_tool_calls=tool_budget,
        initial_symbol_ids={"java:A#run()"},
        symbol_catalog_ids=("java:A#run()",),
        subtask_id="investigation",
    )
    instruction = SubtaskInstruction(
        subtask_id="investigation",
        objective="earlier-question",
        initial_symbol_ids=("java:A#run()",),
        allowed_tools=("read_symbol", "query_relations"),
    )
    model = Model()
    engine = SubtaskReactEngine(client, max_rounds=rounds, max_tool_calls=tool_budget)
    engine._finalize_after_budget = lambda *args, **kwargs: pytest.fail(
        "must not rebuild a separate evidence catalog"
    )
    outcome = engine.run(
        model,
        task=SimpleNamespace(file="A.java", patch="+changed();"),
        symbol_context=None,
        instruction=instruction,
        structured_method="function_calling",
        max_retries=1,
    )
    assert outcome.result and outcome.result.outcome == terminal_outcome
    assert model.calls == 2 and backend_calls == ["java:A#run()"]
    if early:
        assert model.offered_tools[-1] == ["LlmInvestigationDecision"]
        assert not outcome.reason
    else:
        assert model.offered_tools[-1] == ["LlmInvestigationDecision"]
        if terminal_outcome != "inconclusive":
            assert "context_conclusion" in outcome.reason
        else:
            assert outcome.status == "inconclusive"
        assert model.tool_choices[-1] == {
            "type": "function",
            "function": {"name": "LlmInvestigationDecision"},
        }
        assert 'phase="conclude"' in model.histories[-1][-1].content
    assert model.histories[0][0].content == model.histories[-1][0].content
    assert 'phase="explore"' in model.histories[0][-1].content
    assert model.offered_tools[0] == ["LlmInvestigationDecision"]


@pytest.mark.parametrize("bad_tool", [False, True])
def test_invalid_conclusion_never_reopens_tools_or_starts_another_synthesis(bad_tool):
    backend_calls = []

    class Client:
        def read_symbol(self, symbol_id, **kwargs):
            backend_calls.append(symbol_id)
            return ToolResponse(success=True, result="decisive-source")

    class Model(BaseChatModel):
        calls: int = 0

        @property
        def _llm_type(self):
            return "offline-invalid-conclusion"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, **kwargs):
            self.calls += 1
            assert self.calls <= 2
            if self.calls == 1 or bad_tool:
                call = {
                    "name": "read_symbol",
                    "id": str(self.calls),
                    "args": {
                        "symbol_id": "S01",
                        "fact_question": "Which branch invokes changed?",
                    },
                }
            else:
                call = {
                    "name": "LlmInvestigationResult",
                    "id": "bad",
                    "args": {"outcome": "invalid"},
                }
            if self.calls == 1:
                call = {
                    "name": "LlmInvestigationDecision",
                    "id": "read",
                    "args": {
                        "status": "need_evidence",
                        "assessment": "Need changed branch source",
                        "queries": [
                            {
                                "tool": "read_symbol",
                                "expected_symbol_name": "run",
                                "arguments": call["args"],
                            }
                        ],
                    },
                }
            return ChatResult(
                generations=[
                    ChatGeneration(message=AIMessage(content="", tool_calls=[call]))
                ]
            )

    client = CoordinatedDiscoveryToolClient(
        Client(),
        DiscoveryToolCoordinator(),
        max_tool_calls=8,
        initial_symbol_ids={"java:A#run()"},
        symbol_catalog_ids=("java:A#run()",),
        subtask_id="bad",
    )
    instruction = SubtaskInstruction(
        subtask_id="bad",
        objective="Check behavior",
        initial_symbol_ids=("java:A#run()",),
        allowed_tools=("read_symbol",),
    )
    engine = SubtaskReactEngine(client, max_rounds=1)
    engine._finalize_after_budget = lambda *args, **kwargs: pytest.fail(
        "second conclusion is forbidden"
    )
    model = Model()
    result = engine.run(
        model,
        task=SimpleNamespace(file="A.java", patch="+changed();"),
        symbol_context=None,
        instruction=instruction,
        structured_method="function_calling",
        max_retries=1,
    )
    assert result.status == "failed"
    assert model.calls == 2 and len(backend_calls) == 1
