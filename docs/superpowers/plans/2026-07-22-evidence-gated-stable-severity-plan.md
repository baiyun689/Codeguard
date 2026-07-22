# Evidence-Gated Stable Severity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Filter candidates that lack supporting evidence and assign repeatable severities through primary-RiskTag policies with evidence-cited CRITICAL requirements.

**Architecture:** Keep the graph seam `EvidencePlanner -> EvidenceAgent -> CouncilJudge`, but make it one-pass. CouncilJudge becomes a deep module containing a deterministic evidence gate, one constrained LLM evidence-synthesis call, and a deterministic severity-policy resolver; hunk tags remain routing priors and only the candidate's resolved primary tag selects a severity policy.

**Tech Stack:** Python 3.11+, Pydantic v2, LangGraph, LangChain structured output, pytest, Ruff, mypy.

## Global Constraints

- Run Python commands from `services/agent` with `conda run -n codeguard --no-capture-output ...`.
- Preserve all pre-existing uncommitted work. Before editing an already-dirty file, inspect `git diff -- <path>` and merge with it; never restore or overwrite it.
- The dirty files at plan creation include `models/tasks.py`, `pipeline/evidence_planner.py`, `pipeline/evidence_rules/security.py`, `pipeline/evidence_rules/terms.py`, `pipeline/risk_rules/catalog.py`, `pipeline/risk_rules/security.py`, `prompts/council-judge.txt`, `tests/test_evidence_planner.py`, `tests/test_risk_rules.py`, and `tests/test_tasks_models.py`.
- Do not stage pre-existing user changes as an implementation commit. Commit only when the staged diff can be shown to contain implementation-owned changes exclusively; otherwise leave the task changes unstaged and report the checkpoint.
- Keep the product `Issue` schema unchanged.
- Keep `EvidencePurpose` and `EvidenceRequest.question`; do not introduce a parallel obligation model.
- Keep hunk/task `RiskTag` values as non-authoritative routing priors. Only the candidate's primary tag selects a severity policy.
- Keep `CandidateIssue.severity_proposal` for trace only; it must not affect resolved severity.
- Preserve the historical `AggregationStage` and aggregation prompts. Remove merge behavior only from the active CouncilJudge path.
- The approved design is `docs/superpowers/specs/2026-07-22-evidence-gated-stable-severity-design.md`.

---

## File Structure

- Create `services/agent/src/codeguard_agent/pipeline/severity_policy.py`: static policy registry, factor-proof validation, deterministic severity resolution.
- Create `services/agent/tests/test_severity_policy.py`: complete registry and critical-factor behavior tests.
- Modify `services/agent/src/codeguard_agent/models/council.py`: synthesis output models and simplified active `Verdict`.
- Modify `services/agent/src/codeguard_agent/pipeline/evidence_planner.py`: mandatory one-pass support/severity and all registered counter strategies; delete follow-up planning.
- Modify `services/agent/src/codeguard_agent/pipeline/council_judge.py`: evidence gate, synthesis, policy resolution, and final Issue mapping; delete merge and round logic.
- Modify `services/agent/src/codeguard_agent/pipeline/graph.py`: direct Judge-to-END edge and removal of evidence-round state.
- Modify `services/agent/src/codeguard_agent/config.py`, `cli.py`, and `pipeline/orchestrator.py`: remove max-evidence-round configuration plumbing.
- Modify active prompts under `services/agent/src/codeguard_agent/prompts/`: align LLM contracts with evidence synthesis and provisional discovery severity.
- Modify `services/agent/src/codeguard_agent/pipeline/council_metrics.py`, `models/council.py`, `services/agent/evals/schema.py`, and `services/agent/evals/report.py`: expose evidence-gate and stable-severity diagnostics.
- Modify focused tests in `services/agent/tests/`: replace round/merge/downgrade expectations with one-pass gate and policy expectations.
- Modify `.env.example`, `AGENTS.md`, and, only where public behavior changes, `README.md`.

---

### Task 1: Add the evidence assessment models and deterministic severity registry

**Files:**
- Create: `services/agent/src/codeguard_agent/pipeline/severity_policy.py`
- Create: `services/agent/tests/test_severity_policy.py`
- Modify: `services/agent/src/codeguard_agent/models/council.py`
- Test: `services/agent/tests/test_evidence_models.py`

**Interfaces:**
- Produces: `SeverityFactorAssessment`, `CandidateEvidenceAssessment`, `SeverityFactorDefinition`, `SeverityPolicy`, `SeverityResolution`, `policy_for(tag)`, `factor_is_proven(...)`, and `resolve_severity(tag, assessments, findings_by_id)`.
- Consumes: existing `RiskTag`, `Severity`, and `EvidenceFinding`.

- [ ] **Step 1: Add failing model tests**

Replace obsolete `JudgeDecision` field tests in `tests/test_evidence_models.py` with exact structured-assessment validation:

```python
def test_candidate_evidence_assessment_accepts_only_bounded_factor_states():
    assessment = CandidateEvidenceAssessment(
        candidate_id="C001",
        claim_status="supported",
        counter_effect="partial",
        severity_factors=[
            SeverityFactorAssessment(
                factor_id="untrusted_input",
                status="proven",
                evidence_ids=["E1"],
                reason="request parameter reaches the query builder",
            )
        ],
        conflicts=[],
        reason="candidate remains supported after partial mitigation",
    )
    assert assessment.severity_factors[0].status == "proven"


def test_synthesis_model_rejects_unknown_status():
    with pytest.raises(ValidationError):
        SeverityFactorAssessment(
            factor_id="untrusted_input",
            status="likely",
            evidence_ids=["E1"],
            reason="invalid unbounded state",
        )
```

- [ ] **Step 2: Add failing registry tests**

Create `tests/test_severity_policy.py` with the exact approved defaults and ceilings:

