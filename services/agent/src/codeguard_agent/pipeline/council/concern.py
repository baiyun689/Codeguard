"""ConcernAnalyzer：候选组 → 结构化 CandidateConcern。

读取 CandidateGroup 全部成员，确定性提取已有字段。
始终保持 singleton fallback——任何失败都不丢候选。
"""

from __future__ import annotations

import logging
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.council import (
    CandidateClaim,
    CandidateConcern,
    CandidateIssue,
    ConcernAnalysis,
    ConcernTagResolution,
)
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.council.dedup import CandidateGroup

# Rebuild models that reference RiskTag as a forward reference
# (RiskTag is imported under TYPE_CHECKING in models/council to avoid circular imports)
import codeguard_agent.models.council as _council_mod
_CouncilNS = {'RiskTag': RiskTag}
_CouncilNS.update({k: v for k, v in _council_mod.__dict__.items() if not k.startswith('_')})
ConcernTagResolution.model_rebuild(_types_namespace=_CouncilNS)
CandidateConcern.model_rebuild(_types_namespace=_CouncilNS)

logger = logging.getLogger("codeguard")
_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"


class _ParsedClaim(BaseModel):
    candidate_id: str
    root_cause: str = ""
    trigger: str = ""
    observable_consequence: str = ""
    fix_location: str = ""
    fix_action: str = ""
    affected_path: list[str] = Field(default_factory=list)


class _ParsedClaimBatch(BaseModel):
    claims: list[_ParsedClaim] = Field(default_factory=list)

    @field_validator("claims", mode="before")
    @classmethod
    def parse_stringified_claims(cls, value: object) -> object:
        if isinstance(value, str):
            for candidate in (
                value,
                value[value.find("[") : value.rfind("]") + 1],
            ):
                try:
                    parsed = json.loads(candidate)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, dict):
                    parsed = parsed.get("claims", parsed)
                if isinstance(parsed, list):
                    return parsed
        return value


def _extract_claim_from_candidate(candidate: CandidateIssue) -> CandidateClaim:
    """从单个候选确定性提取已有结构化字段。"""
    unresolved: list[str] = []
    if not candidate.suggestion:
        unresolved.append("fix_action")
    # trigger 和 observable_consequence 仍需语义解析；先保留完整原文，不能留空后
    # 让 EvidencePlanner 退化为与候选无关的通用问题。
    unresolved.extend(["trigger", "observable_consequence"])

    return CandidateClaim(
        candidate_id=candidate.id,
        root_cause=candidate.claim,
        trigger="",
        observable_consequence=candidate.claim,
        fix_location=f"{candidate.file}:{candidate.line}" if candidate.line else candidate.file,
        fix_action=candidate.suggestion,
        unresolved_fields=tuple(unresolved),
    )


