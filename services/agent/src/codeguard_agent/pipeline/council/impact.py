"""ImpactAssessor：从 evidence findings 归纳影响因子状态。

确定性填充已知 factor，对需要语义归纳的部分调一次结构化 LLM。
LLM 不接触 Severity 枚举——它只输出 factor 状态。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from codeguard_agent.models.council import (
    EvidenceFinding,
    FactorStatus,
    ImpactAssessment,
    ImpactClass,
    ImpactFactor,
    ImpactFactorAssessment,
    ImpactRubric,
)
from codeguard_agent.pipeline.council.severity import FACTOR_INFO

logger = logging.getLogger("codeguard")

# factor → 从 finding 文本中确定性检测的关键词
_FACTOR_KEYWORDS: dict[ImpactFactor, tuple[str, ...]] = {
    ImpactFactor.RUNTIME_REACHABLE: ("可达", "reachable", "调用路径", "call path"),
    ImpactFactor.LOCAL_MAINTAINABILITY_ONLY: ("可读性", "readability", "复杂度", "complexity",
                                                "命名", "naming", "重复", "duplication"),
    ImpactFactor.AUTO_RECOVERABLE: ("重试", "retry", "自动恢复", "auto recover", "幂等", "idempotent"),
    ImpactFactor.OPERATOR_RECOVERABLE: ("手动", "manual", "运维", "operator", "人工"),
    ImpactFactor.IRREVERSIBLE: ("不可逆", "irreversible", "永久", "permanent"),
    ImpactFactor.PERSISTENT_STATE_CORRUPTION: ("持久化", "persist", "数据库", "database", "写入", "write"),
    ImpactFactor.EXTERNAL_SIDE_EFFECT: ("消息", "message", "事件", "event", "发布", "publish", "回调", "callback"),
    ImpactFactor.MULTI_ENTITY_BLAST_RADIUS: ("多租户", "multi tenant", "批量", "batch", "全部", "all"),
    ImpactFactor.INTEGRITY_LOSS: ("完整性", "integrity", "不一致", "inconsistent", "错误数据", "corrupt"),
    ImpactFactor.FINANCIAL_IMPACT: ("金额", "amount", "支付", "payment", "财务", "financial", "资金"),
    ImpactFactor.CONFIDENTIALITY_LOSS: ("泄露", "leak", "暴露", "expose", "敏感", "sensitive", "PII"),
    ImpactFactor.AVAILABILITY_LOSS: ("不可用", "unavailable", "宕机", "downtime", "崩溃", "crash"),
    ImpactFactor.AUTHORIZATION_BYPASS: ("越权", "authorization", "权限", "permission", "绕过", "bypass"),
    ImpactFactor.EXTERNAL_ACTOR_CONTROLLED: ("攻击者", "attacker", "外部输入", "external input", "用户输入", "user input"),
}


def _deterministic_factors(findings: Sequence[EvidenceFinding]) -> dict[ImpactFactor, ImpactFactorAssessment]:
    """从 findings 文本确定性检测已知 factor 状态。"""
    joined = " ".join(f.observation.lower() for f in findings)
    result: dict[ImpactFactor, ImpactFactorAssessment] = {}

    for factor, keywords in _FACTOR_KEYWORDS.items():
        matching = [f for f in findings if any(kw in f.observation.lower() for kw in keywords)]
        if matching:
            status = FactorStatus.PROVEN
            evidence_ids = tuple(f.evidence_id for f in matching)
        else:
            status = FactorStatus.UNKNOWN
            evidence_ids = ()
        result[factor] = ImpactFactorAssessment(
            factor=factor,
            status=status,
            evidence_ids=evidence_ids,
            reason=f"关键词匹配: {', '.join(keywords[:3])}" if matching else "无直接证据",
        )

    return result


def _determine_impact_class(factors: dict[ImpactFactor, ImpactFactorAssessment]) -> ImpactClass:
    """从已证明的 factors 确定 impact_class。"""
    if _is_proven(factors.get(ImpactFactor.LOCAL_MAINTAINABILITY_ONLY)) and not any(
        _is_proven(factors.get(f)) for f in (
            ImpactFactor.INTEGRITY_LOSS, ImpactFactor.AVAILABILITY_LOSS,
            ImpactFactor.CONFIDENTIALITY_LOSS, ImpactFactor.FINANCIAL_IMPACT,
        )
    ):
        return ImpactClass.MAINTAINABILITY

    if any(_is_proven(factors.get(f)) for f in (
        ImpactFactor.AUTHORIZATION_BYPASS, ImpactFactor.EXTERNAL_ACTOR_CONTROLLED,
        ImpactFactor.CONFIDENTIALITY_LOSS,
    )):
        return ImpactClass.SECURITY

    if _is_proven(factors.get(ImpactFactor.AVAILABILITY_LOSS)):
        return ImpactClass.AVAILABILITY

    return ImpactClass.RUNTIME_CORRECTNESS


def _is_proven(a: ImpactFactorAssessment | None) -> bool:
    return a is not None and a.status == FactorStatus.PROVEN and len(a.evidence_ids) > 0


def assess_impact(
    concern_id: str,
    findings: Sequence[EvidenceFinding],
    rubric: ImpactRubric,
    *,
    llm: Any = None,
) -> ImpactAssessment:
    """从 evidence findings 归纳影响因子状态。

    确定性填充 → LLM 补充语义判断 → 校验。
    不调 LLM 时只用确定性结果。
    """
    diagnostics: list[str] = []

    # 确定性填充
    factors = _deterministic_factors(findings)

    # 只保留 rubric 要求的 factors
    required = {f: factors.get(f, ImpactFactorAssessment(
        factor=f, status=FactorStatus.NOT_APPLICABLE,
    )) for f in rubric.required_factors}

    impact_class = _determine_impact_class(required)

    # LLM 补充（可选）：对 UNKNOWN 的关键 factor 做语义归纳
    if llm is not None:
        unknown_critical = [
            f for f in rubric.required_factors
            if required[f].status == FactorStatus.UNKNOWN
            and f in (
                ImpactFactor.RUNTIME_REACHABLE, ImpactFactor.INTEGRITY_LOSS,
                ImpactFactor.EXTERNAL_ACTOR_CONTROLLED, ImpactFactor.AUTHORIZATION_BYPASS,
                ImpactFactor.CROSS_TENANT_SCOPE, ImpactFactor.FINANCIAL_IMPACT,
                ImpactFactor.PERSISTENT_STATE_CORRUPTION, ImpactFactor.AVAILABILITY_LOSS,
            )
        ]
        if unknown_critical:
            try:
                llm_factors = _llm_assess_factors(concern_id, findings, unknown_critical, llm)
                for fa in llm_factors:
                    if fa.factor in required:
                        required[fa.factor] = fa
            except Exception:
                logger.warning("ImpactAssessor LLM failed, using deterministic only", exc_info=True)
                diagnostics.append("llm_assessment_failed")

    return ImpactAssessment(
        concern_id=concern_id,
        impact_class=impact_class,
        factors=tuple(required.values()),
        diagnostics=tuple(diagnostics),
    )


def _llm_assess_factors(
    concern_id: str,
    findings: Sequence[EvidenceFinding],
    factors: list[ImpactFactor],
    llm: Any,
) -> list[ImpactFactorAssessment]:
    """调 LLM 对关键 UNKNOWN factor 做语义归纳。"""
    from codeguard_agent.llm.client import invoke_with_retry
    from pydantic import BaseModel, Field

    class _FactorAssessmentOutput(BaseModel):
        factor: str
        status: str  # proven / disproven / unknown
        evidence_ids: list[str] = Field(default_factory=list)
        reason: str = ""

    class _AssessmentOutput(BaseModel):
        assessments: list[_FactorAssessmentOutput] = Field(default_factory=list)

    findings_text = "\n".join(
        f"[{f.evidence_id}] ({f.relation}) {f.observation}"
        for f in findings[:20]  # limit to avoid token overflow
    )
    factor_names = ", ".join(f.value for f in factors)
    factor_descs = "\n".join(f"- {f.value}: {FACTOR_INFO.get(f, '')}" for f in factors)

    prompt = f"""根据以下 evidence findings，判断每个因子的状态（proven / disproven / unknown）。