```python
EXPECTED_LEVELS = {
    RiskTag.AUTHORIZATION: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.AUTHENTICATION_SESSION: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.WEB_SECURITY_CONFIG: (Severity.WARNING, Severity.WARNING),
    RiskTag.INPUT_VALIDATION: (Severity.WARNING, Severity.WARNING),
    RiskTag.INJECTION: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.SQL_DATA_ACCESS: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.FILE_PATH_IO: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.SSRF_OUTBOUND: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.CONFIG_SECURITY: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.DATA_EXPOSURE: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.DESERIALIZATION: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.TRANSACTION_ATOMICITY: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.CONCURRENCY_CONSISTENCY: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.IDEMPOTENCY_RETRY: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.CACHE_CONSISTENCY: (Severity.WARNING, Severity.WARNING),
    RiskTag.MESSAGE_DELIVERY: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.ERROR_HANDLING: (Severity.WARNING, Severity.WARNING),
    RiskTag.NULL_STATE_SAFETY: (Severity.WARNING, Severity.WARNING),
    RiskTag.RESOURCE_LIFECYCLE: (Severity.WARNING, Severity.WARNING),
    RiskTag.API_CONTRACT: (Severity.WARNING, Severity.WARNING),
    RiskTag.PERFORMANCE: (Severity.WARNING, Severity.WARNING),
    RiskTag.COMPLEXITY_CONTROL_FLOW: (Severity.INFO, Severity.INFO),
    RiskTag.DUPLICATION_DESIGN: (Severity.INFO, Severity.INFO),
    RiskTag.OBSERVABILITY_TESTABILITY: (Severity.INFO, Severity.INFO),
    RiskTag.GENERAL_REVIEW: (Severity.WARNING, Severity.WARNING),
}


def test_every_risk_tag_has_exact_default_and_ceiling():
    assert set(EXPECTED_LEVELS) == set(RiskTag)
    assert {
        tag: (policy_for(tag).default_severity, policy_for(tag).maximum_severity)
        for tag in RiskTag
    } == EXPECTED_LEVELS
```

Add focused resolution tests using helpers that build `EvidenceFinding` objects:

```python
def finding(
    evidence_id: str,
    *,
    source: str,
    strength: str = "direct",
) -> EvidenceFinding:
    return EvidenceFinding(
        evidence_id=evidence_id,
        source=source,
        observation=f"observation for {evidence_id}",
        relation="supports",
        strength=strength,
    )


def proven_factors(
    factor_ids: tuple[str, ...],
) -> tuple[list[SeverityFactorAssessment], dict[str, EvidenceFinding]]:
    assessments: list[SeverityFactorAssessment] = []
    findings: dict[str, EvidenceFinding] = {}
    for index, factor_id in enumerate(factor_ids):
        evidence_id = f"E{index}"
        assessments.append(
            SeverityFactorAssessment(
                factor_id=factor_id,
                status="proven",
                evidence_ids=[evidence_id],
                reason=f"{factor_id} is directly proven",
            )
        )
        findings[evidence_id] = finding(
            evidence_id,
            source=f"tool:source-{index}",
        )
    return assessments, findings


def test_injection_requires_every_critical_factor():
    policy = policy_for(RiskTag.INJECTION)
    assessments, findings = proven_factors(policy.critical_requires)
    result = resolve_severity(RiskTag.INJECTION, assessments, findings)
    assert result.severity is Severity.CRITICAL
    assert result.missing_critical_factors == ()


def test_one_missing_critical_factor_falls_back_to_warning():
    policy = policy_for(RiskTag.INJECTION)
    assessments, findings = proven_factors(policy.critical_requires[:-1])
    result = resolve_severity(RiskTag.INJECTION, assessments, findings)
    assert result.severity is Severity.WARNING
    assert result.missing_critical_factors == (policy.critical_requires[-1],)


def test_critical_factor_rejects_single_contextual_source():
    assessment = SeverityFactorAssessment(
        factor_id="untrusted_input",
        status="proven",
        evidence_ids=["E1"],
        reason="one contextual observation",
    )
    findings = {"E1": finding("E1", source="task_patch", strength="contextual")}
    result = resolve_severity(RiskTag.INJECTION, [assessment], findings)
    assert result.severity is Severity.WARNING


def test_two_distinct_contextual_sources_can_prove_factor():
    assessment = SeverityFactorAssessment(
        factor_id="untrusted_input",
        status="proven",
        evidence_ids=["E1", "E2"],
        reason="corroborated observations",
    )
    findings = {
        "E1": finding("E1", source="task_patch", strength="contextual"),
        "E2": finding("E2", source="tool:get_file_content", strength="contextual"),
    }
    assert factor_is_proven(assessment, findings)
```

- [ ] **Step 3: Run the focused tests and verify failure**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_evidence_models.py tests/test_severity_policy.py -q
```

Expected: collection fails because the new models and `severity_policy` module do not exist.

- [ ] **Step 4: Add assessment models**

In `models/council.py`, replace active Judge structured-output models with:

```python
class SeverityFactorAssessment(BaseModel):
    factor_id: NonBlankStr
    status: Literal["proven", "disproven", "unknown"]
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class CandidateEvidenceAssessment(BaseModel):
    candidate_id: NonBlankStr
    claim_status: Literal["supported", "refuted", "unresolved"]
    counter_effect: Literal["none", "partial", "complete", "unknown"]
    severity_factors: list[SeverityFactorAssessment] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    reason: str = ""
```

Keep the old `Verdict` shape temporarily until Task 4 updates all active callers; remove `JudgeDecision` and `JudgeDecisions` only when no active import remains.

- [ ] **Step 5: Implement the policy registry and proof resolver**

Create `pipeline/severity_policy.py` with immutable definitions. Use the exact defaults, ceilings, allowlist, and critical factor IDs from the approved design. The core proof and resolution logic must be:

```python
@dataclass(frozen=True)
class SeverityFactorDefinition:
    id: str
    description: str


@dataclass(frozen=True)
class SeverityPolicy:
    tag: RiskTag
    default_severity: Severity
    maximum_severity: Severity
    factors: tuple[SeverityFactorDefinition, ...]
    critical_requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeverityResolution:
    severity: Severity
    matched_rule: str
    proven_factors: tuple[str, ...]
    missing_critical_factors: tuple[str, ...]
    evidence_ids: tuple[str, ...]


