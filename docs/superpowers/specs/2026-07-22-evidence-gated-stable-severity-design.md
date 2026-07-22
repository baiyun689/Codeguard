# Evidence-Gated Stable Severity Design

## 1. Goal

The ReviewCouncil evidence chain will have exactly two product responsibilities:

1. Remove candidates that have no evidence supporting the reported problem, or that are defeated by valid counter-evidence.
2. Assign a stable severity from the candidate's primary `RiskTag` and cited evidence, especially for `CRITICAL` issues.

The change does not attempt to build an open-ended evidence loop or a general argumentation framework. It removes `needs_more_evidence` and CouncilJudge-owned merge behavior.

Success means:

- candidates with no valid support are never emitted as `Issue` objects;
- direct counter-evidence is never ignored;
- the same confirmed issue receives the same severity across repeated reviews when the evidence is equivalent;
- `CRITICAL` is only produced by an explicit allowlisted policy whose required evidence factors are proven;
- hunk-level multi-tag routing signals cannot independently raise a candidate's severity.

## 2. Scope and invariants

The graph becomes a fixed one-pass chain:

```text
CouncilCoordinator -> EvidencePlanner -> EvidenceAgent -> CouncilJudge -> END
```

The following invariants apply:

- `RiskProfile.tag_scores` remains a multi-tag hunk/task routing prior. A task tag does not prove that a candidate has that risk.
- Every candidate is resolved to one primary evidence `RiskTag`. That tag selects the severity policy.
- Other positive task tags remain available as context for evidence collection and synthesis, but cannot satisfy severity conditions by themselves.
- A candidate should describe one root problem. Independent problems remain independent candidates.
- `CandidateIssue.severity_proposal` remains for trace and comparison only. It has no authority over the final severity.
- Product `Issue` fields remain unchanged.
- Python owns evidence interpretation and adjudication. Java continues to provide facts and guardrails only.

## 3. Evidence planning

`EvidencePurpose` and `EvidenceRequest.question` remain. They already express why evidence is requested and what the strategy investigates.

For every valid candidate, the initial and only evidence plan must cover:

- at least one `support` strategy;
- one `severity` strategy;
- every registered counter strategy required by the primary tag, including both local and upstream counter strategies when present.

The existing conditional support gate is removed. Support is mandatory because publication now requires supporting evidence.

The per-candidate request cap is raised from three to the maximum required by the current static registry. With the current registry this is four requests. The cap remains deterministic and is tested against the registry so a future strategy addition cannot be silently truncated.

Planner follow-up behavior is deleted. The Planner creates a complete bounded plan in one pass; it does not consume a Judge request for another purpose.

## 4. CouncilJudge interface and internal flow

CouncilJudge remains one deep module with the existing external seam:

```python
judge_candidates(assembly, judge_llm, structured_method, max_retries) -> JudgeBatch
```

Round-related parameters are removed. Internally the Judge performs three steps.

### 4.1 Evidence gate

The evidence gate is deterministic and runs before the Judge LLM.

A finding is valid only when its request is correctly bound to the candidate and registered strategy. Orphan findings and mismatched requests do not participate.

The gate applies these rules in order:

1. A `counter` request containing `relation=contradicts` and `strength=direct` drops the candidate as `direct_counter_evidence`.
2. A candidate with no usable findings, or whose usable findings are all `insufficient`, is dropped as `evidence_insufficient`.
3. A candidate with no `support` request finding whose relation is `supports` is dropped as `no_supporting_evidence`.
4. Every other candidate proceeds to evidence synthesis.

A contextual support finding is sufficient to enter synthesis. The gate does not require direct support, because doing so would cause a large recall loss. Tool failure, truncation, and unavailable context remain `insufficient` and therefore cannot pass the gate by themselves.

### 4.2 Evidence synthesis

The existing Judge LLM becomes a constrained evidence synthesizer. It does not choose `keep`, `drop`, `downgrade`, `merge`, `needs_more_evidence`, or a severity.

It receives:

