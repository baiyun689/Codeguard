"""EvidenceAgent strategy execution and safe relation tests."""

from __future__ import annotations

import importlib
import json

from codeguard_agent.models.council import (
    CandidateIssue,
    ContextFact,
    EvidenceFinding,
    EvidenceNote,
    EvidenceRequest,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import ReviewTask, RiskProfile, RiskTag, TaskContextBundle
from codeguard_agent.pipeline.evidence_planner import CandidateDossier
from codeguard_agent.pipeline.evidence_rules import STRATEGIES_BY_ID
from codeguard_agent.tools.tool_client import ToolResponse


def _dossier(
    candidate_id: str = "candidate-1",
    *,
    task: ReviewTask | None = None,
    context: TaskContextBundle | None = None,
) -> CandidateDossier:
    task = task or ReviewTask(
        id="src/Service.java#h0",
        file="src/Service.java",
        hunk_header="@@ -9,3 +9,3 @@",
        patch="+public void update() { save(); }",
        changed_lines=[10],
    )
    candidate = CandidateIssue(
        id=candidate_id,
        task_id=task.id,
        source_agent="threat_model",
        file=task.file,
        line=10,
        type="authorization",
        severity_proposal=Severity.WARNING,
        claim="update lacks authorization",
        confidence=0.8,
    )
    return CandidateDossier(
        candidate=candidate,
        task=task,
        risk_profile=RiskProfile(
            task_id=task.id,
            tag_scores={RiskTag.AUTHORIZATION: 3},
        ),
        context_bundle=context,
        requests=(),
        notes=(),
        latest_verdict=None,
    )


def _request(dossier: CandidateDossier, strategy_id: str = "authorization.counter") -> EvidenceRequest:
    strategy = STRATEGIES_BY_ID[strategy_id]
    calls = strategy.build_tool_calls(dossier)
    return EvidenceRequest(
        candidate_id=dossier.candidate.id,
        strategy_id=strategy.id,
        purpose=strategy.purpose,
        target=dossier.task.file,
        question=strategy.question_template,
        preferred_tools=list(dict.fromkeys(call.tool_name for call in calls)),
    )


class _ToolClient:
    def __init__(self, file_content: str = "class Service {}") -> None:
        self.file_content = file_content
        self.calls: list[tuple[str, object]] = []

    def get_file_content(self, file_path: str) -> ToolResponse:
        self.calls.append(("get_file_content", file_path))
        return ToolResponse(success=True, result=self.file_content)

    def find_sensitive_apis(self) -> ToolResponse:
        self.calls.append(("find_sensitive_apis", {}))
        return ToolResponse(success=True, result="")


def _collect(dossiers, requests, *, client=None, enabled_tools=None):
    evidence_agent = importlib.import_module("codeguard_agent.pipeline.evidence_agent")
    return evidence_agent.collect_evidence(
        dossiers,
        requests,
        tool_client=client,
        analyst_llm=None,
        structured_method="function_calling",
        enabled_tools=enabled_tools,
    )


def _collect_with_llm(dossiers, requests, llm, *, client=None):
    module = importlib.import_module("codeguard_agent.pipeline.evidence_agent")
    return module.collect_evidence(
        dossiers,
        requests,
        tool_client=client,
        analyst_llm=llm,
        structured_method="function_calling",
        enabled_tools=None,
    )


def test_request_strategy_mismatch_does_not_call_tools_and_gets_one_note():
    dossier = _dossier()
    valid = _request(dossier)
    invalid = valid.model_copy(update={"question": "invented question"})
    client = _ToolClient()

    batch = _collect([dossier], [invalid], client=client)

    assert client.calls == []
    assert len(batch.notes) == 1
    assert batch.notes[0].request_id == invalid.id
    assert len(batch.notes[0].findings) == 1
    assert batch.notes[0].findings[0].relation == "insufficient"
    assert batch.notes[0].findings[0].limitation == "request_strategy_mismatch"
    assert batch.gathered_context == []


def test_no_tool_client_is_insufficient_and_never_defaults_to_supports():
    dossier = _dossier()
    request = _request(dossier)

    batch = _collect([dossier], [request], client=None)

    assert len(batch.notes) == 1
    assert all(finding.relation == "insufficient" for finding in batch.notes[0].findings)
    assert any(finding.limitation == "no_tool_client" for finding in batch.notes[0].findings)


def test_same_tool_call_is_cached_but_each_request_gets_its_own_note():
    task = ReviewTask(
        id="src/Service.java#h0",
        file="src/Service.java",
        hunk_header="@@ -9,3 +9,3 @@",
        patch="+public void update() { save(); }",
        changed_lines=[10],
    )
    first = _dossier("candidate-1", task=task)
    second = _dossier("candidate-2", task=task)
    requests = [_request(first), _request(second)]
    client = _ToolClient("class Service { void update() { save(); } }")

    batch = _collect([first, second], requests, client=client)

    assert len(batch.notes) == 2
    assert [note.request_id for note in batch.notes] == [request.id for request in requests]
    assert client.calls.count(("get_file_content", "src/Service.java")) == 1
    assert client.calls.count(("find_sensitive_apis", {})) == 1
    assert len(batch.gathered_context) == 2
    assert sum(event == "evidence_tool_called" for event, _ in batch.trace) == 2
    assert sum(event == "evidence_tool_reused" for event, _ in batch.trace) == 2
    tool_ids = [
        {finding.evidence_id for finding in note.findings if finding.source.startswith("tool:")}
        for note in batch.notes
    ]
    assert tool_ids[0] == tool_ids[1]


def test_context_fact_reuse_skips_matching_sensitive_api_tool():
    task = ReviewTask(
        id="src/Service.java#h0",
        file="src/Service.java",
        hunk_header="@@ -9,3 +9,3 @@",
        patch="+public void update() { save(); }",
        changed_lines=[10],
    )
    context = TaskContextBundle(
        task_id=task.id,
        facts=[
            ContextFact(
                source="tool:find_sensitive_apis",
                kind="sensitive_api",
                content="| sink | call | src/Service.java:10 |",
            )
        ],
    )
    dossier = _dossier(task=task, context=context)
    client = _ToolClient()

    batch = _collect([dossier], [_request(dossier)], client=client)

    assert ("find_sensitive_apis", {}) not in client.calls
    assert any(
        finding.source == "context:sensitive_api"
        for finding in batch.notes[0].findings
    )


def test_all_request_strategy_fields_are_validated_before_tools():
    dossier = _dossier()
    request = _request(dossier)
    invalid_requests = [
        request.model_copy(update={"strategy_id": "missing.strategy"}),
        request.model_copy(update={"purpose": "support"}),
        request.model_copy(update={"target": "src/Other.java"}),
        request.model_copy(update={"question": "wrong"}),
        request.model_copy(update={"preferred_tools": ["get_file_content"]}),
    ]
    client = _ToolClient()

    batch = _collect([dossier], invalid_requests, client=client)

    assert client.calls == []
    assert len(batch.notes) == len(invalid_requests)
    assert all(
        note.findings[0].limitation == "request_strategy_mismatch"
        for note in batch.notes
    )


def test_disabled_tools_are_not_called_and_are_recorded_as_insufficient():
    dossier = _dossier()
    client = _ToolClient()

    batch = _collect(
        [dossier],
        [_request(dossier)],
        client=client,
        enabled_tools=[],
    )

    assert client.calls == []
    assert any(
        finding.limitation == "tool_disabled"
        for finding in batch.notes[0].findings
    )


def test_truncated_context_can_only_be_insufficient():
    task = _dossier().task
    context = TaskContextBundle(
        task_id=task.id,
        truncated=True,
        facts=[
            ContextFact(
                source="tool:find_sensitive_apis",
                kind="sensitive_api",
                content="| sink | call | src/Service.java:10 |",
            )
        ],
    )
    dossier = _dossier(task=task, context=context)

    batch = _collect([dossier], [_request(dossier)], client=None)

    contextual = next(
        finding
        for finding in batch.notes[0].findings
        if finding.source == "context:sensitive_api"
    )
    assert contextual.relation == "insufficient"
    assert contextual.strength == "contextual"
    assert contextual.limitation == "context_truncated"


class _StructuredLLM:
    def __init__(self, result=None, *, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.messages = []

    def with_structured_output(self, schema, method):
        self.schema = schema
        self.method = method
        return self

    def invoke(self, messages):
        self.messages.append(messages)
        if self.error is not None:
            raise self.error
        return self.result


def test_analyst_none_and_exception_are_insufficient():
    dossier = _dossier()
    request = _request(dossier)

    none_batch = _collect_with_llm([dossier], [request], _StructuredLLM(None))
    error_batch = _collect_with_llm(
        [dossier],
        [request],
        _StructuredLLM(error=ValueError("broken")),
    )

    assert all(f.relation == "insufficient" for f in none_batch.notes[0].findings)
    assert all(f.relation == "insufficient" for f in error_batch.notes[0].findings)


def test_analyst_prompt_contains_candidate_strategy_task_risk_context_and_fact():
    task = _dossier().task
    context = TaskContextBundle(
        task_id=task.id,
        facts=[
            ContextFact(
                source="tool:get_diff_ast",
                kind="ast_structure",
                content="AST for: src/Service.java\n  class: Service\n    void update() [L9-L12]",
            )
        ],
    )
    dossier = _dossier(task=task, context=context)
    llm = _StructuredLLM(
        {
            "relation": "insufficient",
            "strength": "contextual",
            "observation": "",
            "limitation": "method body unavailable",
        }
    )

    _collect_with_llm([dossier], [_request(dossier)], llm)

    rendered = "\n".join(content for call in llm.messages for _, content in call)
    assert dossier.candidate.claim in rendered
    assert dossier.candidate.type in rendered
    assert dossier.candidate.severity_proposal.value in rendered
    assert "counter" in rendered
    assert STRATEGIES_BY_ID["authorization.counter"].question_template in rendered
    assert task.patch in rendered
    assert "AUTHORIZATION" in rendered
    assert "ast_structure" in rendered
    assert "source" in rendered and "limitation" in rendered


def _authorization_scope_dossier(file_content: str, ast_method: str) -> tuple[CandidateDossier, _ToolClient]:
    task = ReviewTask(
        id="src/Service.java#h0",
        file="src/Service.java",
        hunk_header="@@ -9,3 +9,3 @@",
        patch="+public void update() { save(); }",
        changed_lines=[10],
    )
    context = TaskContextBundle(
        task_id=task.id,
        facts=[
            ContextFact(
                source="tool:get_diff_ast",
                kind="ast_structure",
                content=(
                    "AST for: src/Service.java\n"
                    "  class: Service\n"
                    f"    {ast_method} [L9-L12]"
                ),
            )
        ],
    )
    return _dossier(task=task, context=context), _ToolClient(file_content)


def test_current_method_authorization_annotation_is_direct_counter_evidence():
    dossier, client = _authorization_scope_dossier(
        "class Service {\n\n\n\n\n\n\n\n@PreAuthorize(\"hasRole('ADMIN')\")\npublic void update() { save(); }\n}",
        "@PreAuthorize public void update()",
    )

    batch = _collect([dossier], [_request(dossier)], client=client)

    assert any(
        finding.relation == "contradicts" and finding.strength == "direct"
        for finding in batch.notes[0].findings
    )


def test_current_class_authorization_annotation_is_direct_counter_evidence():
    dossier, client = _authorization_scope_dossier(
        "@PreAuthorize(\"hasRole('ADMIN')\")\npublic class Service {\n\n\n\n\n\n\npublic void update() { save(); }\n}",
        "public void update()",
    )

    batch = _collect([dossier], [_request(dossier)], client=client)

    assert any(
        finding.relation == "contradicts" and finding.strength == "direct"
        for finding in batch.notes[0].findings
    )


def test_other_method_and_comment_annotation_text_are_never_direct():
    dossier, client = _authorization_scope_dossier(
        "public class Service {\n"
        "  @PreAuthorize(\"hasRole('ADMIN')\") void admin() {}\n"
        "  String marker = \"@PreAuthorize\";\n"
        "  // @PreAuthorize\n\n\n\n\n"
        "  public void update() { save(); }\n"
        "}",
        "public void update()",
    )

    batch = _collect([dossier], [_request(dossier)], client=client)

    assert not any(finding.strength == "direct" for finding in batch.notes[0].findings)


def test_severity_request_reuses_prior_observation_with_same_evidence_id():
    dossier = _dossier()
    prior = EvidenceNote(
        request_id="prior-request",
        candidate_id=dossier.candidate.id,
        findings=[
            EvidenceFinding(
                evidence_id="shared-evidence",
                source="tool:find_callers",
                observation="two public callers",
                relation="supports",
                strength="contextual",
            )
        ],
    )
    dossier = CandidateDossier(
        candidate=dossier.candidate,
        task=dossier.task,
        risk_profile=dossier.risk_profile,
        context_bundle=dossier.context_bundle,
        requests=(),
        notes=(prior,),
        latest_verdict=None,
    )
    request = _request(dossier, "authorization.severity")

    batch = _collect([dossier], [request], client=None)

    reused = next(f for f in batch.notes[0].findings if f.evidence_id == "shared-evidence")
    assert reused.source == "prior:tool:find_callers"
    assert reused.observation == "two public callers"


def test_counter_request_reuses_prior_tool_finding_without_repeating_call():
    dossier = _dossier()
    prior = EvidenceNote(
        request_id="prior-request",
        candidate_id=dossier.candidate.id,
        findings=[
            EvidenceFinding(
                evidence_id="prior-file",
                source="tool:get_file_content",
                observation="class Service has no class annotation",
                relation="insufficient",
                strength="contextual",
                limitation="scope unresolved",
            )
        ],
    )
    dossier = CandidateDossier(
        candidate=dossier.candidate,
        task=dossier.task,
        risk_profile=dossier.risk_profile,
        context_bundle=dossier.context_bundle,
        requests=(),
        notes=(prior,),
        latest_verdict=None,
    )
    client = _ToolClient()

    batch = _collect([dossier], [_request(dossier)], client=client)

    assert ("get_file_content", dossier.task.file) not in client.calls
    reused = next(f for f in batch.notes[0].findings if f.evidence_id == "prior-file")
    assert reused.source == "prior:tool:get_file_content"


class _FailingTools(_ToolClient):
    def get_file_content(self, file_path: str) -> ToolResponse:
        self.calls.append(("get_file_content", file_path))
        return ToolResponse(success=False, error="gateway unavailable")


def test_tool_failure_and_empty_result_are_insufficient_but_count_actual_calls():
    dossier = _dossier()
    batch = _collect([dossier], [_request(dossier)], client=_FailingTools())

    limitations = {finding.limitation for finding in batch.notes[0].findings}
    assert "tool_failed" in limitations
    assert "tool_empty" in limitations
    assert len(batch.gathered_context) == 2


class _OtherFileSensitiveTools(_ToolClient):
    def find_sensitive_apis(self) -> ToolResponse:
        self.calls.append(("find_sensitive_apis", {}))
        return ToolResponse(
            success=True,
            result="| sink | call | src/Other.java:10 |",
        )


def test_global_sensitive_api_output_never_uses_other_file_as_evidence():
    dossier = _dossier()
    batch = _collect(
        [dossier],
        [_request(dossier)],
        client=_OtherFileSensitiveTools(),
    )

    sensitive = next(
        finding
        for finding in batch.notes[0].findings
        if finding.source == "tool:find_sensitive_apis"
    )
    assert sensitive.relation == "insufficient"
    assert sensitive.limitation == "no_task_sensitive_api"


def test_current_method_transaction_annotation_is_direct_counter_evidence():
    dossier, client = _authorization_scope_dossier(
        "class Service {\n\n\n\n\n\n\n\n@Transactional\npublic void update() { save(); }\n}",
        "@Transactional public void update()",
    )
    request = _request(dossier, "transaction_atomicity.counter")

    batch = _collect([dossier], [request], client=client)

    assert any(
        finding.relation == "contradicts" and finding.strength == "direct"
        for finding in batch.notes[0].findings
    )


def test_finding_trace_has_stable_required_fields():
    dossier = _dossier()
    request = _request(dossier)
    batch = _collect([dossier], [request], client=None)

    details = [
        json.loads(detail)
        for event, detail in batch.trace
        if event == "evidence_finding_recorded"
    ]
    assert details
    assert set(details[0]) == {
        "request_id",
        "candidate_id",
        "strategy_id",
        "purpose",
        "evidence_id",
        "source",
        "relation",
        "strength",
        "limitation",
        "observation",
    }


class _GlobalSensitiveTools(_ToolClient):
    def find_sensitive_apis(self) -> ToolResponse:
        self.calls.append(("find_sensitive_apis", {}))
        return ToolResponse(
            success=True,
            result=(
                "| sink | call | src/A.java:10 |\n"
                "| sink | call | src/B.java:20 |"
            ),
        )


def test_sensitive_cache_keeps_global_raw_and_slices_per_task_request():
    tasks = [
        ReviewTask(
            id="src/A.java#h0",
            file="src/A.java",
            hunk_header="@@ -10,1 +10,1 @@",
            patch="+a();",
            changed_lines=[10],
        ),
        ReviewTask(
            id="src/B.java#h0",
            file="src/B.java",
            hunk_header="@@ -20,1 +20,1 @@",
            patch="+b();",
            changed_lines=[20],
        ),
        ReviewTask(
            id="src/C.java#h0",
            file="src/C.java",
            hunk_header="@@ -30,1 +30,1 @@",
            patch="+c();",
            changed_lines=[30],
        ),
    ]
    dossiers = [
        _dossier(f"candidate-{index}", task=task)
        for index, task in enumerate(tasks, 1)
    ]
    requests = [_request(dossier) for dossier in dossiers]
    client = _GlobalSensitiveTools()
    llm = _StructuredLLM(
        {
            "relation": "insufficient",
            "strength": "contextual",
            "observation": "",
            "limitation": "not decisive",
        }
    )

    batch = _collect_with_llm(dossiers, requests, llm, client=client)

    assert client.calls.count(("find_sensitive_apis", {})) == 1
    sensitive_prompts = [
        call[1][1]
        for call in llm.messages
        if '"source":"tool:find_sensitive_apis"' in call[1][1]
    ]
    assert len(sensitive_prompts) == 2
    assert "src/A.java:10" in sensitive_prompts[0]
    assert "src/B.java:20" not in sensitive_prompts[0]
    assert "src/B.java:20" in sensitive_prompts[1]
    assert "src/A.java:10" not in sensitive_prompts[1]
    sensitive_findings = [
        next(
            finding
            for finding in note.findings
            if finding.source == "tool:find_sensitive_apis"
        )
        for note in batch.notes
    ]
    assert sensitive_findings[0].evidence_id != sensitive_findings[1].evidence_id
    assert sensitive_findings[2].limitation == "no_task_sensitive_api"


def test_request_target_cannot_use_candidate_basename_to_bypass_task_path():
    dossier = _dossier()
    dossier = CandidateDossier(
        candidate=dossier.candidate.model_copy(update={"file": "Service.java"}),
        task=dossier.task,
        risk_profile=dossier.risk_profile,
        context_bundle=dossier.context_bundle,
        requests=(),
        notes=(),
        latest_verdict=None,
    )
    request = _request(dossier).model_copy(update={"target": "Service.java"})
    client = _ToolClient()

    batch = _collect([dossier], [request], client=client)

    assert client.calls == []
    assert batch.notes[0].findings[0].limitation == "request_strategy_mismatch"


def test_evidence_digest_has_unambiguous_part_boundaries():
    module = importlib.import_module("codeguard_agent.pipeline.evidence_agent")

    assert module._digest("ab", "c") != module._digest("a", "bc")


def test_prior_empty_tool_finding_prevents_cross_round_repeat_call():
    dossier = _dossier()
    prior = EvidenceNote(
        request_id="prior-request",
        candidate_id=dossier.candidate.id,
        findings=[
            EvidenceFinding(
                evidence_id="prior-empty-file",
                source="tool:get_file_content",
                observation="",
                relation="insufficient",
                strength="contextual",
                limitation="tool_empty",
            )
        ],
    )
    dossier = CandidateDossier(
        candidate=dossier.candidate,
        task=dossier.task,
        risk_profile=dossier.risk_profile,
        context_bundle=dossier.context_bundle,
        requests=(),
        notes=(prior,),
        latest_verdict=None,
    )
    client = _ToolClient()

    batch = _collect([dossier], [_request(dossier)], client=client)

    assert ("get_file_content", dossier.task.file) not in client.calls
    assert len(batch.notes) == 1
    reused = next(
        finding
        for finding in batch.notes[0].findings
        if finding.evidence_id == "prior-empty-file"
    )
    assert reused.source == "prior:tool:get_file_content"
    assert reused.limitation == "tool_empty"


def test_overloaded_method_resolution_uses_task_matching_range_not_first_name():
    task = ReviewTask(
        id="src/Service.java#h0",
        file="src/Service.java",
        hunk_header="@@ -22,1 +22,1 @@",
        patch="+update(42);",
        changed_lines=[22],
    )
    context = TaskContextBundle(
        task_id=task.id,
        facts=[
            ContextFact(
                source="tool:get_diff_ast",
                kind="ast_structure",
                content=(
                    "AST for: src/Service.java\n"
                    "  class: Service\n"
                    "    @PreAuthorize public void update(String value) [L2-L5]\n"
                    "    public void update(int value) [L20-L25]"
                ),
            )
        ],
    )
    dossier = _dossier(task=task, context=context)
    client = _ToolClient(
        "class Service {\n"
        + "  @PreAuthorize void update(String value) {}\n"
        + "\n" * 17
        + "  void update(int value) {}\n"
        + "}"
    )

    batch = _collect([dossier], [_request(dossier)], client=client)

    assert not any(
        finding.relation == "contradicts" and finding.strength == "direct"
        for finding in batch.notes[0].findings
    )
