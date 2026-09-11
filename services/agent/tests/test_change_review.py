"""Default change coverage and evidence handoff, no paid model calls."""

import json
import pytest
from types import SimpleNamespace
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from codeguard_agent.models.tasks import ReviewTask, TaskSymbolContext
from codeguard_agent.models.tasks.symbols import ResolvedSymbol
from codeguard_agent.pipeline.controlled.change_review import (
    build_change_review_node,
    change_groups,
    review_group_id,
)
from codeguard_agent.pipeline.orchestration.graph import (
    _allocate_subtask_budgets,
    _investigation_candidate,
    build_review_graph,
)
from codeguard_agent.tools.tool_client import ToolResponse


def fixture():
    task = ReviewTask(
        id="t",
        file="A.java",
        patch="@@ -2 +2 @@\n-return 1;\n+return 0;",
        changed_lines=[2],
    )
    symbol = ResolvedSymbol(
        file="A.java",
        symbol_id="java:A#run()",
        kind="METHOD",
        start_line=1,
        end_line=3,
        source_set="MAIN",
    )
    return (task, TaskSymbolContext(task_id="t", symbols=(symbol,), status="resolved"))


def test_all_changed_declarations_including_deletion_are_covered_once():
    task, context = fixture()
    symbols = tuple(
        (
            context.symbols[0].model_copy(
                update={
                    "symbol_id": f"java:A#m{i}()",
                    "start_line": i * 10 + 1,
                    "end_line": i * 10 + 3,
                }
            )
            for i in range(9)
        )
    )
    task = task.model_copy(
        update={"changed_lines": [i * 10 + 2 for i in range(8)], "deletion_anchors": []}
    )
    context = context.model_copy(update={"symbols": symbols})
    groups = change_groups(task, context)
    assert [len(g) for g in groups] == [1] * 8
    assert all(len(g) == 1 for g in groups)
    assert len({s.symbol_id for g in groups for s in g}) == 8
    task = task.model_copy(
        update={
            "changed_lines": [],
            "deletion_anchors": [SimpleNamespace(anchor_line=82)],
        }
    )
    assert change_groups(task, context) == [(symbols[8],)]


def test_fields_share_one_group_while_other_declarations_stay_independent():
    task, context = fixture()
    symbols = tuple(
        context.symbols[0].model_copy(
            update={
                "symbol_id": symbol_id,
                "kind": kind,
                "start_line": start_line,
                "end_line": start_line,
            }
        )
        for symbol_id, kind, start_line in (
            ("java:A#first()", "METHOD", 10),
            ("java:A#fieldA", "FIELD", 20),
            ("java:A#second()", "METHOD", 30),
            ("java:A#fieldB", "FIELD", 40),
            ("java:A", "TYPE", 50),
        )
    )
    task = task.model_copy(update={"changed_lines": [10, 20, 30, 40, 50]})
    groups = change_groups(task, context.model_copy(update={"symbols": symbols}))

    assert [[s.symbol_id for s in group] for group in groups] == [
        ["java:A#first()"],
        ["java:A#fieldA", "java:A#fieldB"],
        ["java:A#second()"],
        ["java:A"],
    ]


def test_default_graph_does_not_run_planning_or_triage_models():
    graph = build_review_graph().get_graph()
    assert "plan" not in graph.nodes
    assert "summary" not in graph.nodes
    assert "controlled_review" in graph.nodes


def test_full_evaluation_profile_matches_default_discovery():
    from evals.profiles import load_profiles

    profiles = load_profiles()
    for name in ("eval-codeguard-full", "eval-controlled-codegraph"):
        assert profiles[name].discovery_mode == "controlled"
        assert profiles[name].tools == ["read_symbol", "query_relations"]
        assert profiles[name].orchestration == "change-review"


def test_blank_added_separator_does_not_expand_review_to_whole_type():
    task, context = fixture()
    owner = context.symbols[0].model_copy(
        update={"symbol_id": "java:A", "kind": "TYPE", "start_line": 1, "end_line": 100}
    )
    task = task.model_copy(
        update={"patch": "@@ -2,1 +2,2 @@\n+return 0;\n+\n", "changed_lines": [2, 3]}
    )
    method = context.symbols[0].model_copy(update={"end_line": 2})
    assert change_groups(
        task, context.model_copy(update={"symbols": (owner, method)})
    ) == [(method,)]


