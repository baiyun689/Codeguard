"""按 RiskTag 加载确定性的领域知识片段。

知识直接注入审查员 prompt（不走工具循环），相关的 RiskTag 已由任务 RiskProfile
确定性给出——无需审查员自行判断需要哪类知识。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from codeguard_agent.models.tasks import RiskTag

_KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "prompts" / "knowledge"


def load_knowledge(domain: str, tags: Iterable[RiskTag]) -> str:
    """Return matching domain knowledge fragments in stable RiskTag order.

    ``GENERAL_REVIEW`` deliberately has no knowledge fragment: it is an
    unclassified fallback rather than a review direction. Missing fragments are
    ignored so an incomplete knowledge library cannot interrupt a review; the
    later completeness test is responsible for enforcing file coverage.
    """
    requested_tags = set(tags)
    parts: list[str] = []

    for tag in RiskTag:
        if tag is RiskTag.GENERAL_REVIEW or tag not in requested_tags:
            continue
        path = _KNOWLEDGE_DIR / domain / f"{tag.value}.txt"
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8").strip()
        if content:
            parts.append(content)

    return "\n\n".join(parts)