CRITICAL_FACTORS: dict[RiskTag, tuple[str, ...]] = {
    RiskTag.AUTHORIZATION: (
        "untrusted_actor_reachable",
        "effective_authorization_absent",
        "high_value_cross_boundary_impact",
    ),
    RiskTag.AUTHENTICATION_SESSION: (
        "credential_or_session_control",
        "effective_session_validation_absent",
        "account_takeover_or_broad_scope",
    ),
    RiskTag.INJECTION: (
        "untrusted_input",
        "dangerous_interpreter_sink",
        "effective_mitigation_absent",
        "high_impact_execution_or_data",
    ),
    RiskTag.SQL_DATA_ACCESS: (
        "dangerous_data_operation",
        "scope_constraint_absent",
        "operation_reachable",
        "broad_irreversible_or_cross_tenant_impact",
    ),
    RiskTag.FILE_PATH_IO: (
        "untrusted_path",
        "filesystem_sink_reached",
        "effective_confinement_absent",
        "sensitive_read_or_arbitrary_write",
    ),
    RiskTag.SSRF_OUTBOUND: (
        "untrusted_destination",
        "outbound_sink_reached",
        "effective_network_restriction_absent",
        "credential_or_privileged_internal_impact",
    ),
    RiskTag.CONFIG_SECURITY: (
        "production_reachable",
        "security_control_disabled_or_secret_exposed",
        "broad_privileged_impact",
    ),
    RiskTag.DATA_EXPOSURE: (
        "sensitive_data_flow",
        "unauthorized_audience_reachable",
        "effective_redaction_or_access_control_absent",
        "broad_or_high_value_scope",
    ),
    RiskTag.DESERIALIZATION: (
        "untrusted_payload",
        "unsafe_deserializer_reached",
        "effective_type_restriction_absent",
        "code_execution_or_privileged_impact",
    ),
    RiskTag.TRANSACTION_ATOMICITY: (
        "critical_multi_step_state_change",
        "atomicity_gap",
        "failure_or_interleaving_reachable",
        "irreversible_financial_or_data_impact",
    ),
    RiskTag.CONCURRENCY_CONSISTENCY: (
        "shared_critical_state",
        "race_reachable",
        "effective_synchronization_absent",
        "financial_or_data_integrity_impact",
    ),
    RiskTag.IDEMPOTENCY_RETRY: (
        "duplicate_execution_reachable",
        "effective_idempotency_protection_absent",
        "irreversible_high_value_action",
    ),
    RiskTag.MESSAGE_DELIVERY: (
        "critical_event",
        "loss_duplicate_or_order_failure_reachable",
        "effective_delivery_protection_absent",
        "irreversible_high_impact",
    ),
}


LEVELS: dict[RiskTag, tuple[Severity, Severity]] = {
    RiskTag.AUTHORIZATION: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.AUTHENTICATION_SESSION: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.WEB_SECURITY_CONFIG: (Severity.WARNING, Severity.WARNING),
    RiskTag.INPUT_VALIDATION: (Severity.WARNING, Severity.WARNING),
    RiskTag.INJECTION: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.SQL_DATA_ACCESS: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.FILE_PATH_IO: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.SSRF_OUTBOUND: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.CONFIG_SECURITY: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.DATA_EXPOSURE: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.DESERIALIZATION: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.TRANSACTION_ATOMICITY: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.CONCURRENCY_CONSISTENCY: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.IDEMPOTENCY_RETRY: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.CACHE_CONSISTENCY: (Severity.WARNING, Severity.WARNING),
    RiskTag.MESSAGE_DELIVERY: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.ERROR_HANDLING: (Severity.WARNING, Severity.WARNING),
    RiskTag.NULL_STATE_SAFETY: (Severity.WARNING, Severity.WARNING),
    RiskTag.RESOURCE_LIFECYCLE: (Severity.WARNING, Severity.WARNING),
    RiskTag.API_CONTRACT: (Severity.WARNING, Severity.WARNING),
    RiskTag.PERFORMANCE: (Severity.WARNING, Severity.WARNING),
    RiskTag.COMPLEXITY_CONTROL_FLOW: (Severity.INFO, Severity.INFO),
    RiskTag.DUPLICATION_DESIGN: (Severity.INFO, Severity.INFO),
    RiskTag.OBSERVABILITY_TESTABILITY: (Severity.INFO, Severity.INFO),
    RiskTag.GENERAL_REVIEW: (Severity.WARNING, Severity.WARNING),
}


POLICIES: dict[RiskTag, SeverityPolicy] = {
    tag: SeverityPolicy(
        tag=tag,
        default_severity=levels[0],
        maximum_severity=levels[1],
        factors=tuple(
            SeverityFactorDefinition(
                id=factor_id,
                description=factor_id.replace("_", " "),
            )
            for factor_id in CRITICAL_FACTORS.get(tag, ())
        ),
        critical_requires=CRITICAL_FACTORS.get(tag, ()),
    )
    for tag, levels in LEVELS.items()
}


def policy_for(tag: RiskTag) -> SeverityPolicy:
    return POLICIES[tag]


def factor_is_proven(
    assessment: SeverityFactorAssessment,
    findings_by_id: Mapping[str, EvidenceFinding],
) -> bool:
    if assessment.status != "proven":
        return False
    cited = [findings_by_id[item] for item in assessment.evidence_ids if item in findings_by_id]
    supporting = [item for item in cited if item.relation == "supports"]
    if any(item.strength == "direct" for item in supporting):
        return True
    contextual_sources = {
        item.source for item in supporting if item.strength == "contextual"
    }
    return len(contextual_sources) >= 2


