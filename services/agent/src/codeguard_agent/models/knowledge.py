"""Knowledge 片段、选择结果和渲染输出的数据模型。"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from codeguard_agent.models.tasks import ReviewerKind


class KnowledgeKind(str, Enum):
    BASE = "base"
    SPECIALIZED = "specialized"


class KnowledgeSelectionSource(str, Enum):
    PLAN = "plan"


class KnowledgeFragment(BaseModel):
    """一段 Knowledge 内容及其元数据。"""

    reviewer: ReviewerKind
    kind: KnowledgeKind
    topic: str
    content: str = ""


class SelectedKnowledge(BaseModel):
    """被选中注入的 Knowledge 片段及选择理由。"""

    fragment: KnowledgeFragment
    score: float = 0.0
    reasons: tuple[str, ...] = ()


class KnowledgeBundle(BaseModel):
    """一个 (task, reviewer) 的完整 Knowledge 注入包。

    调用者只消费 rendered_text 和诊断字段，不读取文件系统。
    """

    task_id: str
    reviewer: ReviewerKind
    base: SelectedKnowledge | None = None
    specialized: tuple[SelectedKnowledge, ...] = ()
    rendered_text: str = ""
    truncated: bool = False
    omitted_topics: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


class KnowledgeBudget(BaseModel):
    """Knowledge 选择的资源约束。"""

    max_chars: int = 6000
    max_specialized_fragments: int = 3
    reserved_base_chars: int = 1200
