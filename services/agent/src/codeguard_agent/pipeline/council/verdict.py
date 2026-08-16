"""裁决模块:确定性门控 + LLM 终审 + 组内合并(ADR-046)。

门控依赖关系分析产出;门控本身零 LLM;终审基于关系三元输出统一裁决;
组内合并由后续 Task 补入本模块。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Sequence

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.council import CandidateDirectAssessment, FactRelation
from codeguard_agent.pipeline.evidence.planner import CandidateDossier

logger = logging.getLogger("codeguard")

_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"


def gate_candidate(relations: Sequence[FactRelation]) -> tuple[str, str] | None:
    """三条确定性证据门控(零 LLM 成本淘汰)。返回 (reason_code, reason) 表示应 drop。"""
    if any(
        item.relation == "contradicts" and item.strength == "direct"
        for item in relations
    ):
        return "direct_counter_evidence", "直接反证足以排除候选"
    if not relations or all(
        item.relation == "insufficient" for item in relations
    ):
        return "evidence_insufficient", "候选没有可用证据"
    if not any(item.relation == "supports" for item in relations):
        return "no_supporting_evidence", "没有 support 证据支持候选主张"
    return None


def synthesize_verdict(
    dossier: CandidateDossier,
    relations: Sequence[FactRelation],
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
) -> CandidateDirectAssessment | None:
    """终审:基于关系三元输出统一裁决。失败/None 返回 None,由调用方确定性保留。

    裁决模型固定用别名 C001 指向候选;校验通过后重映射回 dossier 真实候选 id,
    调用方拿到的结果可直接落 State。
    """
    if judge_llm is None:
        return None
    try:
        structured = judge_llm.with_structured_output(
            CandidateDirectAssessment,
            method=structured_method,
        )
        system_prompt = (_PROMPT_DIR / "council-judge.txt").read_text(encoding="utf-8")
        result = invoke_with_retry(
            structured,
            [
                ("system", system_prompt),
                ("user", _verdict_payload(dossier, relations)),
            ],
            max_retries=max_retries,
        )
        if result is None:
            return None
        if not isinstance(result, CandidateDirectAssessment):
            result = CandidateDirectAssessment.model_validate(result)
        if result.candidate_id != "C001":
            logger.warning("verdict returned unexpected candidate_id: %s", result.candidate_id)
            return None
        return result.model_copy(update={"candidate_id": dossier.candidate.id})
    except Exception:
        logger.warning("verdict LLM synthesis failed", exc_info=True)
        return None


def _verdict_payload(
    dossier: CandidateDossier,
    relations: Sequence[FactRelation],
) -> str:
    return json.dumps(
        {
            "candidate_alias": "C001",
            "candidate": {
                "type": dossier.candidate.type,
                "claim": dossier.candidate.claim,
                "file": dossier.candidate.file,
                "line": dossier.candidate.line,
                "severity_proposal": dossier.candidate.severity_proposal.value,
                "suggestion": dossier.candidate.suggestion,
                "confidence": dossier.candidate.confidence,
            },
            "task_patch": dossier.task.patch,
            "relations": [item.model_dump(mode="json") for item in relations],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
