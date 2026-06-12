"""聚合去重阶段。

阶段 3:把多个并行审查员产出的 issues 做**跨审查员去重**。

设计取舍:
- 只做**纯规则去重**(确定性、零成本),不调 LLM。一种更重的做法是再加一次 LLM
  做二次聚合/改写,但那更贵、更不可控,留到"规则去重不够用"时再说。
- **保守指纹**:只合并近乎相同的发现,不强行合并不同问题——宁可漏合,不可错合。
  跨审查员"同一问题但措辞不同"这类语义重复,规则抓不全,留待后续(语义聚合/统一 type 分类)。
- 本阶段**不删误报**,只去重复。这样"去重"与"误报过滤(阶段4)"的效果能分别量化。

去重时同一指纹保留**最高 severity**(severity 相同则保留 confidence 更高的那条)。
"""

from __future__ import annotations

import logging
import re

from codeguard_agent.models.schemas import Issue, Severity
from codeguard_agent.pipeline.stages.base import PipelineContext, PipelineStage

logger = logging.getLogger("codeguard")

_SEVERITY_RANK = {Severity.CRITICAL: 3, Severity.WARNING: 2, Severity.INFO: 1}


def _basename(path: str) -> str:
    """取文件名(忽略目录与大小写),不同审查员可能报不同前缀的路径。"""
    return (path or "").replace("\\", "/").rsplit("/", 1)[-1].strip().lower()


def _norm_text(text: str) -> str:
    """归一化文本:折叠空白、转小写、截断,抵消无意义的措辞差异。"""
    return re.sub(r"\s+", " ", text or "").strip().lower()[:160]


def _dedup_key(issue: Issue) -> tuple:
    """构造去重指纹。

    有行号(line>0)时:同一 文件+行号+type 视为重复(最强信号);
    无行号时:退化为 文件+type+归一化message,避免把无法定位的不同问题误并。
    """
    file_key = _basename(issue.file)
    type_key = (issue.type or "").strip().lower()
    if issue.line and issue.line > 0:
        return (file_key, issue.line, type_key)
    return (file_key, type_key, _norm_text(issue.message))


def _better(a: Issue, b: Issue) -> Issue:
    """同一指纹下选更值得保留的一条:先比 severity,再比 confidence。"""
    ra, rb = _SEVERITY_RANK.get(a.severity, 0), _SEVERITY_RANK.get(b.severity, 0)
    if ra != rb:
        return a if ra > rb else b
    return a if a.confidence >= b.confidence else b


def deduplicate(issues: list[Issue]) -> list[Issue]:
    """跨审查员去重,保留首次出现顺序。"""
    best: dict[tuple, Issue] = {}
    order: list[tuple] = []
    for issue in issues:
        key = _dedup_key(issue)
        if key not in best:
            best[key] = issue
            order.append(key)
        else:
            best[key] = _better(best[key], issue)
    return [best[key] for key in order]


class AggregationStage(PipelineStage):
    """聚合阶段:跨审查员去重。"""

    @property
    def name(self) -> str:
        return "aggregation"

    def execute(self, context: PipelineContext) -> PipelineContext:
        before = len(context.issues)
        context.issues = deduplicate(context.issues)
        after = len(context.issues)
        logger.info("管线阶段 [aggregation]:去重 %d → %d 条(合并 %d 条重复)",
                    before, after, before - after)
        return context