def resolve_severity(
    tag: RiskTag,
    assessments: Sequence[SeverityFactorAssessment],
    findings_by_id: Mapping[str, EvidenceFinding],
) -> SeverityResolution:
    policy = policy_for(tag)
    by_id = {
        item.factor_id: item
        for item in assessments
        if item.factor_id in {factor.id for factor in policy.factors}
    }
    proven = tuple(
        factor_id
        for factor_id in policy.critical_requires
        if factor_id in by_id and factor_is_proven(by_id[factor_id], findings_by_id)
    )
    missing = tuple(item for item in policy.critical_requires if item not in proven)
    severity = (
        Severity.CRITICAL
        if policy.maximum_severity is Severity.CRITICAL
        and policy.critical_requires
        and not missing
        else policy.default_severity
    )
    evidence_ids = tuple(
        dict.fromkeys(
            evidence_id
            for factor_id in proven
            for evidence_id in by_id[factor_id].evidence_ids
            if evidence_id in findings_by_id
        )
    )
    return SeverityResolution(
        severity=severity,
        matched_rule=(f"{tag.value.lower()}.critical" if not missing else f"{tag.value.lower()}.default"),
        proven_factors=proven,
        missing_critical_factors=missing,
        evidence_ids=evidence_ids,
    )
```

For non-CRITICAL policies, `critical_requires` is empty and resolution always returns the configured default, which equals or remains below the maximum.

- [ ] **Step 6: Run focused tests**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_evidence_models.py tests/test_severity_policy.py -q
conda run -n codeguard --no-capture-output ruff check src/codeguard_agent/models/council.py src/codeguard_agent/pipeline/severity_policy.py tests/test_severity_policy.py
```

Expected: all focused tests pass and Ruff reports no errors.

- [ ] **Step 7: Checkpoint commit when cleanly stageable**

Stage only the new module, its new tests, and owned model hunks. Verify with `git diff --cached` before committing:

```powershell
git commit -m "feat(pipeline): 增加证据驱动的固定定级策略"
```

If model hunks cannot be separated from user changes, commit only the new files and leave the model change unstaged.

---

### Task 2: Make evidence planning complete and one-pass

**Files:**
- Modify: `services/agent/src/codeguard_agent/pipeline/evidence_planner.py`
- Modify: `services/agent/tests/test_evidence_planner.py`

**Interfaces:**
- Produces: `plan_evidence(dossiers, *, classifier_llm, structured_method) -> EvidencePlan` with no `evidence_round` parameter.
- Consumes: `strategies_for(primary_tag, purpose)` and existing dossier bindings.

- [ ] **Step 1: Preserve and inspect existing dirty changes**

Run:

```powershell
git diff -- src/codeguard_agent/pipeline/evidence_planner.py tests/test_evidence_planner.py
```

Record that the current user work already raises the request cap to three and adds initial severity planning. The implementation must build on those changes rather than replace them.

- [ ] **Step 2: Replace follow-up tests with complete-plan tests**

Delete tests that construct `needs_more_evidence` verdicts. Add these assertions:

```python
def test_initial_plan_always_contains_support_and_severity(monkeypatch):
    dossier = _dossier(candidate_id="candidate-1", confidence=1.0)
    _resolve_as(monkeypatch, RiskTag.GENERAL_REVIEW)
    plan = plan_evidence(
        [dossier], classifier_llm=None, structured_method="function_calling"
    )
    assert [request.purpose for request in plan.requests] == [
        "counter", "support", "severity"
    ]


def test_authorization_plan_contains_local_and_upstream_counter(monkeypatch):
    dossier = _dossier(candidate_id="candidate-auth")
    _resolve_as(monkeypatch, RiskTag.AUTHORIZATION)
    plan = plan_evidence(
        [dossier], classifier_llm=None, structured_method="function_calling"
    )
    assert [request.strategy_id for request in plan.requests] == [
        "authorization.counter",
        "authorization.counter_upstream",
        "authorization.support",
        "authorization.severity",
    ]


def test_plan_interface_has_no_evidence_round():
    assert "evidence_round" not in inspect.signature(plan_evidence).parameters
```

Update the explicit cap test to assert `MAX_INITIAL_REQUESTS_PER_CANDIDATE == 4`.

- [ ] **Step 3: Run planner tests and verify failure**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_evidence_planner.py -q
```

Expected: failures show gated support, missing upstream counter, follow-up behavior, and the old round parameter.

- [ ] **Step 4: Implement one-pass planning**

Set the cap to four. Remove `_needs_initial_support`, `_initial_counter_strategy`, `_plan_followup`, `latest_verdict` consumption, and round-specific trace fields. For each resolved dossier, append all unqueued counter strategies in priority order, then one support strategy, then one severity strategy:

```python
for dossier, resolution, excluded in resolved:
    for strategy in strategies_for(resolution.tag, "counter"):
        if strategy.id in excluded:
            continue
        _append_request(plan, dossier, strategy, reason="initial_counter")
        excluded.add(strategy.id)

for purpose in ("support", "severity"):
    for dossier, resolution, excluded in resolved:
        strategy = _next_strategy(resolution.tag, purpose, excluded)
        if strategy is None:
            _trace_no_initial_strategy(plan, dossier, resolution, purpose)
            continue
        _append_request(plan, dossier, strategy, reason=f"initial_{purpose}")
        excluded.add(strategy.id)
```

Before appending, enforce the four-request cap and emit `evidence_plan_skipped` with `reason="candidate_request_cap"` if a registry change would exceed it. Remove the `evidence_round` parameter from `_append_request`, `_plan_initial`, and `plan_evidence`.

- [ ] **Step 5: Run planner tests and lint**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_evidence_planner.py tests/test_evidence_rules.py -q
conda run -n codeguard --no-capture-output ruff check src/codeguard_agent/pipeline/evidence_planner.py tests/test_evidence_planner.py
```

Expected: all tests pass; every tag has support/severity and all registered counters in stable order.

- [ ] **Step 6: Leave an unstaged checkpoint if user changes cannot be separated**

Do not commit the entire dirty Planner or test file. Verify the combined diff and continue only if both the existing DESERIALIZATION/severity work and the new one-pass behavior are present.

---

### Task 3: Rewrite prompt contracts for evidence synthesis

