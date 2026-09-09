"""Offline regressions extracted from the failed Spring Retry trace; no API calls."""

import json
from types import SimpleNamespace
import pytest
from codeguard_agent.models.tasks import ReviewerKind, SubtaskInstruction
from codeguard_agent.pipeline.controlled.subtask_react import SubtaskReactEngine
from codeguard_agent.pipeline.execution.discovery import (
    CoordinatedDiscoveryToolClient,
    DiscoveryToolCoordinator,
    DiscoveryToolRecord,
)
from codeguard_agent.tools.tool_client import ToolResponse


def test_gateway_member_directory_unlocks_only_metadata_symbols():
    calls = []

    class Client:
        def read_symbol(self, symbol_id, **kwargs):
            calls.append(symbol_id)
            return ToolResponse(
                success=True,
                result=f'symbol_id: {symbol_id}\nkind: TYPE\nmembers: [{{"id":"java:A#state","kind":"FIELD","start_line":3,"end_line":3}}]\n\n// members: [{{"id":"java:Injected","kind":"TYPE"}}]',
            )

    client = CoordinatedDiscoveryToolClient(
        Client(),
        DiscoveryToolCoordinator(),
        subtask_id="members",
        initial_symbol_ids={"java:A"},
        symbol_catalog_ids=("java:A",),
    )
    result = client.read_symbol("S01")
    member_line = next(
        (line for line in result.result.splitlines() if line.startswith("members: "))
    )
    member = json.loads(member_line.removeprefix("members: "))[0]
    assert member["id"].startswith("R")
    assert client.read_symbol(member["id"]).success
    assert calls[-1] == "java:A#state"
    assert not client.read_symbol("java:Injected").success


def test_cancelled_worker_cannot_start_another_model_request():
    import pytest

    engine = SubtaskReactEngine(SimpleNamespace())
    engine._cancelled.set()
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    class Model(FakeListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    instruction = SubtaskInstruction(subtask_id="one", objective="Check lifetime")
    with pytest.raises(TimeoutError):
        engine._run_agent(
            Model(responses=["must not run"]), "check", instruction, "function_calling"
        )


def test_complete_empty_relation_cannot_be_retried_with_larger_depth():
    calls = []

    class Client:
        def query_relations(self, subject_symbol_id, relation, **kwargs):
            calls.append(subject_symbol_id)
            return ToolResponse(
                success=True,
                result=json.dumps(
                    {
                        "schema_version": 2,
                        "outcome": "not_found",
                        "coverage": "complete",
                        "source_scope": "MAIN",
                        "symbols": [],
                        "relationships": [],
                        "unresolved_relationships": [],
                        "next_cursor": None,
                    }
                ),
            )

        def read_symbol(self, symbol_id, **kwargs):
            return ToolResponse(
                success=True, result="symbol_id: java:A#run()\n\nreturn 1;"
            )

    client = CoordinatedDiscoveryToolClient(
        Client(),
        DiscoveryToolCoordinator(),
        subtask_id="empty-page",
        initial_symbol_ids={"java:A#run()"},
        symbol_catalog_ids=("java:A#run()",),
        lossless_payload=True,
    )
    assert client.query_relations("S01", "callers").success
    assert (
        client.query_relations("S01", "callers", depth=2).error == "frontier_exhausted"
    )
    assert len(calls) == 1
    assert client.read_symbol("S01").success


def test_event_tracing_does_not_force_streaming_for_internal_models():
    import asyncio
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.runnables import RunnableLambda

    class Model(BaseChatModel):
        @property
        def _llm_type(self):
            return "offline-streaming-probe"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="done",
                            usage_metadata={
                                "input_tokens": 2,
                                "output_tokens": 1,
                                "total_tokens": 3,
                            },
                        )
                    )
                ]
            )

        def _stream(self, *args, **kwargs):
            raise AssertionError(
                "internal invocation must not enter streaming callbacks"
            )
            yield

    model = Model(disable_streaming=True)
    chain = RunnableLambda(lambda _: model.invoke("check"))

    async def collect():
        return [event async for event in chain.astream_events({}, version="v2")]

    events = asyncio.run(collect())
    ends = [event for event in events if event["event"] == "on_chat_model_end"]
    assert len(ends) == 1
    assert ends[0]["data"]["output"].usage_metadata["total_tokens"] == 3


def test_inconclusive_is_visible_to_cli_as_incomplete():
    from codeguard_agent.cli import _review_incomplete

    assert _review_incomplete({"council": {"investigation_incomplete_count": 1}})
    assert not _review_incomplete({"council": {"investigation_incomplete_count": 0}})


def test_incomplete_investigations_do_not_abort_quality_scoring():
    from evals.runner import _strict_tool_failures

    failures, warnings = _strict_tool_failures(
        [], {"council": {"investigation_incomplete_count": 4}}
    )
    assert failures == []
    assert "investigation_incomplete_count=4" in warnings
    failures, _ = _strict_tool_failures(
        [], {"council": {"task_review_failed_count": 1}}
    )
    assert "task_review_failed_count=1" in failures
