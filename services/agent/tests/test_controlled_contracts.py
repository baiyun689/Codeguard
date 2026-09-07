import json

import pytest

from codeguard_agent.models.tasks import (
    AssessmentStatus,
    CandidateSeed,
    CoverageDeclaration,
    CoverageDecision,
    DirectTriageResult,
    EvidenceNeed,
    GraphQuestion,
    ProofMatchStatus,
    ProofScope,
    ReviewerKind,
    TaskSymbolContext,
)
from codeguard_agent.pipeline.controlled import (
    bind_seed_ids,
    get_tool_proof_contract,
    match_graph_proof,
    route_seed,
    validate_coverage,
    validate_graph_question,
)
from codeguard_agent.models.tasks.symbols import ResolvedSymbol, SymbolResolutionStatus
from codeguard_agent.pipeline.controlled.graph_plan import run_graph_plan
from codeguard_agent.pipeline.controlled.llm_contracts import LlmDirectTriageResult


def _seed(**updates) -> CandidateSeed:
    values = {
        "reviewer": ReviewerKind.BEHAVIOR,
        "change_unit_id": "CU-01",
        "claim": "局部条件扩大了异常捕获范围",
        "mechanism": "catch 类型从 Exception 改成 Throwable",
        "location_file": "A.java",
        "proof_scope": ProofScope.LOCAL,
        "evidence_basis": ("changed_lines",),
    }
    values.update(updates)
    return CandidateSeed(**values)


def test_direct_seed_routes_without_confidence_threshold():
    assert route_seed(_seed(confidence=0.01)) == "direct_proven"


def test_cross_file_seed_requires_graph_question():
    seed = _seed(proof_scope=ProofScope.CROSS_FILE)
    assert route_seed(seed) == "unresolved"
    seed = seed.model_copy(update={
        "graph_question": GraphQuestion(
            subject_ref="S01",
            direction="downstream",
            path_kind="behavior",
            expected_targets=("target-1",),
            question="是否到达 target-1？",
        ),
        "evidence_need": EvidenceNeed.INSPECT_PATH,
    })
    assert route_seed(seed) == "graph_required"


def test_seed_ids_are_stable_and_llm_id_is_replaced():
    result = DirectTriageResult(
        coverage=(CoverageDeclaration(
            change_unit_id="CU-01",
            decision=CoverageDecision.LOCAL_ONLY,
            reason="局部证据足够",
        ),),
        issues=(_seed(seed_id="model-picked-id"),),
    )
    bound = bind_seed_ids(result)
    assert bound.issues[0].seed_id.startswith("seed-behavior-")
    assert bound.issues[0].seed_id != "model-picked-id"
    assert bind_seed_ids(result).issues[0].seed_id == bound.issues[0].seed_id


def test_coverage_requires_every_change_unit_once():
    result = DirectTriageResult(
        coverage=(CoverageDeclaration(
            change_unit_id="CU-01",
            decision=CoverageDecision.GRAPH_NEEDED,
            reason="需要验证下游路径",
        ),),
    )
    diagnostics = validate_coverage(
        change_unit_ids=("CU-01", "CU-02"), result=result
    )
    assert diagnostics == ("missing_coverage:CU-02",)


def test_graph_question_requires_target_or_relationship():
    question = GraphQuestion(
        subject_ref="S01",
        direction="downstream",
        path_kind="behavior",
        question="检查路径",
    )
    assert "graph_question_requires_target_or_relationship" in validate_graph_question(question)


def test_proof_matcher_requires_complete_path_for_proved():
    payload = {
        "schema_version": 2,
        "outcome": "found",
        "coverage": "complete",
        "subject_symbol_id": "s-do-execute",
        "relationships": [
            {"sourceId": "s-do-execute", "targetId": "s-open", "kind": "CALLS"},
            {"sourceId": "s-open", "targetId": "s-listener", "kind": "CALLS"},
        ],
        "unresolved_count": 0,
        "limitations": [],
    }
    question = GraphQuestion(
        subject_ref="S01",
        direction="downstream",
        path_kind="behavior",
        expected_targets=("s-listener",),
        required_relationships=("CALLS",),
        max_depth=3,
        question="是否到达 listener？",
    )
    match = match_graph_proof(
        work_item_id="WI-01",
        payload=json.dumps(payload),
        question=question,
        subject_symbol_id="s-do-execute",
    )
    assert match.status is ProofMatchStatus.PROVED
    assert match.matched_targets == ("s-listener",)

    payload["coverage"] = "partial"
    partial = match_graph_proof(
        work_item_id="WI-01",
        payload=json.dumps(payload),
        question=question,
        subject_symbol_id="s-do-execute",
    )
    assert partial.status is ProofMatchStatus.PARTIAL