class Model(BaseChatModel):
    seen: list = []
    patch_only: bool = False

    @property
    def _llm_type(self):
        return "offline-change-review"

    def bind_tools(self, tools, **kwargs):
        assert (
            "observation_refs" not in tools[0]["function"]["parameters"]["properties"]
        )
        return self

    def _generate(self, messages, **kwargs):
        text = "\n".join((str(m.content) for m in messages))
        self.seen.append(text)
        assert "return 0" in text and "T01" in text
        assert "initial_symbols" in text
        assert "mechanism_note2" not in text
        result = dict(
            subtask_id="change-1",
            outcome="findings",
            findings=[
                dict(
                    claim="The changed result violates the declared contract",
                    mechanism="Return value changed",
                    location_file="A.java",
                    location_line=2,
                    observations=[]
                    if self.patch_only
                    else [dict(observation_id="T01", role="mechanism")],
                )
            ],
        )
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            dict(
                                name="LlmInvestigationDecision",
                                id="finish",
                                args=dict(
                                    assessment="Source is sufficient",
                                    queries=[],
                                    result=result,
                                    observation_refs=[{"observation_id": "T01"}],
                                ),
                            )
                        ],
                    )
                )
            ]
        )


class Client:
    def read_symbol(self, symbol_id, **kwargs):
        return ToolResponse(
            success=True,
            result=f"symbol_id: {symbol_id}\nkind: METHOD\nfile: A.java\nlines: 1-3\ntruncated: false\n\n1: int run() {{\n2: return 0;\n3: }}",
        )

    def query_relations(self, subject_symbol_id, relation, **kwargs):
        return ToolResponse(
            True,
            json.dumps(
                dict(
                    schema_version=2,
                    outcome="found",
                    coverage="complete",
                    source_scope="MAIN",
                    subject_symbol_id=subject_symbol_id,
                    symbols=[],
                    relationships=[],
                    unresolved_relationships=[],
                    unresolved_count=0,
                    limitations=[],
                    next_cursor=None,
                )
            ),
        )


class GraphClient(Client):
    def __init__(self):
        self.queries = []

    def query_relations(self, subject_symbol_id, relation, **kwargs):
        self.queries.append((subject_symbol_id, relation, kwargs))
        caller = "java:B#consume()"
        return ToolResponse(
            True,
            json.dumps(
                dict(
                    schema_version=2,
                    outcome="found",
                    coverage="partial",
                    source_scope="MAIN",
                    subject_symbol_id=subject_symbol_id,
                    symbols=[
                        dict(id=subject_symbol_id, kind="METHOD", source_set="MAIN"),
                        dict(id=caller, kind="METHOD", source_set="MAIN"),
                    ],
                    relationships=[
                        dict(
                            sourceId=caller,
                            targetId=subject_symbol_id,
                            kind="CALLS",
                            file="B.java",
                            line=7,
                            source_set="MAIN",
                            resolution="RESOLVED",
                        )
                    ],
                    unresolved_relationships=[],
                    unresolved_count=0,
                    limitations=["page_limit"],
                    next_cursor=6,
                )
            ),
        )


class GraphModel(Model):
    def _generate(self, messages, **kwargs):
        text = "\n".join((str(m.content) for m in messages))
        assert "java:B#consume()" in text and "T02" in text
        assert "partial" in text and "next_cursor" in text
        response = super()._generate(messages, **kwargs)
        args = response.generations[0].message.tool_calls[0]["args"]
        args["result"]["findings"][0]["observations"] = [
            dict(observation_id="T02", role="relation")
        ]
        return response


def test_first_decision_sees_one_hop_evidence_and_can_cite_it_without_querying():
    task, context = fixture()
    model, client = (GraphModel(), GraphClient())
    node = build_change_review_node(
        model,
        client,
        candidate_factory=_investigation_candidate,
        scope_factory=lambda s: SimpleNamespace(scoped_patch=lambda p: p),
        allocate_budgets=_allocate_subtask_budgets,
    )
    result = node(dict(review_tasks=[task], task_symbol_contexts={"t": context}))
    assert len(model.seen) == 1
    assert [q[1] for q in client.queries] == ["callers", "callees"]
    assert all(
        (
            q[2]["depth"] == 1 and q[2]["limit"] == 6 and q[2]["include_context"]
            for q in client.queries
        )
    )
    assert len(result["tool_trace_records"]) == 3
    candidate = result["candidate_issues"][0]
    assert not candidate.evidence_ref_errors
    assert len(candidate.evidence_refs) == 2
    assert any(
        (
            "java:B#consume()" in str(a.payload)
            for a in result["evidence_artifacts"].values()
        )
    )


