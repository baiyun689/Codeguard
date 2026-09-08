"""文件系统 Knowledge Catalog：发现、读取和校验知识片段。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from codeguard_agent.models.knowledge import KnowledgeFragment, KnowledgeKind
from codeguard_agent.models.tasks import ReviewerKind

logger = logging.getLogger("codeguard")
_KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "prompts" / "knowledge"


class KnowledgeCatalog:
    """按 Reviewer 目录读取 BASE 和显式可选的主题片段。"""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or _KNOWLEDGE_DIR

    def base_fragment(self, reviewer: ReviewerKind) -> KnowledgeFragment | None:
        return self._read_fragment(
            self._root / reviewer.value / "BASE.txt",
            reviewer,
            KnowledgeKind.BASE,
            "BASE",
        )

    def specialized_fragments(
        self, reviewer: ReviewerKind,
    ) -> Sequence[KnowledgeFragment]:
        domain_dir = self._root / reviewer.value
        if not domain_dir.is_dir():
            return ()
        fragments: list[KnowledgeFragment] = []
        for path in sorted(domain_dir.glob("*.txt"), key=lambda p: p.name):
            if path.stem == "BASE":
                continue
            fragment = self._read_fragment(
                path, reviewer, KnowledgeKind.SPECIALIZED, path.stem,
            )
            if fragment is not None:
                fragments.append(fragment)
        return tuple(fragments)

    def shared_specialized_fragments(self) -> Sequence[KnowledgeFragment]:
        """返回历史领域目录合并后的主题集合。

        Controlled 模式的 ReviewPlan 只负责 task 级知识路由；统一 Reviewer
        看到同一份专项主题，避免把知识路由误当成 reviewer 路由。
        同名主题按稳定领域顺序保留第一份，重复内容不会注入两次。
        """
        fragments: list[KnowledgeFragment] = []
        seen: set[str] = set()
        for reviewer in ReviewerKind:
            for fragment in self.specialized_fragments(reviewer):
                if fragment.topic in seen:
                    continue
                seen.add(fragment.topic)
                fragments.append(fragment)
        return tuple(fragments)

    def _read_fragment(
        self,
        path: Path,
        reviewer: ReviewerKind,
        kind: KnowledgeKind,
        topic: str,
    ) -> KnowledgeFragment | None:
        if not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            logger.warning("Knowledge fragment read failed: %s", path, exc_info=True)
            return None
        if not content:
            return None
        return KnowledgeFragment(
            reviewer=reviewer,
            kind=kind,
            topic=topic,
            content=content,
        )