**Files:**
- Modify: `services/agent/src/codeguard_agent/prompts/council-judge.txt`
- Modify: `services/agent/src/codeguard_agent/prompts/evidence-analysis.txt`
- Modify: `services/agent/src/codeguard_agent/prompts/evidence-tag-classifier-system.txt`
- Modify: `services/agent/src/codeguard_agent/prompts/threat-model-base.txt`
- Modify: `services/agent/src/codeguard_agent/prompts/behavior-base.txt`
- Modify: `services/agent/src/codeguard_agent/prompts/maintainability-base.txt`
- Modify: `services/agent/tests/test_prompt_contracts.py`

**Interfaces:**
- Produces: prompt contract for `CandidateEvidenceAssessment`.
- Consumes: the factor definitions supplied by CouncilJudge and existing `_EvidenceAnalysis` model.

- [ ] **Step 1: Add failing prompt-contract assertions**

Replace action-field assertions with:

```python
def test_council_judge_prompt_is_evidence_synthesis_only():
    judge = _prompt("council-judge.txt")
    for field in (
        "claim_status",
        "counter_effect",
        "severity_factors",
        "factor_id",
        "status",
        "evidence_ids",
        "conflicts",
    ):
        assert field in judge
    for forbidden in (
        "needs_more_evidence",
        "merge_target_id",
        "adjusted_severity",
        "requested_purpose",
    ):
        assert forbidden not in judge
    assert "不得输出最终 severity" in judge
    assert "任务 RiskTag 只能作为背景" in judge


def test_evidence_prompt_defines_relation_against_candidate():
    prompt = _prompt("evidence-analysis.txt")
    assert "relation 始终相对于候选主张" in prompt
    assert "不得建议 CRITICAL、WARNING 或 INFO" in prompt


def test_discoverer_prompts_mark_severity_as_provisional():
    for name in ("threat-model-base.txt", "behavior-base.txt", "maintainability-base.txt"):
        assert "severity 只是发现阶段的初步建议" in _prompt(name)
```

- [ ] **Step 2: Run prompt tests and verify failure**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_prompt_contracts.py -q
```

Expected: the new contract assertions fail against the action-oriented Judge prompt.

- [ ] **Step 3: Rewrite `council-judge.txt` without overwriting user changes blindly**

First inspect its dirty diff. Replace the action-oriented contract with a synthesis contract that contains these exact rules:

```text
返回 CandidateEvidenceAssessment，只能评估输入候选。
claim_status 只能是 supported、refuted、unresolved。
counter_effect 只能是 none、partial、complete、unknown。
severity_factors 只能使用输入 allowed_factor_ids 中的 factor_id。
每个 proven/disproven factor 必须引用当前候选的 evidence_ids。
insufficient finding 不能证明或反驳 factor。
未找到保护不等于证明保护不存在。
任务 RiskTag 只能作为背景，primary RiskTag 才选择定级规则。
不得输出最终 severity，不得选择 keep/drop，不得请求工具、补证或 merge。
```

- [ ] **Step 4: Tighten the analysis, classifier, and discoverer prompts**

Add the approved wording:

```text
relation 始终相对于候选主张，purpose 只表示本次调查视角。
severity finding 只能描述可达性、前置条件、影响和范围，不得建议 CRITICAL、WARNING 或 INFO。
```

Tell the tag classifier that a task can have multiple prior tags but it must select one candidate primary tag from candidate semantics; ambiguity returns `GENERAL_REVIEW` through existing validation. Add the provisional-severity sentence to each discoverer base prompt without changing its existing methodology.

- [ ] **Step 5: Run prompt tests**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_prompt_contracts.py -q
```

Expected: all prompt contract tests pass.

- [ ] **Step 6: Commit only cleanly owned prompt hunks**

Because `council-judge.txt` was dirty before implementation, do not stage it unless the staged diff can exclude the pre-existing change. Other clean prompt files and contract tests may be committed as:

```powershell
git commit -m "refactor(prompts): 收紧证据综合与初步定级职责"
```

---

### Task 4: Refactor CouncilJudge into gate, synthesis, and policy resolution

**Files:**
- Modify: `services/agent/src/codeguard_agent/pipeline/council_judge.py`
- Modify: `services/agent/src/codeguard_agent/models/council.py`
- Replace focused behavior in: `services/agent/tests/test_council_judge.py`

**Interfaces:**
- Consumes: `CandidateEvidenceAssessment`, `policy_for`, `resolve_severity`, dossiers, and valid request-bound findings.
- Produces: `judge_candidates(assembly, *, judge_llm, structured_method, max_retries) -> JudgeBatch` and simplified `Verdict(action="keep"|"drop", resolved_severity=...)`.

- [ ] **Step 1: Replace obsolete Judge tests with gate tests**

Add test doubles and exact cases:

```python
class _FailIfCalledLLM:
    def with_structured_output(self, schema, method):
        raise AssertionError("LLM must not run when the deterministic gate decides")


class _ReturningNoneStructured:
    def invoke(self, messages):
        return None


class _ReturningNoneLLM:
    def with_structured_output(self, schema, method):
        return _ReturningNoneStructured()


class _AssessmentStructured:
    def __init__(self, owner):
        self.owner = owner

    def invoke(self, messages):
        self.owner.calls += 1
        return self.owner.assessment


class _AssessmentLLM:
    def __init__(self, assessment):
        self.assessment = assessment
        self.calls = 0

    def with_structured_output(self, schema, method):
        assert schema is CandidateEvidenceAssessment
        return _AssessmentStructured(self)


def supported_assessment(**updates) -> CandidateEvidenceAssessment:
    values = {
        "candidate_id": "C001",
        "claim_status": "supported",
        "counter_effect": "none",
        "severity_factors": [],
        "conflicts": [],
        "reason": "support evidence establishes the candidate",
    }
    values.update(updates)
    return CandidateEvidenceAssessment(**values)


def supported_dossier(
    *,
    tag: RiskTag = RiskTag.AUTHORIZATION,
    proposed: Severity = Severity.WARNING,
    factor_ids: tuple[str, ...] = (),
) -> CandidateDossier:
    base = _dossier(severity=proposed)
    strategy = strategies_for(tag, "support")[0]
    request = EvidenceRequest(
        candidate_id=base.candidate.id,
        strategy_id=strategy.id,
        purpose="support",
        target=base.task.file,
        question=strategy.question_template,
        preferred_tools=list(strategy.allowed_tools),
    )
    findings = [
        _finding("supports", "direct", evidence_id="claim-support"),
        *[
            _finding("supports", "direct", evidence_id=f"factor-{index}")
            for index, _ in enumerate(factor_ids)
        ],
    ]
    note = EvidenceNote(
        request_id=request.id,
        candidate_id=base.candidate.id,
        findings=findings,
    )
    return replace(base, requests=(request,), notes=(note,))


def injection_critical_assessment() -> CandidateEvidenceAssessment:
    factors = policy_for(RiskTag.INJECTION).critical_requires
    return supported_assessment(
        severity_factors=[
            SeverityFactorAssessment(
                factor_id=factor_id,
                status="proven",
                evidence_ids=[f"factor-{index}"],
                reason=f"{factor_id} is directly proven",
            )
            for index, factor_id in enumerate(factors)
        ]
    )


def test_direct_counter_drops_before_llm_call():
    dossier = _dossier(request_findings=[("counter", _finding("contradicts", "direct"))])
    llm = _FailIfCalledLLM()
    batch = _judge([dossier], llm=llm)
    assert batch.verdicts[0].action == "drop"
    assert batch.verdicts[0].reason_code == "direct_counter_evidence"


def test_all_insufficient_drops_before_llm_call():
    dossier = _dossier(request_findings=[("support", _finding("insufficient"))])
    batch = _judge([dossier], llm=_FailIfCalledLLM())
    assert batch.verdicts[0].reason_code == "evidence_insufficient"


def test_no_support_purpose_finding_drops_candidate():
    dossier = _dossier(request_findings=[("severity", _finding("supports", "direct"))])
    batch = _judge([dossier], llm=_FailIfCalledLLM())
    assert batch.verdicts[0].reason_code == "no_supporting_evidence"


def test_contextual_support_enters_synthesis():
    dossier = _dossier(request_findings=[("support", _finding("supports", "contextual"))])
    llm = _AssessmentLLM(supported_assessment())
    batch = _judge([dossier], llm=llm)
    assert llm.calls == 1
    assert batch.verdicts[0].action == "keep"
```

- [ ] **Step 2: Add synthesis and fallback tests**

```python
def test_complete_counter_effect_drops_candidate():
    assessment = supported_assessment(counter_effect="complete")
    batch = _judge([supported_dossier()], llm=_AssessmentLLM(assessment))
    assert batch.verdicts[0].reason_code == "synthesized_counter_evidence"


def test_unresolved_conflict_drops_candidate():
    assessment = supported_assessment(
        claim_status="unresolved", conflicts=["upstream guard coverage is unclear"]
    )
    batch = _judge([supported_dossier()], llm=_AssessmentLLM(assessment))
    assert batch.verdicts[0].reason_code == "evidence_conflict_unresolved"


def test_llm_failure_keeps_gate_passed_candidate_at_policy_default():
    dossier = supported_dossier(tag=RiskTag.INJECTION, proposed=Severity.CRITICAL)
    batch = _judge([dossier], llm=_ReturningNoneLLM())
    assert batch.final_issues[0].severity is Severity.WARNING
    assert batch.verdicts[0].reason_code == "severity_evidence_incomplete"


def test_proposed_severity_never_changes_resolved_severity():
    factors = policy_for(RiskTag.INJECTION).critical_requires
    low = supported_dossier(
        tag=RiskTag.INJECTION, proposed=Severity.INFO, factor_ids=factors
    )
    high = supported_dossier(
        tag=RiskTag.INJECTION, proposed=Severity.CRITICAL, factor_ids=factors
    )
    llm = _AssessmentLLM(injection_critical_assessment())
    assert _judge([low], llm=llm).final_issues[0].severity is Severity.CRITICAL
    assert _judge([high], llm=llm).final_issues[0].severity is Severity.CRITICAL
```

Import `replace` from `dataclasses`, `RiskTag`, `strategies_for`, `policy_for`, and the new assessment models. Keep the existing `_dossier` builder for low-level gate cases; use `supported_dossier` when primary-tag policy selection matters.

Delete tests for `needs_more_evidence`, per-candidate LLM merge, semantic merge, and downgrade normalization.

- [ ] **Step 3: Run Judge tests and verify failure**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_council_judge.py -q
```

Expected: the action-oriented Judge fails the new gate and assessment expectations.

- [ ] **Step 4: Simplify active verdict models**

After updating imports, make the active verdict shape:

```python
@dataclass
class Verdict:
    candidate_id: str
    action: Literal["keep", "drop"]
    reason_code: str
    reason: str = ""
    resolved_severity: Severity | None = None
```

Remove `JudgeDecision`, `JudgeDecisions`, `requested_purpose`, merge target fields, and severity override fields from active Council models. Keep compatibility only in legacy-owned files if an active import search proves it necessary.

- [ ] **Step 5: Implement deterministic evidence gating**

In `council_judge.py`, retain orphan-request filtering and add:

```python
def _gate_candidate(
    findings: Sequence[tuple[EvidencePurpose, EvidenceFinding]],
) -> tuple[str, str] | None:
    if any(
        purpose == "counter"
        and finding.relation == "contradicts"
        and finding.strength == "direct"
        for purpose, finding in findings
    ):
        return "direct_counter_evidence", "直接反证足以排除候选"
    if not findings or all(finding.relation == "insufficient" for _, finding in findings):
        return "evidence_insufficient", "候选没有可用证据"
    if not any(
        purpose == "support" and finding.relation == "supports"
        for purpose, finding in findings
    ):
        return "no_supporting_evidence", "没有 support 证据支持候选主张"
    return None