@pytest.mark.parametrize("budget", [8, 10])
def test_prefetch_preserves_exploration_budget_and_reports_unqueried_frontiers(budget):
    from codeguard_agent.pipeline.controlled.change_review import prepare_change_context
    from codeguard_agent.pipeline.execution.discovery import (
        CoordinatedDiscoveryToolClient,
        DiscoveryToolCoordinator,
    )

    _, context = fixture()
    group = tuple(
        (
            context.symbols[0].model_copy(update={"symbol_id": f"java:A#m{i}()"})
            for i in range(4)
        )
    )
    client = CoordinatedDiscoveryToolClient(
        GraphClient(),
        DiscoveryToolCoordinator(),
        canonical_symbol_ids=True,
        lossless_payload=True,
        max_tool_calls=budget,
        initial_symbol_ids={s.symbol_id for s in group},
        subtask_id="prefetch",
    )
    instruction = SimpleNamespace(
        max_tool_calls=budget,
        allowed_tools=("read_symbol", "query_relations"),
        allowed_relations=("callers", "callees"),
    )
    with client.context_preparation():
        text = prepare_change_context(client, group, instruction)
    assert client.tool_calls == 6 and (not client.budget_exhausted)
    manifest = json.loads(
        text.split("<preparation_scope>")[1].split("</preparation_scope>")[0]
    )
    assert len(manifest["unqueried_relations"]) == 6
    assert manifest["reserved_tool_attempts"] == budget - 6
    assert client.read_symbol("java:B#consume()").success
    assert client.tool_calls == 7


def test_empty_prefetch_does_not_close_model_exploration_but_closes_empty_frontier():
    from codeguard_agent.pipeline.execution.discovery import (
        CoordinatedDiscoveryToolClient,
        DiscoveryToolCoordinator,
    )

    class EmptyClient(Client):
        def query_relations(self, subject_symbol_id, relation, **kwargs):
            response = super().query_relations(subject_symbol_id, relation, **kwargs)
            return ToolResponse(True, response.result.replace('"found"', '"not_found"'))

    roots = {f"java:A#m{i}()" for i in range(4)}
    client = CoordinatedDiscoveryToolClient(
        EmptyClient(),
        DiscoveryToolCoordinator(),
        canonical_symbol_ids=True,
        lossless_payload=True,
        max_tool_calls=8,
        initial_symbol_ids=roots,
        subtask_id="prefetch",
    )
    with client.context_preparation():
        for root in sorted(roots):
            assert client.query_relations(root, "callers", limit=6).success
    assert not client.no_progress_exhausted and client.tool_calls == 4
    assert client.read_symbol(sorted(roots)[0]).success
    repeat = client.query_relations(sorted(roots)[0], "callers", limit=6)
    assert not repeat.success and repeat.error == "frontier_exhausted"


def test_preparation_deadline_prevents_first_model_call(monkeypatch):
    from codeguard_agent.models.tasks import SubtaskInstruction
    from codeguard_agent.pipeline.controlled import subtask_react

    task, context = fixture()
    now = [0.0]
    monkeypatch.setattr(subtask_react, "monotonic", lambda: now[0])

    def prepare():
        now[0] = 121.0
        return "late context"

    model = Model()
    instruction = SubtaskInstruction(subtask_id="t", objective="review")
    outcome = subtask_react.SubtaskReactEngine(
        SimpleNamespace(), prepare_context=prepare
    ).run(
        model,
        task=task,
        symbol_context=context,
        instruction=instruction,
        structured_method="function_calling",
        max_retries=0,
    )
    assert outcome.reason == "subtask_timeout" and (not model.seen)


def test_prefetched_source_is_visible_referencable_and_counted():
    task, context = fixture()
    model = Model()
    node = build_change_review_node(
        model,
        Client(),
        candidate_factory=_investigation_candidate,
        scope_factory=lambda s: SimpleNamespace(scoped_patch=lambda p: p),
        allocate_budgets=_allocate_subtask_budgets,
    )
    result = node(
        dict(
            review_tasks=[task],
            task_symbol_contexts={"t": context},
            evidence_revision="rev",
            enabled_tools=["read_symbol"],
        )
    )
    assert result["controlled_subtask_outcomes"] == {
        f"t:{review_group_id('t', 0)}": "findings"
    }
    assert len(model.seen) == 1
    assert len(result["tool_trace_records"]) == 1
    candidate = result["candidate_issues"][0]
    assert len(candidate.evidence_refs) == 2
    assert not candidate.evidence_ref_errors
    assert (
        result["controlled_candidate_contexts"][candidate.id]["mechanism"]
        == "Return value changed"
    )


def test_patch_only_candidate_does_not_require_unrelated_tool_reference():
    task, context = fixture()
    node = build_change_review_node(
        Model(patch_only=True),
        Client(),
        candidate_factory=_investigation_candidate,
        scope_factory=lambda s: SimpleNamespace(scoped_patch=lambda p: p),
        allocate_budgets=_allocate_subtask_budgets,
    )
    result = node(dict(review_tasks=[task], task_symbol_contexts={"t": context}))
    assert len(result["candidate_issues"][0].evidence_refs) == 1


