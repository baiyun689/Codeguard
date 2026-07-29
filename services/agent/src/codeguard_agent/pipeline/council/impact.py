"""ImpactAssessor：从 evidence findings 归纳影响因子状态。

确定性填充已知 factor，对需要语义归纳的部分调一次结构化 LLM。
LLM 不接触 Severity 枚举——它只输出 factor 状态。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path
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
_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"

# factor → 从 finding 文本中确定性检测的关键词
_FACTOR_KEYWORDS: dict[ImpactFactor, tuple[str, ...]] = {
    ImpactFactor.RUNTIME_REACHABLE: ("可达", "reachable", "调用路径", "call path"),
    ImpactFactor.LOCAL_MAINTAINABILITY_ONLY: ("可读性", "readability", "复杂度", "complexity",
                                                "命名", "naming", "重复", "duplication"),
    ImpactFactor.AUTO_RECOVERABLE: (
        "可自动恢复", "自动重试可恢复", "auto recoverable",
    ),
    ImpactFactor.OPERATOR_RECOVERABLE: ("手动", "manual", "运维", "operator", "人工"),
    ImpactFactor.IRREVERSIBLE: ("不可逆", "irreversible", "永久", "permanent"),
    ImpactFactor.PERSISTENT_STATE_CORRUPTION: (
        "持久状态损坏", "持久化错误", "错误写入", "数据损坏",
        "persistent state corruption", "corrupt persisted", "incorrectly persisted",
    ),
    ImpactFactor.EXTERNAL_SIDE_EFFECT: ("消息", "message", "事件", "event", "发布", "publish", "回调", "callback"),
    ImpactFactor.MULTI_ENTITY_BLAST_RADIUS: (
        "多租户", "跨租户", "multi tenant", "cross tenant",
        "批量实体", "多个订单", "多个用户", "multiple entities",
    ),
    ImpactFactor.INTEGRITY_LOSS: ("完整性", "integrity", "不一致", "inconsistent", "错误数据", "corrupt"),
    ImpactFactor.FINANCIAL_IMPACT: ("金额", "amount", "支付", "payment", "财务", "financial", "资金"),
    ImpactFactor.CONFIDENTIALITY_LOSS: ("泄露", "leak", "暴露", "expose", "敏感", "sensitive", "PII"),
    ImpactFactor.AVAILABILITY_LOSS: ("不可用", "unavailable", "宕机", "downtime", "崩溃", "crash"),
    ImpactFactor.AUTHORIZATION_BYPASS: (
        "越权", "绕过授权", "缺少授权校验",
        "authorization bypass", "missing authorization",
    ),
    ImpactFactor.EXTERNAL_ACTOR_CONTROLLED: ("攻击者", "attacker", "外部输入", "external input", "用户输入", "user input"),
}


def _contains_asserted_keyword(text: str, keyword: str) -> bool:
    """只接受未被局部否定的关键词，避免“不可达/无法恢复”反向证明因子。"""
    lowered = text.lower()
    needle = keyword.lower()
    start = 0
    while (index := lowered.find(needle, start)) >= 0:
        prefix = lowered[max(0, index - 12):index]
        if not (
            re.search(r"(?:不|未|无|无法|不能|并非|not|never|without)\s*$", prefix)
            or prefix.endswith("un")
        ):
            return True
        start = index + len(needle)
    return False


def _deterministic_factors(
    findings: Sequence[EvidenceFinding],
) -> dict[ImpactFactor, ImpactFactorAssessment]:
    """从 findings 文本确定性检测已知 factor 状态。"""
    result: dict[ImpactFactor, ImpactFactorAssessment] = {}
    supporting = [
        finding
        for finding in findings
        if finding.relation == "supports" and finding.observation.strip()
    ]

    for factor, keywords in _FACTOR_KEYWORDS.items():
        matching = [
            finding
            for finding in supporting
            if any(
                _contains_asserted_keyword(finding.observation, keyword)
                for keyword in keywords
            )
        ]
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
    aligned_findings = [
        finding
        for finding in findings
        if finding.concern_id in (None, concern_id)
    ]
    factors = _deterministic_factors(aligned_findings)

    # 只保留 rubric 要求的 factors
    required = {f: factors.get(f, ImpactFactorAssessment(
        factor=f, status=FactorStatus.NOT_APPLICABLE,
    )) for f in rubric.required_factors}

    impact_class = _determine_impact_class(required)

    # LLM 补充（可选）：对 UNKNOWN 的关键 factor 做语义归纳
    if llm is not None:
        unknown_critical: list[ImpactFactor] = [
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
                llm_factors = _llm_assess_factors(
                    concern_id, aligned_findings, unknown_critical, llm,
                )
                for fa in llm_factors:
                    if fa.factor in required:
                        required[fa.factor] = fa
            except Exception:
                logger.warning("ImpactAssessor LLM failed, using deterministic only", exc_info=True)
                diagnostics.append("llm_assessment_failed")

    impact_class = _determine_impact_class(required)
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
    factor_descs = "\n".join(f"- {f.value}: {FACTOR_INFO.get(f, '')}" for f in factors)

    prompt = (
        "Evidence findings:\n"
        f"{findings_text}\n\n"
        "需要评估的因子：\n"
        f"{factor_descs}"
    )

    try:
        structured = llm.with_structured_output(_AssessmentOutput, method="function_calling")
        result = invoke_with_retry(
            structured,
            [
                (
                    "system",
                    (_PROMPT_DIR / "impact-factor-assessor.txt").read_text(
                        encoding="utf-8",
                    ),
                ),
                ("user", prompt),
            ],
            max_retries=1,
        )
        if result is None or not isinstance(result, _AssessmentOutput):
            return []

        out: list[ImpactFactorAssessment] = []
        valid_ids = {
            f.evidence_id
            for f in findings
            if f.relation != "insufficient"
        }
        supporting_ids = {
            f.evidence_id
            for f in findings
            if f.relation == "supports"
        }
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
            if (
                status == FactorStatus.PROVEN
                and not any(eid in supporting_ids for eid in valid_evidence)
            ):
                status = FactorStatus.UNKNOWN
            if status in (FactorStatus.UNKNOWN, FactorStatus.NOT_APPLICABLE):
                valid_evidence = []

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