def _parse_candidate_claims(
    candidates: Sequence[CandidateIssue],
    *,
    llm: Any,
    structured_method: str,
) -> tuple[dict[str, CandidateClaim], tuple[str, ...]]:
    if llm is None or not candidates:
        return {}, ()
    payload = {
        "candidates": [
            {
                "candidate_id": candidate.id,
                "claim": candidate.claim,
                "suggestion": candidate.suggestion,
                "file": candidate.file,
                "line": candidate.line,
                "type": candidate.type,
            }
            for candidate in candidates
        ]
    }
    try:
        structured = llm.with_structured_output(
            _ParsedClaimBatch, method=structured_method,
        )
        result = invoke_with_retry(
            structured,
            [
                (
                    "system",
                    (_PROMPT_DIR / "concern-analyzer.txt").read_text(
                        encoding="utf-8",
                    ),
                ),
                ("user", json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            ],
            max_retries=1,
        )
        if result is None:
            return {}, ("concern_claim_parse_none",)
        batch = (
            result
            if isinstance(result, _ParsedClaimBatch)
            else _ParsedClaimBatch.model_validate(result)
        )
    except Exception:
        logger.warning("ConcernAnalyzer claim parsing failed", exc_info=True)
        return {}, ("concern_claim_parse_failed",)

    candidates_by_id = {candidate.id: candidate for candidate in candidates}
    parsed: dict[str, CandidateClaim] = {}
    diagnostics: list[str] = []
    for item in batch.claims:
        candidate = candidates_by_id.get(item.candidate_id)
        if candidate is None:
            diagnostics.append(f"unknown parsed candidate: {item.candidate_id}")
            continue
        if item.candidate_id in parsed:
            diagnostics.append(f"duplicate parsed candidate: {item.candidate_id}")
            continue
        fallback = _extract_claim_from_candidate(candidate)
        trigger = item.trigger.strip()
        consequence = item.observable_consequence.strip()
        unresolved = [
            field
            for field, value in (
                ("trigger", trigger),
                ("observable_consequence", consequence),
                ("fix_action", item.fix_action.strip() or fallback.fix_action),
            )
            if not value
        ]
        parsed[item.candidate_id] = CandidateClaim(
            candidate_id=candidate.id,
            root_cause=item.root_cause.strip() or fallback.root_cause,
            trigger=trigger,
            observable_consequence=consequence or fallback.observable_consequence,
            fix_location=item.fix_location.strip() or fallback.fix_location,
            fix_action=item.fix_action.strip() or fallback.fix_action,
            affected_path=tuple(
                value.strip() for value in item.affected_path if value.strip()
            ),
            unresolved_fields=tuple(unresolved),
        )
    missing = set(candidates_by_id) - set(parsed)
    if missing:
        diagnostics.append(f"missing parsed candidates: {sorted(missing)}")
    return parsed, tuple(diagnostics)


def _extract_tags_from_members(
    members: Sequence[CandidateIssue],
    group: CandidateGroup | None = None,
    tag_resolutions: Mapping[str, Any] | None = None,
) -> ConcernTagResolution:
    """从成员和 group 聚合候选标签。"""
    all_tags: list[RiskTag] = []

    # 从 member type 字段提取
    for member in members:
        resolved_tag = (tag_resolutions or {}).get(member.id)
        if isinstance(resolved_tag, RiskTag) and resolved_tag is not RiskTag.GENERAL_REVIEW:
            all_tags.append(resolved_tag)
        try:
            tag = RiskTag(member.type)
            if tag is not RiskTag.GENERAL_REVIEW:
                all_tags.append(tag)
        except ValueError:
            pass

    # 从 group 的 primary_risk_tag 获取
    if group is not None and group.primary_risk_tag is not RiskTag.GENERAL_REVIEW:
        if group.primary_risk_tag not in all_tags:
            all_tags.insert(0, group.primary_risk_tag)

    # 去重保持顺序
    seen: set[RiskTag] = set()
    unique: list[RiskTag] = []
    for tag in all_tags:
        if tag not in seen:
            seen.add(tag)
            unique.append(tag)

    concrete = [t for t in unique if t is not RiskTag.GENERAL_REVIEW]
    if not concrete:
        return ConcernTagResolution(
            source="unclassified",
            reasons=("no concrete tags from members or group",),
        )

    primary = concrete[0]
    secondary = tuple(concrete[1:3])  # max 2
    return ConcernTagResolution(
        primary_tag=primary,
        secondary_tags=secondary,
        confidence=0.85,
        source="deterministic",
        reasons=(f"aggregated from {len(members)} members and group",),
    )


def _members_share_core(
    claims: Sequence[CandidateClaim],
) -> bool:
    """判定成员是否可以共享一个 concern。"""
    if len(claims) <= 1:
        return True
    first_rc = claims[0].root_cause[:80].strip().lower()
    if not first_rc:
        return False
    return all(
        c.root_cause[:80].strip().lower() == first_rc
        for c in claims[1:]
    )


def _build_singleton_concern(
    candidate: CandidateIssue,
    *,
    tag_resolutions: Mapping[str, Any] | None = None,
    parsed_claims: Mapping[str, CandidateClaim] | None = None,
) -> CandidateConcern:
    """为单个候选构造最小 CandidateConcern（singleton fallback）。"""
    claim = (parsed_claims or {}).get(
        candidate.id, _extract_claim_from_candidate(candidate),
    )
    return CandidateConcern(
        member_candidate_ids=(candidate.id,),
        claims=(claim,),
        tags=_extract_tags_from_members(
            (candidate,), tag_resolutions=tag_resolutions,
        ),
        member_risk_tags={
            candidate.id: tuple(
                tag
                for tag in (
                    (tag_resolutions or {}).get(candidate.id),
                )
                if isinstance(tag, RiskTag)
                and tag is not RiskTag.GENERAL_REVIEW
            )
        },
        source_agents=(candidate.source_agent,),
        task_ids=(candidate.task_id,),
        files=(candidate.file,),
        confidence=candidate.confidence,
    )


def analyze_candidate_groups(
    groups: Sequence[CandidateGroup],
    *,
    candidates: Sequence[CandidateIssue] = (),
    candidate_tag_resolutions: Mapping[str, Any] | None = None,
    llm: Any = None,
    structured_method: str = "function_calling",
) -> ConcernAnalysis:
    """从候选组无损构造 CandidateConcern 列表。

    不修改候选去重判定。无法形成共享 concern 时自动拆组为 singleton。
    保证：输入 candidate IDs 的集合 == concerns 覆盖的 member_candidate_ids 集合。
    """
    concerns: list[CandidateConcern] = []
    candidate_to_concern: dict[str, str] = {}
    diagnostics: list[str] = []
    candidate_universe = {
        candidate.id: candidate
        for candidate in (
            *(member for group in groups for member in group.members),
            *candidates,
        )
    }
    parsed_claims, parse_diagnostics = _parse_candidate_claims(
        tuple(candidate_universe.values()),
        llm=llm,
        structured_method=structured_method,
    )
    diagnostics.extend(parse_diagnostics)

    for group in groups:
        members = list(group.members)
        if not members:
            continue

        claims = [
            parsed_claims.get(m.id, _extract_claim_from_candidate(m))
            for m in members
        ]

        if _members_share_core(claims):
            concern = CandidateConcern(
                group_id=group.id,
                member_candidate_ids=tuple(m.id for m in members),
                claims=tuple(claims),
                tags=_extract_tags_from_members(
                    members, group, candidate_tag_resolutions,
                ),
                member_risk_tags={
                    m.id: tuple(
                        tag
                        for tag in (
                            (candidate_tag_resolutions or {}).get(m.id)
                            or group.primary_risk_tag,
                        )
                        if isinstance(tag, RiskTag)
                        and tag is not RiskTag.GENERAL_REVIEW
                    )
                    for m in members
                },
                source_agents=tuple(dict.fromkeys(m.source_agent for m in members)),
                task_ids=tuple(dict.fromkeys(m.task_id for m in members)),
                files=tuple(dict.fromkeys(m.file for m in members)),
                confidence=group.confidence,
            )
            concerns.append(concern)
            for m in members:
                candidate_to_concern[m.id] = concern.concern_id
        else:
            diagnostics.append(
                f"group {group.id} split: members do not share core claim"
            )
            for m in members:
                singleton = _build_singleton_concern(
                    m,
                    tag_resolutions=candidate_tag_resolutions,
                    parsed_claims=parsed_claims,
                )
                concerns.append(singleton)
                candidate_to_concern[m.id] = singleton.concern_id

    # accepted groups 只覆盖真正归并的成员；所有未归组候选必须以 singleton
    # 进入 concern，否则一旦存在任意 group，普通候选会被整批漏掉。
    grouped_ids = {m.id for g in groups for m in g.members}
    for candidate in candidates:
        if candidate.id in grouped_ids or candidate.id in candidate_to_concern:
            continue
        singleton = _build_singleton_concern(
            candidate,
            tag_resolutions=candidate_tag_resolutions,
            parsed_claims=parsed_claims,
        )
        concerns.append(singleton)
        candidate_to_concern[candidate.id] = singleton.concern_id

    # 验证覆盖率
    all_candidate_ids = grouped_ids | {candidate.id for candidate in candidates}
    covered = set(candidate_to_concern.keys())
    missing = all_candidate_ids - covered
    if missing:
        diagnostics.append(
            f"candidate coverage gap: {sorted(missing)}"
        )

    return ConcernAnalysis(
        concerns=tuple(concerns),
        candidate_to_concern=candidate_to_concern,
        diagnostics=tuple(diagnostics),
    )