```

- [ ] **Step 6: Implement constrained synthesis**

Determine the candidate primary tag from the registered strategy IDs attached to its valid requests. All strategies must resolve to one tag; otherwise use `GENERAL_REVIEW` and trace `ambiguous_candidate_primary_tag`.

Use this exact resolver shape:

```python
def _primary_tag(dossier: CandidateDossier) -> RiskTag:
    tags = {
        tag
        for request in dossier.requests
        if request.strategy_id in STRATEGIES_BY_ID
        for tag in STRATEGIES_BY_ID[request.strategy_id].tags
    }
    return next(iter(tags)) if len(tags) == 1 else RiskTag.GENERAL_REVIEW
```

Invoke structured output with `CandidateEvidenceAssessment`. The payload must include `primary_tag`, non-authoritative `task_tags`, `allowed_factors`, requests, and findings, and must not contain round or allowed-action fields. Reject a result whose `candidate_id` is not `C001`.

- [ ] **Step 7: Apply synthesis and severity resolution**

For a valid assessment:

```python
if assessment.claim_status == "refuted" or assessment.counter_effect == "complete":
    drop("synthesized_counter_evidence")
elif assessment.claim_status == "unresolved":
    drop("evidence_conflict_unresolved")
else:
    resolution = resolve_severity(primary_tag, assessment.severity_factors, findings_by_id)
    keep(resolved_severity=resolution.severity)
```

For missing/invalid synthesis, keep the gate-passed candidate with `policy_for(primary_tag).default_severity` and reason `severity_evidence_incomplete`. Map every kept dossier to `candidate.to_issue().model_copy(update={"severity": verdict.resolved_severity})`.

Delete fallback downgrades, `_normalized_severity`, `_deduplicate_survivors`, `_semantic_merge_survivors`, and aggregation imports.

- [ ] **Step 8: Run Judge and policy tests**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_council_judge.py tests/test_severity_policy.py tests/test_evidence_models.py -q
conda run -n codeguard --no-capture-output ruff check src/codeguard_agent/pipeline/council_judge.py src/codeguard_agent/models/council.py
```

Expected: all focused tests pass; no merge, round, or downgrade symbol remains in CouncilJudge.

- [ ] **Step 9: Commit clean Judge and model changes**

After verifying the staged diff excludes unrelated user work:

```powershell
git commit -m "refactor(pipeline): 以证据门槛和固定策略重写终审"
```

---

### Task 5: Remove evidence rounds from graph, configuration, and orchestration

**Files:**
- Modify: `services/agent/src/codeguard_agent/pipeline/graph.py`
- Modify: `services/agent/src/codeguard_agent/config.py`
- Modify: `services/agent/src/codeguard_agent/cli.py`
- Modify: `services/agent/src/codeguard_agent/pipeline/orchestrator.py`
- Modify: `services/agent/tests/test_graph_orchestration.py`
- Modify: `services/agent/tests/test_graph_phase5b.py`
- Modify: `services/agent/tests/test_config_settings.py`

**Interfaces:**
- Produces: fixed graph edge `council_judge -> END` and Settings without `max_evidence_rounds`.
- Consumes: one-pass `plan_evidence` and simplified `judge_candidates` signatures.

- [ ] **Step 1: Replace round-routing tests**

Delete `_route_after_council_judge` tests. Add:

```python
def test_review_graph_ends_after_council_judge():
    graph = G.build_review_graph(enable_summary=False, llm=None)
    drawable = graph.get_graph()
    edges = {(edge.source, edge.target) for edge in drawable.edges}
    assert ("council_judge", "__end__") in edges
    assert ("council_judge", "evidence_planner") not in edges


def test_review_state_excludes_evidence_round_configuration():
    annotations = G.ReviewState.__annotations__
    assert "evidence_round" not in annotations
    assert "max_evidence_rounds" not in annotations
```

Replace config tests with:

```python
def test_settings_no_longer_reads_max_evidence_rounds(monkeypatch):
    monkeypatch.setenv("CODEGUARD_MAX_EVIDENCE_ROUNDS", "2")
    settings = Settings.from_env()
    assert not hasattr(settings, "max_evidence_rounds")
```

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_graph_orchestration.py tests/test_graph_phase5b.py tests/test_config_settings.py -q
```

Expected: failures identify round state, conditional routing, and config plumbing.

- [ ] **Step 3: Remove round state and wire a direct END edge**

Remove `evidence_round`, `max_evidence_rounds`, `_route_after_council_judge`, and EvidenceAgent's round increment. Update Planner and Judge node calls to their new signatures. Replace conditional edges with:

```python
g.add_edge("council_judge", END)
```

EvidenceAgent still emits a single execution statistic through Council trace/metrics rather than a graph round counter.

- [ ] **Step 4: Remove configuration plumbing**

Delete `_evidence_rounds_env`, `Settings.max_evidence_rounds`, CLI forwarding, orchestrator storage/state injection, and any `DEFAULT_MAX_EVIDENCE_ROUNDS` constant. The environment variable becomes ignored because it is no longer read.

- [ ] **Step 5: Run graph, config, and pipeline tests**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_graph_orchestration.py tests/test_graph_phase5b.py tests/test_config_settings.py tests/test_pipeline.py -q
conda run -n codeguard --no-capture-output ruff check src/codeguard_agent/pipeline/graph.py src/codeguard_agent/config.py src/codeguard_agent/cli.py src/codeguard_agent/pipeline/orchestrator.py
```

Expected: all tests pass and `rg "max_evidence_rounds|needs_more_evidence" src tests` has no active-path result.

- [ ] **Step 6: Commit graph and configuration cleanup**

```powershell
git commit -m "refactor(pipeline): 移除证据补证回环"
```

---

### Task 6: Add evidence-gate and stable-severity metrics

**Files:**
- Modify: `services/agent/src/codeguard_agent/models/council.py`
- Modify: `services/agent/src/codeguard_agent/pipeline/council_metrics.py`
- Modify: `services/agent/evals/schema.py`
- Modify: `services/agent/evals/report.py`
- Modify: `services/agent/tests/test_council_metrics.py`
- Modify: `services/agent/tests/test_report_views.py`

**Interfaces:**
- Produces: `no_support_candidate_count`, `no_support_retained_count`, `severity_defaulted_count`, `critical_candidate_count`, `critical_policy_matched_count`, `critical_missing_factor_count`, and proposal-to-resolution transition counts.
- Consumes: final candidate IDs, simplified verdicts, and Judge trace events.