Evidence findings:
{findings_text}

需要评估的因子：
{factor_descs}

规则：
- proven: 有明确证据支持该因子成立
- disproven: 有明确证据排除该因子
- unknown: 证据不足以判断
- 每个 proven/disproven 必须引用至少一个 evidence_id
- 不要猜测，不要从命名推断"""

    try:
        structured = llm.with_structured_output(_AssessmentOutput, method="function_calling")
        result = invoke_with_retry(structured, [("user", prompt)], max_retries=1)
        if result is None or not isinstance(result, _AssessmentOutput):
            return []

        out: list[ImpactFactorAssessment] = []
        valid_ids = {f.evidence_id for f in findings}
        for a in result.assessments:
            try:
                factor = ImpactFactor(a.factor)
            except ValueError:
                continue
            try:
                status = FactorStatus(a.status)
            except ValueError:
                status = FactorStatus.UNKNOWN

            valid_evidence = [eid for eid in a.evidence_ids if eid in valid_ids]
            if status in (FactorStatus.PROVEN, FactorStatus.DISPROVEN) and not valid_evidence:
                status = FactorStatus.UNKNOWN

            out.append(ImpactFactorAssessment(
                factor=factor, status=status,
                evidence_ids=tuple(valid_evidence),
                reason=a.reason,
            ))
        return out
    except Exception:
        logger.warning("ImpactAssessor LLM call failed", exc_info=True)
        return []


def assess_impact_fallback(concern_id: str = "") -> ImpactAssessment:
    """完全失败时的最小 fallback assessment。"""
    return ImpactAssessment(
        concern_id=concern_id,
        impact_class=ImpactClass.RUNTIME_CORRECTNESS,
        factors=(ImpactFactorAssessment(
            factor=ImpactFactor.RUNTIME_REACHABLE,
            status=FactorStatus.UNKNOWN,
        ),),
        diagnostics=("fallback_assessment",),
    )
