"""Risk-aware Knowledge 选择的数据模型。

这些模型描述 Knowledge 片段、选择结果和渲染输出，
供 catalog、selector 和 reviewer prompt builder 使用。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from codeguard_agent.models.tasks import ReviewerKind, RiskTag


class KnowledgeKind(str, Enum):
    BASE = "base"
    SPECIALIZED = "specialized"


class KnowledgeSelectionSource(str, Enum):
    RISK_PRIOR = "risk_prior"
    PATCH_SEMANTICS = "patch_semantics"
    FILE_ROLE = "file_role"
    CONTEXT_SYMBOL = "context_symbol"


class KnowledgeFragment(BaseModel):
    """一段 Knowledge 内容及其元数据。"""

    reviewer: ReviewerKind
    kind: KnowledgeKind
    topic: str
    risk_tag: RiskTag | None = None
    content: str = ""
    # 专门主题的检索词：strong_terms 强匹配（函数调用/API名），weak_terms 弱匹配（概念/模式）
    strong_terms: tuple[str, ...] = ()
    weak_terms: tuple[str, ...] = ()


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
