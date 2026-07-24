"""Load deterministic RiskTag-scoped domain knowledge fragments.

Knowledge is injected directly rather than exposed as a tool: Phase 4 direct
reviews have no tool loop, and the relevant tags are already deterministic
signals from the task's ``RiskProfile``.
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
