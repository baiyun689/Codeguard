"""One decision per model response, exercised through the real agent and SDK."""

import json
from types import SimpleNamespace
import httpx
import pytest
from langchain_openai import ChatOpenAI
from codeguard_agent.models.tasks import ReviewerKind, SubtaskInstruction
from codeguard_agent.pipeline.controlled.subtask_react import SubtaskReactEngine
from codeguard_agent.pipeline.execution.discovery import (
    CoordinatedDiscoveryToolClient,
    DiscoveryToolCoordinator,
)
from codeguard_agent.tools.tool_client import ToolResponse


@pytest.mark.parametrize(
    "fault", [None, "unknown_symbol", "result_with_query", "bad_batch", "too_many_refs"]
)
@pytest.mark.parametrize("verbose", [False, True])
def test_decision_consumes_observations_before_querying_or_finishing(fault, verbose):
    requests, executed = ([], [])
    query = {
        "tool": "read_symbol",
        "expected_symbol_name": "run",
        "arguments": {"symbol_id": "S01", "fact_question": "Does run validate input?"},
    }
    terminal = {
        "subtask_id": "step",
        "outcome": "findings",
        "findings": [
            {
                "claim": "Validation bypassed",
                "mechanism": "Changed branch omits validation",
                "location_file": "A.java",
                "observations": [{"observation_id": "T01", "role": "mechanism"}],
            }
        ],
    }

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert [tool["function"]["name"] for tool in body["tools"]] == [
            "LlmInvestigationDecision"
        ]
        assert body["tool_choice"] == {
            "type": "function",
            "function": {"name": "LlmInvestigationDecision"},
        }
        if len(requests) == 1:
            decision = {
                "status": "need_evidence",
                "assessment": "Need the changed implementation",
                "observation_refs": [],
                "queries": [query],
            }
        elif fault and len(requests) == 2:
            decision = {
                "status": "need_evidence",
                "assessment": "Need a related implementation",
                "observation_refs": ["T01"],
                "queries": [query],
            }
            if fault == "unconsumed":
                decision["observation_refs"] = []
            elif fault == "unknown_symbol":
                decision["queries"] = [
                    {**query, "arguments": {**query["arguments"], "symbol_id": "S99"}}
                ]
            elif fault == "unknown_ref":
                decision["observation_refs"] = ["T01", "T99"]
            elif fault == "bad_batch":
                decision["queries"] = [
                    query,
                    {**query, "arguments": {**query["arguments"], "symbol_id": "S99"}},
                ]
            elif fault == "too_many_refs":
                decision["queries"] = []
                decision["result"] = {
                    **terminal,
                    "findings": [
                        {
                            **terminal["findings"][0],
                            "observations": terminal["findings"][0]["observations"] * 4,
                        }
                    ],
                }
            elif fault == "empty_observations":
                decision["queries"] = []
                decision["result"] = {
                    **terminal,
                    "findings": [{**terminal["findings"][0], "observations": []}],
                }
            else:
                decision["status"] = "ready"
                decision["result"] = terminal
        else:
            assert len(requests) == (3 if fault else 2)
            assert any(
                ("decisive-source" in str(message) for message in body["messages"])
            )
            if fault:
                assert any(
                    (
                        "decision_rejected" in str(message)
                        for message in body["messages"]
                    )
                )
            if fault == "too_many_refs":
                assert any(
                    (
                        "result.findings.0.observations" in str(message)
                        and "at most 3" in str(message)
                        for message in body["messages"]
                    )
                )
            decision = {
                "status": "ready",
                "assessment": "T01 confirms the missing validation",
                "observation_refs": ["T01"],
                "queries": [],
                "result": terminal,
            }
        if verbose:
            decision["assessment"] += " Extra commentary." * 80
        return httpx.Response(
            200,
            json={
                "id": "offline",
                "object": "chat.completion",
                "created": 1,
                "model": "offline",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"decision{len(requests)}",
                                    "type": "function",
                                    "function": {
                                        "name": "LlmInvestigationDecision",
                                        "arguments": json.dumps(decision),
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    class Backend:
        def read_symbol(self, symbol_id, **kwargs):
            executed.append((symbol_id, kwargs))
            return ToolResponse(
                True,
                f"symbol_id: {symbol_id}\nkind: METHOD\nfile: A.java\nlines: 1-1\n\nvoid run() {{}} // decisive-source",
            )

    client = CoordinatedDiscoveryToolClient(
        Backend(),
        DiscoveryToolCoordinator(),
        max_tool_calls=8,
        initial_symbol_ids={"java:A#run()"},
        symbol_catalog_ids=("java:A#run()",),
        subtask_id="step",
    )
    instruction = SubtaskInstruction(
        subtask_id="step",
        objective="Check validation",
        initial_symbol_ids=("java:A#run()",),
        allowed_tools=("read_symbol", "query_relations"),
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        model = ChatOpenAI(
            model="offline",
            api_key="offline",
            base_url="http://offline.invalid/v1",
            http_client=http,
            max_retries=0,
        )
        outcome = SubtaskReactEngine(client, max_rounds=6).run(
            model,
            task=SimpleNamespace(file="A.java", patch="+changed();"),
            symbol_context=None,
            instruction=instruction,
            structured_method="function_calling",
            max_retries=1,
        )
    assert outcome.status == "complete", outcome.reason
    assert outcome.reason == ""
    assert len(requests) == (3 if fault else 2)
    assert len(executed) == 1
    assert "fact_question" not in executed[0][1]
    assert "expected_symbol_name" not in client.trace_records[0].arguments


@pytest.mark.parametrize("malformed", [False, True])
@pytest.mark.parametrize("terminal_outcome", ["inconclusive", "failed"])
def test_parallel_observations_or_repeated_rejections_stay_within_decision_budget(
    malformed, terminal_outcome
):
    requests, executed = ([], [])

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        offered = body["tools"][0]["function"]["name"]
        if 'phase="conclude"' in body["messages"][-1]["content"]:
            assert malformed and len(requests) == 4
            result = {
                "assessment": "No usable evidence",
                "queries": [],
                "result": {
                    "subtask_id": "step",
                    "outcome": terminal_outcome,
                    "limitations": ["No usable evidence"],
                },
            }
        elif malformed:
            assert len(requests) <= 3
            result = {"status": "need_evidence", "assessment": "No query provided"}
        elif len(requests) == 1:
            result = {
                "status": "need_evidence",
                "assessment": "Need two independent declarations",
                "queries": [
                    {
                        "tool": "read_symbol",
                        "expected_symbol_name": name,
                        "arguments": {
                            "symbol_id": f"S0{index}",
                            "fact_question": "What state is accessed?",
                        },
                    }
                    for index, name in enumerate(("close", "run"), 1)
                ],
            }
        else:
            assert len(requests) == 2
            query_ids = {
                call["id"]
                for message in body["messages"]
                for call in message.get("tool_calls", [])
            }
            tool_ids = {
                message["tool_call_id"]
                for message in body["messages"]
                if message["role"] == "tool"
            }
            assert len(query_ids) == 1 and query_ids == tool_ids
            assert all(
                (
                    call["function"]["name"] == "LlmInvestigationDecision"
                    for message in body["messages"]
                    for call in message.get("tool_calls", [])
                )
            )
            observations = json.loads(
                next(
                    (
                        message["content"]
                        for message in body["messages"]
                        if message["role"] == "tool"
                    )
                )
            )
            assert len(observations) == 2
            assert "T01,T02" in body["messages"][-1]["content"]
            result = {
                "status": "blocked",
                "assessment": "Both declarations lack the external implementation",
                "observation_refs": ["T01", "T02"],
                "result": {
                    "subtask_id": "step",
                    "outcome": terminal_outcome,
                    "limitations": ["External implementation unavailable"],
                },
            }
        return httpx.Response(
            200,
            json={
                "id": "offline",
                "object": "chat.completion",
                "created": 1,
                "model": "offline",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"step{len(requests)}",
                                    "type": "function",
                                    "function": {
                                        "name": offered,
                                        "arguments": json.dumps(result),
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    class Backend:
        def read_symbol(self, symbol_id, **kwargs):
            executed.append(symbol_id)
            return ToolResponse(
                True,
                f"symbol_id: {symbol_id}\nkind: METHOD\nfile: A.java\nlines: 1-1\n\nvoid method() {{}}",
            )

    roots = ("java:A#run()", "java:A#close()")
    client = CoordinatedDiscoveryToolClient(
        Backend(),
        DiscoveryToolCoordinator(),
        max_tool_calls=8,
        initial_symbol_ids=set(roots),
        symbol_catalog_ids=roots,
        subtask_id="step",
    )
    instruction = SubtaskInstruction(
        subtask_id="step",
        objective="Check state lifetime",
        initial_symbol_ids=roots,
        allowed_tools=("read_symbol",),
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        model = ChatOpenAI(
            model="offline",
            api_key="offline",
            base_url="http://offline.invalid/v1",
            http_client=http,
            max_retries=0,
        )
        engine = SubtaskReactEngine(client, max_rounds=3)
        engine._finalize_after_budget = lambda *args, **kwargs: pytest.fail(
            "no extra synthesis"
        )
        outcome = engine.run(
            model,
            task=SimpleNamespace(file="A.java", patch="+changed();"),
            symbol_context=None,
            instruction=instruction,
            structured_method="function_calling",
            max_retries=1,
        )
    assert outcome.status == terminal_outcome, outcome.reason
    assert len(requests) == (4 if malformed else 2)
    assert len(executed) == (0 if malformed else 2)
    assert engine._context_conclusion_used == malformed


@pytest.mark.parametrize(
    "raw,name",
    [
        ("java:pkg.A#run(int)", "run"),
        ("java:pkg.A#state", "state"),
        ("java:pkg.A#<init>A(int)", "A"),
        ("java:pkg.A$Nested", "Nested"),
    ],
)
def test_canonical_name_is_available_before_first_query(raw, name):
    from codeguard_agent.pipeline.controlled.investigation_decision import symbol_name

    assert symbol_name(raw) == name
    instruction = SubtaskInstruction(
        subtask_id="s", objective="Check behavior", initial_symbol_ids=(raw,)
    )
    engine = SubtaskReactEngine(SimpleNamespace(symbol_aliases={"S01": raw}))
    prompt = engine._build_user_prompt(
        SimpleNamespace(file="A.java", patch="+changed();"), None, instruction
    )
    navigation = json.loads(
        prompt.split("<initial_symbols>")[1].split("</initial_symbols>")[0]
    )
    assert navigation == [{"symbol_id": raw, "name": name}]


@pytest.mark.parametrize("canonical", [False, True])
def test_relation_result_unlocks_cross_file_source_and_natural_candidate_submission(
    canonical,
):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    executed = []

    class Backend:
        def query_relations(self, subject_symbol_id, relation, **kwargs):
            executed.append(("query_relations", subject_symbol_id, relation))
            assert "fact_question" not in kwargs
            return ToolResponse(
                True,
                json.dumps(
                    {
                        "schema_version": 2,
                        "outcome": "found",
                        "coverage": "complete",
                        "source_scope": "MAIN",
                        "subject_symbol_id": subject_symbol_id,
                        "symbols": [
                            {
                                "id": raw,
                                "kind": "METHOD",
                                "file": file,
                                "source_set": "MAIN",
                                "signature": signature,
                                "startLine": 1,
                                "endLine": 1,
                            }
                            for raw, file, signature in (
                                (subject_symbol_id, "A.java", "String run()"),
                                ("java:B#consume()", "B.java", "void consume()"),
                            )
                        ],
                        "relationships": [
                            {
                                "sourceId": "java:B#consume()",
                                "targetId": subject_symbol_id,
                                "kind": "CALLS",
                                "file": "B.java",
                                "line": 1,
                                "source_set": "MAIN",
                                "resolution": "RESOLVED",
                            }
                        ],
                        "unresolved_relationships": [],
                        "unresolved_count": 0,
                        "limitations": [],
                        "next_cursor": None,
                    }
                ),
            )

        def read_symbol(self, symbol_id, **kwargs):
            executed.append(("read_symbol", symbol_id))
            return ToolResponse(
                True,
                f"symbol_id: {symbol_id}\nkind: METHOD\nfile: B.java\nlines: 1-1\n\nvoid consume() {{ new A().run().length(); }} // consumer-source",
            )

    class Model(BaseChatModel):
        calls: int = 0

        @property
        def _llm_type(self):
            return "offline-cross-file-decision"

        def bind_tools(self, tools, **kwargs):
            assert tools[0]["function"]["name"] == "LlmInvestigationDecision"
            return self

        def _generate(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                result = {
                    "status": "need_evidence",
                    "assessment": "Need a consumer of the changed return value",
                    "queries": [
                        {
                            "tool": "query_relations",
                            "expected_symbol_name": "run",
                            "arguments": {
                                "subject_symbol_id": "java:A#run()"
                                if canonical
                                else "S01",
                                "relation": "callers",
                                "fact_question": "Who consumes run's return?",
                            },
                        }
                    ],
                }
            elif self.calls == 2:
                target = "java:B#consume()" if canonical else "R01"
                if canonical:
                    assert client.symbol_aliases == {}
                else:
                    assert client.symbol_aliases["R01"] == "java:B#consume()"
                assert any(
                    (
                        target in str(message.content)
                        for message in messages
                        if isinstance(message, ToolMessage)
                    )
                )
                result = {
                    "status": "need_evidence",
                    "assessment": "T01 locates a consumer; its null handling is unknown",
                    "observation_refs": ["T01"],
                    "queries": [
                        {
                            "tool": "read_symbol",
                            "expected_symbol_name": "consume",
                            "arguments": {
                                "symbol_id": target,
                                "fact_question": "Does consume guard the returned value?",
                            },
                        }
                    ],
                }
            else:
                assert self.calls == 3
                assert any(
                    ("consumer-source" in str(message.content) for message in messages)
                )
                result = {
                    "status": "ready",
                    "assessment": "T02 dereferences the changed return without a guard",
                    "observation_refs": ["T02"],
                    "result": {
                        "subtask_id": "cross",
                        "outcome": "findings",
                        "findings": [
                            {
                                "claim": "Existing consumer dereferences null",
                                "mechanism": "run now returns null to consume",
                                "location_file": "A.java",
                                "location_line": 1,
                                "observations": [
                                    {"observation_id": "T01", "role": "relation"},
                                    {"observation_id": "T02", "role": "impact"},
                                ],
                            }
                        ],
                    },
                }
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "LlmInvestigationDecision",
                                    "id": f"step{self.calls}",
                                    "args": result,
                                }
                            ],
                        )
                    )
                ]
            )

    client = CoordinatedDiscoveryToolClient(
        Backend(),
        DiscoveryToolCoordinator(),
        max_tool_calls=8,
        initial_symbol_ids={"java:A#run()"},
        subtask_id="cross",
        canonical_symbol_ids=canonical,
        lossless_payload=True,
    )
    instruction = SubtaskInstruction(
        subtask_id="cross",
        objective="Check return contract",
        initial_symbol_ids=("java:A#run()",),
        allowed_tools=("read_symbol", "query_relations"),
    )
    outcome = SubtaskReactEngine(client, max_rounds=6).run(
        Model(),
        task=SimpleNamespace(file="A.java", patch="+return null;"),
        symbol_context=None,
        instruction=instruction,
        structured_method="function_calling",
        max_retries=1,
    )
    assert outcome.status == "complete" and outcome.reason == "", outcome.reason
    assert outcome.result and len(outcome.result.findings) == 1
    assert executed == [
        ("query_relations", "java:A#run()", "callers"),
        ("read_symbol", "java:B#consume()"),
    ]
    assert len(client.trace_records) == 2


@pytest.mark.parametrize("warm_cache", [False, True])
def test_repeat_feedback_preserves_navigation_and_original_evidence_identity(
    warm_cache,
):

    class Backend:
        calls = 0

        def read_symbol(self, symbol_id, **kwargs):
            self.calls += 1
            return ToolResponse(
                True,
                f"symbol_id: {symbol_id}\nkind: METHOD\nowner_id: java:A\nlines: 1-1\n\nvoid run() {{}}",
            )

    backend = Backend()
    coordinator = DiscoveryToolCoordinator()
    if warm_cache:
        other = CoordinatedDiscoveryToolClient(
            backend,
            coordinator,
            initial_symbol_ids={"java:A#run()"},
            canonical_symbol_ids=True,
            subtask_id="other",
        )
        other.read_symbol("java:A#run()")
    client = CoordinatedDiscoveryToolClient(
        backend,
        coordinator,
        initial_symbol_ids={"java:A#run()"},
        canonical_symbol_ids=True,
        lossless_payload=True,
        subtask_id="repeat",
    )
    client.read_symbol("java:A#run()")
    response = client.read_symbol("java:A#run()")
    assert backend.calls == 1
    assert "owner_id: java:A" in response.result and "T01" in response.result
    assert "void run()" not in response.result
    from codeguard_agent.pipeline.execution.discovery import REPEATED_TOOL_RESULT

    assert client.trace_records[-1].output == REPEATED_TOOL_RESULT
    assert len(client.observation_aliases) == 1
    assert client.read_symbol("java:A").success
    assert not client.read_symbol("java:Unrelated").success
    assert backend.calls == 2


def test_finish_uses_result_outcome_without_redundant_decision_status():
    from codeguard_agent.pipeline.controlled.investigation_decision import (
        decision_error,
        decision_schema,
    )

    schema = decision_schema([])
    decision = schema.model_validate(
        {
            "status": "ready",
            "assessment": "Missing external contract",
            "result": {"subtask_id": "s", "outcome": "inconclusive"},
        }
    )
    assert decision_error(decision, aliases={}, known={"T01"}, pending={"T01"}) == ""
    assert "status" not in schema.model_json_schema()["properties"]
    assert decision.result.outcome == "inconclusive"