- the candidate and task patch;
- the resolved primary tag;
- all positive hunk/task tags as non-authoritative context;
- requests grouped by `support`, `counter`, and `severity`;
- valid findings with evidence IDs, source, relation, strength, observation, and limitation;
- the factor IDs allowed by the primary tag's severity policy.

It returns:

```python
class SeverityFactorAssessment(BaseModel):
    factor_id: str
    status: Literal["proven", "disproven", "unknown"]
    evidence_ids: list[str]
    reason: str


class CandidateEvidenceAssessment(BaseModel):
    candidate_id: str
    claim_status: Literal["supported", "refuted", "unresolved"]
    counter_effect: Literal["none", "partial", "complete", "unknown"]
    severity_factors: list[SeverityFactorAssessment]
    conflicts: list[str]
    reason: str
```

Synthesis rules:

- every `proven` or `disproven` factor must cite evidence IDs from the current candidate;
- an `insufficient` finding cannot prove or disprove a factor;
- not finding a guard does not prove that a guard is absent;
- hunk tags cannot prove a factor;
- only policy-declared factor IDs may be returned;
- the synthesizer cannot create a new vulnerability claim;
- partial mitigation must be reported as `counter_effect=partial`, not as a complete refutation;
- unresolved material conflicts produce `claim_status=unresolved`.

Post-synthesis adjudication is deterministic:

- `claim_status=refuted` or `counter_effect=complete` drops the candidate;
- `claim_status=unresolved` drops the candidate as `evidence_conflict_unresolved`;
- `claim_status=supported` continues to severity resolution;
- if the synthesizer is unavailable or returns invalid structured output, a candidate that already passed the evidence gate remains publishable and receives the policy default severity.

### 4.3 Severity policy resolution

Severity resolution validates factor citations and applies a static policy. Validation is part of the resolver rather than a separate shallow module.

```python
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
```

The resolver enforces:

- cited evidence IDs must belong to the current candidate;
- `insufficient` findings never satisfy a factor;
- a critical factor is accepted only with one direct supporting finding or at least two contextual supporting findings from distinct sources;
- `unknown` never satisfies a critical requirement;
- every `critical_requires` factor must be proven before returning `CRITICAL`;
- the primary policy's `maximum_severity` is a hard ceiling;
- hunk/task tags and `severity_proposal` never raise or lower the result;
- an absent, invalid, or failed synthesis uses `default_severity` and can never produce `CRITICAL`.

## 5. Severity registry

All 23 concrete tags plus `GENERAL_REVIEW` receive a deterministic default and ceiling in this change.

| RiskTag | Default | Maximum |
|---|---|---|
| AUTHORIZATION | WARNING | CRITICAL |
| AUTHENTICATION_SESSION | WARNING | CRITICAL |
| WEB_SECURITY_CONFIG | WARNING | WARNING |
| INPUT_VALIDATION | WARNING | WARNING |
| INJECTION | WARNING | CRITICAL |
| SQL_DATA_ACCESS | WARNING | CRITICAL |
| FILE_PATH_IO | WARNING | CRITICAL |
| SSRF_OUTBOUND | WARNING | CRITICAL |
| CONFIG_SECURITY | WARNING | CRITICAL |
| DATA_EXPOSURE | WARNING | CRITICAL |
| DESERIALIZATION | WARNING | CRITICAL |
| TRANSACTION_ATOMICITY | WARNING | CRITICAL |
| CONCURRENCY_CONSISTENCY | WARNING | CRITICAL |
| IDEMPOTENCY_RETRY | WARNING | CRITICAL |
| CACHE_CONSISTENCY | WARNING | WARNING |
| MESSAGE_DELIVERY | WARNING | CRITICAL |
| ERROR_HANDLING | WARNING | WARNING |
| NULL_STATE_SAFETY | WARNING | WARNING |
| RESOURCE_LIFECYCLE | WARNING | WARNING |
| API_CONTRACT | WARNING | WARNING |
| PERFORMANCE | WARNING | WARNING |
| COMPLEXITY_CONTROL_FLOW | INFO | INFO |
| DUPLICATION_DESIGN | INFO | INFO |
| OBSERVABILITY_TESTABILITY | INFO | INFO |
| GENERAL_REVIEW | WARNING | WARNING |