def test_provider_and_internal_findings_capacity_agree():
    from codeguard_agent.models.tasks import InvestigationResult
    from codeguard_agent.pipeline.controlled.llm_contracts import LlmInvestigationResult

    for model in (InvestigationResult, LlmInvestigationResult):
        assert model.model_json_schema()["properties"]["findings"]["maxItems"] == 8
    assert "mechanism_note2" not in json.dumps(
        LlmInvestigationResult.model_json_schema()
    )
    assert review_group_id("file-a", 0) != review_group_id("file-b", 0)


def test_presentation_truncation_preserves_original_patch_digest_and_marks_gap():
    task, context = fixture()
    full_task = task.model_copy(update={"patch": task.patch + "\n+unseen();"})
    node = build_change_review_node(
        Model(patch_only=True),
        Client(),
        candidate_factory=_investigation_candidate,
        scope_factory=lambda s: SimpleNamespace(scoped_patch=lambda p: task.patch),
        allocate_budgets=_allocate_subtask_budgets,
    )
    result = node(dict(review_tasks=[full_task], task_symbol_contexts={"t": context}))
    assert list(result["controlled_subtask_outcomes"].values()) == ["inconclusive"]
    assert list(result["controlled_subtask_reasons"].values()) == [
        "task_patch_truncated"
    ]
    patch = next(
        (
            a
            for a in result["evidence_artifacts"].values()
            if a.source_kind.value == "task_patch"
        )
    )
    assert patch.payload == full_task.patch


def test_final_evidence_ids_remain_strict_without_observation_receipts():
    from codeguard_agent.models.tasks import InvestigationResult
    from codeguard_agent.pipeline.controlled.subtask_react import SubtaskReactEngine

    engine = SubtaskReactEngine(SimpleNamespace(observation_aliases={"T01": "call-1"}))
    result = InvestigationResult.model_validate(
        dict(
            subtask_id="x",
            outcome="findings",
            findings=[
                dict(
                    claim="claim",
                    mechanism="mechanism",
                    location_file="A.java",
                    observations=[
                        dict(observation_id="T01", role="mechanism"),
                        dict(observation_id="T99", role="impact"),
                    ],
                )
            ],
        )
    )
    assert engine._terminal_error(result) == "unknown_finding_observations:T99"


def test_candidate_location_corrects_unique_added_snippet_without_model_call():
    from codeguard_agent.models.tasks import InvestigationFinding
    from codeguard_agent.pipeline.evidence.ledger import EvidenceCatalogBuilder

    task, context = fixture()
    finding = InvestigationFinding(
        claim="claim",
        mechanism="mechanism",
        location_file="A.java",
        location_line=80,
        location_snippet="return 0;",
    )
    catalog = EvidenceCatalogBuilder().build_initial(
        task=task, symbol_context=context, reviewer="behavior", revision="r"
    )
    candidate = _investigation_candidate(
        finding,
        task=task,
        reviewer="behavior",
        catalog=catalog,
        alias_by_call_id={},
        candidate_index=1,
        allow_patch_only=True,
    )
    assert candidate.line == 2


def test_pure_deletion_anchor_reaches_reviewer_and_survives_location_binding():
    from codeguard_agent.models.tasks import (
        DeletionAnchor,
        InvestigationFinding,
        SubtaskInstruction,
    )
    from codeguard_agent.pipeline.controlled.subtask_react import SubtaskReactEngine
    from codeguard_agent.pipeline.evidence.ledger import EvidenceCatalogBuilder

    task = ReviewTask(
        id="d",
        file="A.java",
        patch="@@ -2,2 +2,1 @@\n- if (guard) return;\n run();",
        deletion_anchors=[
            DeletionAnchor(
                anchor_line=2,
                anchor_kind="next_surviving",
                deleted_snippet=" if (guard) return;",
            )
        ],
    )
    instruction = SubtaskInstruction(subtask_id="d", objective="review")
    text = SubtaskReactEngine(SimpleNamespace())._build_user_prompt(
        task, None, instruction
    )
    assert (
        "<deletion_anchors>" in text
        and '"anchor_line":2' in text
        and ("next_surviving" in text)
    )
    catalog = EvidenceCatalogBuilder().build_initial(
        task=task, symbol_context=None, reviewer="behavior", revision="r"
    )
    finding = InvestigationFinding(
        claim="guard removed",
        mechanism="early exit removed",
        location_file="A.java",
        location_line=2,
    )
    candidate = _investigation_candidate(
        finding,
        task=task,
        reviewer="behavior",
        catalog=catalog,
        alias_by_call_id={},
        candidate_index=1,
        allow_patch_only=True,
    )
    assert candidate.line == 2