- [ ] **Step 1: Add failing metric tests**

Add a metric fixture containing one no-support drop, one defaulted WARNING, and one matched CRITICAL. Assert:

```python
assert stats.no_support_candidate_count == 1
assert stats.no_support_retained_count == 0
assert stats.direct_counter_retained_count == 0
assert stats.severity_defaulted_count == 1
assert stats.critical_candidate_count == 1
assert stats.critical_policy_matched_count == 1
assert stats.critical_missing_factor_count == 0
assert stats.severity_transitions == {"CRITICAL->WARNING": 1, "WARNING->CRITICAL": 1}
```

Update report-view tests to expect a section containing evidence-gate removal counts and severity policy outcomes. Keep `removed_by_aggregation` compatible at zero on the active path.

- [ ] **Step 2: Run metric tests and verify failure**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_council_metrics.py tests/test_report_views.py -q
```

Expected: missing CouncilRunStats/eval fields and report columns.

- [ ] **Step 3: Implement metrics from stable reason codes and trace**

Count drops by `Verdict.reason_code`, defaults by `severity_evidence_incomplete`, and critical policy matches from Judge trace event `severity_resolved` where `matched_rule` ends in `.critical`. Build transition keys from the candidate proposal and kept verdict's `resolved_severity`:

```python
transition = f"{candidate.severity_proposal.value}->{verdict.resolved_severity.value}"
```

Use final candidate IDs to guarantee retained counts are product-output aligned.

- [ ] **Step 4: Update eval schema and Markdown report**

Mirror the new CouncilRunStats fields in `evals/schema.py`. Add concise report rows for no-support retention, defaulted severities, critical matches, missing factors, and severity transitions. Do not remove legacy aggregation report parsing in this task.

- [ ] **Step 5: Run metric and report tests**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_council_metrics.py tests/test_report_views.py -q
```

Expected: all tests pass and zero denominators continue to render as unavailable rather than divide by zero.

- [ ] **Step 6: Commit metrics**

```powershell
git commit -m "feat(evals): 增加证据门槛与定级稳定性指标"
```

---

### Task 7: Update documentation and run end-to-end verification

**Files:**
- Modify: `.env.example`
- Modify: `AGENTS.md`
- Modify: `README.md` only if it mentions evidence rounds or old Judge behavior
- Modify: any focused tests still importing removed active models

**Interfaces:**
- Produces: documented fixed topology and a verified repository state.
- Consumes: all prior task outputs.

- [ ] **Step 1: Remove stale documentation and configuration**

Delete `CODEGUARD_MAX_EVIDENCE_ROUNDS` from `.env.example`. Update `AGENTS.md` diagrams and descriptions to state:

```text
EvidencePlanner 一次性规划完整 support/counter/severity 请求；EvidenceAgent 执行一次；
CouncilJudge 先执行证据门槛，再综合上下文证据，最后通过 primary RiskTag 固定策略定级。
Judge 不补证、不 merge，severity_proposal 仅用于诊断。
```

Keep the multi-tag routing invariant explicit. Update README only when search finds user-facing old behavior.

- [ ] **Step 2: Run stale-symbol searches**

Run:

```powershell
rg -n "needs_more_evidence|max_evidence_rounds|CODEGUARD_MAX_EVIDENCE_ROUNDS|merge_target_id|adjusted_severity|requested_purpose|severity_override" src tests evals README.md ../../AGENTS.md ../../.env.example
```

Expected: no active Council-path references. References in committed historical/legacy material are allowed only when clearly labeled historical.

- [ ] **Step 3: Run all deterministic Python tests**

Run from `services/agent`:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/ -q
```

Expected: all tests pass.

- [ ] **Step 4: Run static checks**

```powershell
conda run -n codeguard --no-capture-output ruff check src/ tests/ evals/
conda run -n codeguard --no-capture-output mypy src/
```

Expected: Ruff and mypy exit successfully.

- [ ] **Step 5: Run focused stability verification**

Run the deterministic severity policy suite repeatedly:

```powershell
1..5 | ForEach-Object {
  conda run -n codeguard --no-capture-output python -m pytest tests/test_severity_policy.py tests/test_council_judge.py -q
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

Expected: five identical passing runs. For configured real-LLM eval environments, additionally run the existing pipeline profile three times and record precision, recall, severity accuracy, no-support retention, and CRITICAL agreement; do not require network-backed evals when credentials are absent.

- [ ] **Step 6: Review the final diff against dirty-worktree ownership**

Run:

```powershell
git status --short
git diff --check
git diff --stat
```

Confirm that all pre-existing DESERIALIZATION and security-rule work remains present and that no unrelated `trace/` files are staged.

- [ ] **Step 7: Commit only implementation-owned remaining changes when separable**

Use a Conventional Commit message:

```powershell
git commit -m "docs: 同步证据门槛与固定定级流程"
```

If user-owned hunks remain inseparable in a file, leave them unstaged and report exactly which paths require the user's final commit decision.

---

## Final Acceptance Checklist

- [ ] Every candidate receives one-pass support, severity, and all required counter requests.
- [ ] Candidates with no support, all-insufficient evidence, direct counter-evidence, complete synthesized protection, or unresolved conflicts are not emitted.
- [ ] LLM synthesis cannot emit actions or severity labels.
- [ ] All RiskTags have deterministic default and maximum severity.
- [ ] Only the approved 13 primary tags can become CRITICAL.
- [ ] Every CRITICAL rule requires all cited, sufficiently strong factors.
- [ ] `severity_proposal` does not affect final severity.
- [ ] GENERAL_REVIEW never exceeds WARNING.
- [ ] Judge LLM failure defaults a gate-passed candidate and never produces CRITICAL.
- [ ] The active graph has no evidence loop and CouncilJudge has no merge behavior.
- [ ] Existing user modifications remain preserved and are not accidentally staged.
- [ ] Full pytest, Ruff, mypy, and five-run deterministic stability checks pass.