The CRITICAL allowlist and required factors are:

| Primary RiskTag | All required CRITICAL factors |
|---|---|
| AUTHORIZATION | `untrusted_actor_reachable`, `effective_authorization_absent`, `high_value_cross_boundary_impact` |
| AUTHENTICATION_SESSION | `credential_or_session_control`, `effective_session_validation_absent`, `account_takeover_or_broad_scope` |
| INJECTION | `untrusted_input`, `dangerous_interpreter_sink`, `effective_mitigation_absent`, `high_impact_execution_or_data` |
| SQL_DATA_ACCESS | `dangerous_data_operation`, `scope_constraint_absent`, `operation_reachable`, `broad_irreversible_or_cross_tenant_impact` |
| FILE_PATH_IO | `untrusted_path`, `filesystem_sink_reached`, `effective_confinement_absent`, `sensitive_read_or_arbitrary_write` |
| SSRF_OUTBOUND | `untrusted_destination`, `outbound_sink_reached`, `effective_network_restriction_absent`, `credential_or_privileged_internal_impact` |
| CONFIG_SECURITY | `production_reachable`, `security_control_disabled_or_secret_exposed`, `broad_privileged_impact` |
| DATA_EXPOSURE | `sensitive_data_flow`, `unauthorized_audience_reachable`, `effective_redaction_or_access_control_absent`, `broad_or_high_value_scope` |
| DESERIALIZATION | `untrusted_payload`, `unsafe_deserializer_reached`, `effective_type_restriction_absent`, `code_execution_or_privileged_impact` |
| TRANSACTION_ATOMICITY | `critical_multi_step_state_change`, `atomicity_gap`, `failure_or_interleaving_reachable`, `irreversible_financial_or_data_impact` |
| CONCURRENCY_CONSISTENCY | `shared_critical_state`, `race_reachable`, `effective_synchronization_absent`, `financial_or_data_integrity_impact` |
| IDEMPOTENCY_RETRY | `duplicate_execution_reachable`, `effective_idempotency_protection_absent`, `irreversible_high_value_action` |
| MESSAGE_DELIVERY | `critical_event`, `loss_duplicate_or_order_failure_reachable`, `effective_delivery_protection_absent`, `irreversible_high_impact` |

Factor definitions live beside their policies and include a plain-language description used by the synthesis prompt. Task tags may guide the synthesizer toward relevant evidence, but do not count as factor evidence.

Detailed per-tag WARNING/INFO predicates are outside this change. Non-critical confirmed candidates use the table's stable default. This deliberately prioritizes stable CRITICAL classification and bounded delivery scope.

## 6. Model and graph changes

`Verdict` is reduced to actual adjudication outcomes:

```python
@dataclass
class Verdict:
    candidate_id: str
    action: Literal["keep", "drop"]
    reason_code: str
    reason: str = ""
    resolved_severity: Severity | None = None
```

The following fields and actions are removed from the active Council path:

- `merge`;
- `needs_more_evidence`;
- `requested_purpose`;
- `suggested_target_id` / `merge_target_id`;
- `severity_override` / `adjusted_severity`;
- evidence round state and routing;
- `CODEGUARD_MAX_EVIDENCE_ROUNDS`.

The existing candidate reducer remains the Council's deduplication mechanism. CouncilJudge's `_deduplicate_survivors`, semantic merge LLM call, and aggregation verdicts are removed. The historical `AggregationStage` and its prompts remain because legacy code and dedicated tests still use them.

`removed_by_aggregation` may remain as a compatibility field with value zero on the active Council path; removing historical reporting fields is not required to achieve this design.

## 7. Prompt contracts

### Council Judge prompt