def test_proof_matcher_resolves_class_and_method_name_aliases():
    payload = {
        "schema_version": 2,
        "outcome": "found",
        "coverage": "complete",
        "subject_symbol_id": "java:pkg.Entry#run()",
        "relationships": [
            {
                "sourceId": "java:pkg.Entry#run()",
                "targetId": "java:pkg.RetryTemplate#open()",
                "kind": "CALLS",
            },
            {
                "sourceId": "java:pkg.RetryTemplate#open()",
                "targetId": "java:pkg.RetryContextCache#get(java.lang.Object)",
                "kind": "CALLS",
            },
        ],
        "unresolved_count": 0,
        "limitations": [],
    }
    question = GraphQuestion(
        subject_ref="java:pkg.Entry#run()",
        direction="downstream",
        path_kind="behavior",
        expected_targets=("RetryContextCache",),
        required_relationships=("calls",),
        max_depth=3,
        question="是否到达缓存？",
    )
    match = match_graph_proof(
        work_item_id="WI-ALIAS",
        payload=json.dumps(payload),
        question=question,
        subject_symbol_id="java:pkg.Entry#run()",
    )
    assert match.status is ProofMatchStatus.PROVED
    assert match.matched_targets == ("java:pkg.RetryContextCache#get(java.lang.Object)",)


def test_proof_matcher_resolves_role_aliases_at_camelcase_boundary():
    """A reviewer may name a reachable type by its role rather than its class."""

    payload = {
        "schema_version": 2,
        "outcome": "found",
        "coverage": "complete",
        "subject_symbol_id": "java:pkg.Entry#run()",
        "relationships": [
            {
                "sourceId": "java:pkg.Entry#run()",
                "targetId": "java:pkg.RetryTemplate#open()",
                "kind": "CALLS",
            },
            {
                "sourceId": "java:pkg.RetryTemplate#open()",
                "targetId": "java:pkg.RetryListener#open()",
                "kind": "CALLS",
            },
            {
                "sourceId": "java:pkg.RetryTemplate#open()",
                "targetId": "java:pkg.RetryCallback#doWithRetry()",
                "kind": "CALLS",
            },
        ],
        "unresolved_count": 0,
        "limitations": [],
    }
    question = GraphQuestion(
        subject_ref="java:pkg.Entry#run()",
        direction="downstream",
        path_kind="behavior",
        expected_targets=("listener", "callback"),
        required_relationships=("CALLS",),
        max_depth=3,
        question="是否到达 listener 和 callback？",
    )

    match = match_graph_proof(
        work_item_id="WI-ROLE-ALIASES",
        payload=json.dumps(payload),
        question=question,
        subject_symbol_id="java:pkg.Entry#run()",
    )

    assert match.status is ProofMatchStatus.PROVED
    assert match.matched_targets == (
        "java:pkg.RetryCallback#doWithRetry()",
        "java:pkg.RetryListener#open()",
    )


def test_proof_matcher_does_not_turn_partial_not_found_into_absence():
    payload = {
        "schema_version": 2,
        "outcome": "found",
        "coverage": "partial",
        "subject_symbol_id": "s1",
        "relationships": [],
        "unresolved_count": 2,
        "limitations": ["projection_truncated"],
    }
    question = GraphQuestion(
        subject_ref="S01",
        direction="downstream",
        path_kind="behavior",
        expected_targets=("s2",),
        question="是否到达 s2？",
    )
    match = match_graph_proof(
        work_item_id="WI-01",
        payload=json.dumps(payload),
        question=question,
        subject_symbol_id="s1",
    )
    assert match.status is ProofMatchStatus.INDETERMINATE


