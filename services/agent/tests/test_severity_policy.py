"""Evidence-gated deterministic severity policy tests."""

from codeguard_agent.models.council import EvidenceFinding
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.council.severity import (
    SeverityFactorAssessment,
    factor_is_proven,
    policy_for,
    resolve_severity,
)

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


# -- helpers --


def _finding(
    evidence_id: str,
    *,
    source: str = "task_patch",
    strength: str = "direct",
    relation: str = "supports",
) -> EvidenceFinding:
    return EvidenceFinding(
        evidence_id=evidence_id,
        source=source,
        observation=f"observation for {evidence_id}",
        relation=relation,
        strength=strength,
    )


def _proven_factors(
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
        findings[evidence_id] = _finding(
            evidence_id,
            source=f"tool:source-{index}",
        )
    return assessments, findings


# -- CRITICAL requirements --


def test_injection_requires_every_critical_factor():
    policy = policy_for(RiskTag.INJECTION)
    assessments, findings = _proven_factors(policy.critical_requires)
    result = resolve_severity(RiskTag.INJECTION, assessments, findings)
    assert result.severity is Severity.CRITICAL
    assert result.missing_critical_factors == ()


def test_one_missing_critical_factor_falls_back_to_warning():
    policy = policy_for(RiskTag.INJECTION)
    assessments, findings = _proven_factors(policy.critical_requires[:-1])
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
    findings = {"E1": _finding("E1", source="task_patch", strength="contextual")}
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
        "E1": _finding("E1", source="task_patch", strength="contextual"),
        "E2": _finding("E2", source="tool:get_file_content", strength="contextual"),
    }
    assert factor_is_proven(assessment, findings)


def test_duplicate_evidence_id_uses_all_findings_independent_of_order():
    assessment = SeverityFactorAssessment(
        factor_id="untrusted_input",
        status="proven",
        evidence_ids=["E1"],
    )
    supporting = _finding("E1", relation="supports", strength="direct")
    contradicting = _finding("E1", relation="contradicts", strength="contextual")

    assert factor_is_proven(assessment, {"E1": [supporting, contradicting]})
    assert factor_is_proven(assessment, {"E1": [contradicting, supporting]})


# -- Factor proof edge cases --


def test_unknown_status_never_proven():
    assessment = SeverityFactorAssessment(
        factor_id="untrusted_input",
        status="unknown",
        evidence_ids=["E1"],
    )
    assert not factor_is_proven(assessment, {"E1": _finding("E1")})


def test_disproven_status_never_proven():
    assessment = SeverityFactorAssessment(
        factor_id="untrusted_input",
        status="disproven",
        evidence_ids=["E1"],
    )
    assert not factor_is_proven(assessment, {"E1": _finding("E1")})


def test_uncited_evidence_ignored():
    assessment = SeverityFactorAssessment(
        factor_id="untrusted_input",
        status="proven",
        evidence_ids=["E1"],
    )
    assert not factor_is_proven(assessment, {})


def test_non_support_relation_does_not_prove():
    assessment = SeverityFactorAssessment(
        factor_id="untrusted_input",
        status="proven",
        evidence_ids=["E1"],
    )
    findings = {"E1": _finding("E1", relation="contradicts")}
    assert not factor_is_proven(assessment, findings)


# -- Non-CRITICAL tags --


def test_non_critical_tag_returns_default():
    for tag in (
        RiskTag.WEB_SECURITY_CONFIG,
        RiskTag.COMPLEXITY_CONTROL_FLOW,
        RiskTag.OBSERVABILITY_TESTABILITY,
        RiskTag.GENERAL_REVIEW,
    ):
        result = resolve_severity(tag, [], {})
        assert result.severity is EXPECTED_LEVELS[tag][0]
        assert result.missing_critical_factors == ()
        assert result.matched_rule == f"{tag.value.lower()}.default"


def test_general_review_never_critical():
    result = resolve_severity(RiskTag.GENERAL_REVIEW, [], {})
    assert result.severity is not Severity.CRITICAL


# -- Maximum ceiling enforcement --


def test_non_critical_tags_respect_maximum():
    """Tags with WARNING ceiling never produce CRITICAL even with proven factors."""
    for tag in (RiskTag.WEB_SECURITY_CONFIG, RiskTag.ERROR_HANDLING):
        policy = policy_for(tag)
        assert policy.maximum_severity is not Severity.CRITICAL


# -- All 13 critical-enable tags have required factors --


CRITICAL_ENABLED_TAGS = {
    RiskTag.AUTHORIZATION,
    RiskTag.AUTHENTICATION_SESSION,
    RiskTag.INJECTION,
    RiskTag.SQL_DATA_ACCESS,
    RiskTag.FILE_PATH_IO,
    RiskTag.SSRF_OUTBOUND,
    RiskTag.CONFIG_SECURITY,
    RiskTag.DATA_EXPOSURE,
    RiskTag.DESERIALIZATION,
    RiskTag.TRANSACTION_ATOMICITY,
    RiskTag.CONCURRENCY_CONSISTENCY,
    RiskTag.IDEMPOTENCY_RETRY,
    RiskTag.MESSAGE_DELIVERY,
}


def test_all_13_critical_tags_have_required_factors():
    for tag in CRITICAL_ENABLED_TAGS:
        policy = policy_for(tag)
        assert policy.critical_requires, f"{tag.value} must have critical_requires"
        assert len(policy.critical_requires) > 0


def test_every_critical_policy_requires_all_of_its_factors():
    for tag in CRITICAL_ENABLED_TAGS:
        policy = policy_for(tag)
        assessments, findings = _proven_factors(policy.critical_requires)
        assert resolve_severity(tag, assessments, findings).severity is Severity.CRITICAL

        missing_assessments, missing_findings = _proven_factors(
            policy.critical_requires[:-1]
        )
        resolution = resolve_severity(tag, missing_assessments, missing_findings)
        assert resolution.severity is policy.default_severity
        assert resolution.missing_critical_factors == (policy.critical_requires[-1],)


def test_critical_tags_have_factor_descriptions():
    for tag in CRITICAL_ENABLED_TAGS:
        policy = policy_for(tag)
        factor_ids = {f.id for f in policy.factors}
        assert set(policy.critical_requires) <= factor_ids
        for f in policy.factors:
            assert f.description, f"{tag.value}.{f.id} missing description"


# -- Resolution metadata --


def test_critical_resolution_has_correct_matched_rule():
    policy = policy_for(RiskTag.DESERIALIZATION)
    assessments, findings = _proven_factors(policy.critical_requires)
    result = resolve_severity(RiskTag.DESERIALIZATION, assessments, findings)
    assert result.matched_rule == "deserialization.critical"


def test_default_resolution_has_correct_matched_rule():
    result = resolve_severity(RiskTag.DESERIALIZATION, [], {})
    assert result.matched_rule == "deserialization.default"


def test_resolution_includes_proven_and_evidence_ids():
    policy = policy_for(RiskTag.AUTHORIZATION)
    assessments, findings = _proven_factors(policy.critical_requires)
    result = resolve_severity(RiskTag.AUTHORIZATION, assessments, findings)
    assert set(result.proven_factors) == set(policy.critical_requires)
    assert len(result.evidence_ids) == len(policy.critical_requires)