`council-judge.txt` is rewritten for `CandidateEvidenceAssessment`.

It must:

- prohibit action and severity selection;
- prohibit tools, new claims, merge, and additional evidence requests;
- require evidence citations for factor states;
- distinguish no observed guard from proven absence of a guard;
- treat task tags as context only;
- limit factor IDs to the supplied policy definitions;
- report partial mitigation and unresolved conflicts explicitly.

### Evidence analysis prompt

`evidence-analysis.txt` is tightened without changing its output model:

- relation is always relative to the candidate claim, not the request purpose;
- purpose selects the investigation lens only;
- severity findings describe reachability, preconditions, impact, and scope, but never recommend a severity label;
- observations should be atomic and cite the supplied fact;
- failure, truncation, and missing context remain `insufficient`.

### Candidate tag classifier prompt

The classifier is told that task tags are multi-tag priors and it must select one candidate primary tag from candidate semantics. Low-confidence or ambiguous classification returns `GENERAL_REVIEW`.

### Discoverer prompts

Discoverers continue to emit `severity_proposal` for schema compatibility, but the prompts describe it as provisional and prohibit attention-seeking inflation. Final severity belongs to the evidence policy.

Prompt contract tests are updated with the structured model changes.

## 8. Failure behavior

| Failure | Behavior |
|---|---|
| Candidate/task binding invalid | Drop with explicit binding reason |
| No support finding | Drop as `no_supporting_evidence` |
| All usable findings insufficient | Drop as `evidence_insufficient` |
| Direct counter contradiction | Drop as `direct_counter_evidence` |
| Contextual conflict resolved as complete protection | Drop as refuted |
| Contextual conflict unresolved | Drop as `evidence_conflict_unresolved` |
| Judge LLM unavailable/invalid after gate passes | Keep with policy default severity |
| Factor contains unknown evidence ID | Ignore factor; record trace |
| Critical factor lacks required proof strength | Treat factor as unknown |
| Primary tag is GENERAL_REVIEW | Maximum WARNING |

Tool/context failure and genuine absence of support both suppress product output in the precision-oriented default path, but retain distinct trace reason codes so evals can distinguish review uncertainty from a false candidate.

## 9. Metrics and verification

Council metrics add or replace measurements needed by the two product goals:

- `no_support_candidate_count`;
- `no_support_retained_count` (must be zero);
- `direct_counter_retained_count` (must be zero);
- `severity_defaulted_count`;
- `critical_candidate_count`;
- `critical_policy_matched_count`;
- `critical_missing_factor_count`;
- severity proposal-to-resolution transition counts.

Deterministic tests cover:

- mandatory support planning for every tag;
- complete local/upstream counter planning without follow-up;
- each evidence-gate branch;
- invalid and cross-candidate evidence citations;
- contextual corroboration source requirements;
- all default and maximum values in the registry;
- every CRITICAL policy's complete and one-missing-factor cases;
- `severity_proposal` having no effect on resolution;
- GENERAL_REVIEW never becoming CRITICAL;
- LLM failure defaulting rather than producing CRITICAL;
- graph topology containing no Judge loop;
- CouncilJudge containing no merge behavior.

Eval verification uses existing precision, recall, and severity accuracy plus repeated-run stability:

- no-evidence candidate retention target: `0%`;
- direct-counter candidate retention target: `0%`;
- deterministic severity resolver agreement: `100%`;
- repeated severity agreement on identical expected matches: target `100%` for CRITICAL fixtures;
- CRITICAL precision and recall reported separately so stability is not achieved by suppressing every CRITICAL issue.

## 10. Documentation and compatibility

The implementation updates:

- `AGENTS.md` topology, Judge responsibilities, and evidence round documentation;
- `README.md` only where public behavior or configuration changes are described;
- `.env.example` to remove `CODEGUARD_MAX_EVIDENCE_ROUNDS`;
- internal council/eval schemas and reports for changed verdict and metrics fields;
- tests and prompt contracts affected by removed actions.

No product `Issue` schema field changes are required.