def test_proof_matcher_preserves_positive_partial_relationship_when_target_is_omitted():
    """A found partial edge must reach the semantic Judge, not vanish early."""

    payload = {
        "schema_version": 2,
        "outcome": "found",
        "coverage": "partial",
        "subject_symbol_id": "s1",
        "relationships": [
            {"sourceId": "s1", "targetId": "s3", "kind": "CALLS"},
        ],
        "unresolved_count": 3,
        "limitations": ["projection_truncated"],
    }
    question = GraphQuestion(
        subject_ref="S01",
        direction="downstream",
        path_kind="behavior",
        expected_targets=("s2",),
        required_relationships=("CALLS",),
        question="是否到达 s2？",
    )

    match = match_graph_proof(
        work_item_id="WI-PARTIAL-POSITIVE",
        payload=json.dumps(payload),
        question=question,
        subject_symbol_id="s1",
    )

    assert match.status is ProofMatchStatus.PARTIAL
    assert "proof_positive_relation_target_unresolved" in match.limitations


def test_proof_matcher_does_not_combine_disconnected_target_and_relationship():
    payload = {
        "schema_version": 2,
        "outcome": "found",
        "coverage": "complete",
        "subject_symbol_id": "s1",
        "relationships": [
            {"sourceId": "s1", "targetId": "s2", "kind": "CALLS"},
            {"sourceId": "s1", "targetId": "s3", "kind": "READS"},
        ],
        "unresolved_count": 0,
        "limitations": [],
    }
    question = GraphQuestion(
        subject_ref="S01",
        direction="downstream",
        path_kind="behavior",
        expected_targets=("s2",),
        required_relationships=("READS",),
        question="s2 是否通过 READS 到达？",
    )
    match = match_graph_proof(
        work_item_id="WI-01",
        payload=json.dumps(payload),
        question=question,
        subject_symbol_id="s1",
    )
    assert match.status is ProofMatchStatus.NOT_FOUND


def test_proof_contracts_are_machine_readable():
    contract = get_tool_proof_contract("inspect_path")
    assert contract is not None
    assert "bounded downstream relationship facts" in contract.can_prove
    assert "behavior complete paths when path_kind=behavior" in contract.can_prove
    assert "sensitive call hits when path_kind=security" in contract.can_prove
    assert "path absence" in contract.cannot_prove
    assert "complete security data-flow or parameter propagation" in contract.cannot_prove
    assert contract.required_arguments == ("symbol_id", "path_kind", "max_depth")


def test_controlled_models_reject_unknown_fields():
    with pytest.raises(Exception):
        _seed(unexpected="nope")


def test_provider_envelope_drops_display_metadata_before_strict_runtime_validation():
    raw = {
        "coverage": [{
            "change_unit_id": "CU-01",
            "decision": "local_only",
            "reason": "局部证据足够",
            "provider_display_note": "ignored",
        }],
        "issues": [{
            **_seed().model_dump(),
            "provider_display_note": "ignored",
        }],
    }
    tolerant = LlmDirectTriageResult.model_validate(raw)
    strict = DirectTriageResult.model_validate(tolerant.model_dump())
    assert strict.issues[0].claim == "局部条件扩大了异常捕获范围"
    with pytest.raises(Exception):
        DirectTriageResult.model_validate(raw)


def test_controlled_model_accepts_common_mechanism_note_alias():
    seed = _seed(mechanism_note="模型补充的机制说明")
    assert seed.mechanism_note == "模型补充的机制说明"


def test_assessment_accepts_indeterminate_status():
    assert AssessmentStatus("indeterminate") is AssessmentStatus.INDETERMINATE


def test_graph_plan_failure_uses_minimal_baseline_plan():
    seed = _seed(
        proof_scope=ProofScope.CROSS_FILE,
        evidence_need=EvidenceNeed.INSPECT_PATH,
        graph_question=GraphQuestion(
            subject_ref="s1",
            direction="downstream",
            path_kind="behavior",
            expected_targets=("s2",),
            question="是否到达 s2？",
        ),
    )
    context = TaskSymbolContext(
        task_id="A.java#h0",
        status=SymbolResolutionStatus.RESOLVED,
        symbols=(
            ResolvedSymbol(
                file="A.java",
                symbol_id="s1",
                kind="METHOD",
                start_line=1,
                end_line=4,
                source_set="MAIN",
            ),
        ),
    )
    plan, diagnostics = run_graph_plan(
        reviewer=ReviewerKind.BEHAVIOR,
        task_id="A.java#h0",
        seeds=(seed.model_copy(update={"seed_id": "seed-1"}),),
        symbol_context=context,
        llm=None,
        max_retries=1,
        structured_method="function_calling",
    )
    assert len(plan.work_items) == 1
    assert plan.work_items[0].evidence_steps[0].tool == "inspect_path"
    assert "graph_plan_baseline_used" in diagnostics
