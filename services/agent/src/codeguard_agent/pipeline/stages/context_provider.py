"""ADR-032 ContextProvider:在 ReviewCouncil 前构造共享事实包。"""

from __future__ import annotations

import logging

from codeguard_agent.git.diff_collector import parse_changed_files
from codeguard_agent.models.council import ContextBundle, ContextFact
from codeguard_agent.pipeline.engines import GatheredContext
from codeguard_agent.pipeline.stages.base import PipelineContext, PipelineStage

logger = logging.getLogger("codeguard")

_FACT_BUDGET = 4000


def _clip(text: str, budget: int = _FACT_BUDGET) -> tuple[str, bool]:
    if len(text) <= budget:
        return text, False
    return text[:budget] + "...(已截断)", True


class ContextProviderStage(PipelineStage):
    """构造 ReviewCouncil 共享 ContextBundle。

    第一版只产出事实和来源信息,不判断候选是否为真实问题。
    """

    @property
    def name(self) -> str:
        return "context_provider"

    def execute(self, context: PipelineContext) -> PipelineContext:
        changed_files = parse_changed_files(context.diff_text)
        facts: list[ContextFact] = []
        sources = ["diff"]

        for path in changed_files:
            facts.append(ContextFact(source="diff", kind="changed_file", content=path))

        if context.diff_summary.strip():
            facts.append(
                ContextFact(
                    source="summary",
                    kind="summary",
                    content=context.diff_summary.strip(),
                )
            )
            sources.append("summary")

        gathered: list[GatheredContext] = []
        if context.tool_client is not None:
            resp = context.tool_client.find_sensitive_apis()
            content = resp.as_tool_output()
            clipped, truncated = _clip(content)
            if content.strip():
                facts.append(
                    ContextFact(
                        source="tool:find_sensitive_apis",
                        kind="sensitive_api",
                        content=clipped,
                        truncated=truncated,
                    )
                )
                gathered.append(GatheredContext("find_sensitive_apis", "{}", content))
                sources.append("tool:find_sensitive_apis")

        bundle = ContextBundle(
            changed_files=changed_files,
            diff_summary=context.diff_summary,
            facts=facts,
            sources=sorted(set(sources)),
            truncated=any(f.truncated for f in facts),
        )
        context.context_bundle = bundle
        context.gathered_context.extend(gathered)
        logger.info(
            "管线阶段 [context_provider]:%d 个文件,%d 条事实,来源=%s",
            len(changed_files),
            len(facts),
            bundle.sources,
        )
        return context

