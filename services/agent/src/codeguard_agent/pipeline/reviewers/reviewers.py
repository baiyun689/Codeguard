"""发现者 Agent 定义与辅助函数。

Reviewer dataclass 描述每个发现者的配置（名称、prompt、共享工具边界）。
DEFAULT_REVIEWERS 是三个默认发现者（ThreatModel/Behavior/Maintainability）。
辅助函数供 graph.py 的发现者子图使用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from codeguard_agent.models.tasks import ReviewTask, TaskSymbolContext
from codeguard_agent.pipeline.prompting import render_prompt_template

logger = logging.getLogger("codeguard")

# prompts/ 目录在 codeguard_agent 包下。本文件位于 codeguard_agent/pipeline/reviewers/,
# 上溯两层(reviewers → pipeline → codeguard_agent)再进 prompts/。
_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"

COMMON_REVIEW_TOOLS = [
    "get_file_content",
    "inspect_structure",
    "inspect_change_impact",
    "inspect_path",
]


@dataclass(frozen=True)
class Reviewer:
    """一个领域审查员:名字 + 它的 system prompt 文件名 + 工具白名单。

    tool_allowlist:该审查员可用的工具名称列表。None=使用全局默认;[]=无工具(直连)。
    """

    name: str
    prompt_file: str
    source_agent: str = ""
    tool_allowlist: list[str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_agent", self.source_agent or self.name)


# 默认的三个并行领域审查员共享全部事实工具；领域 Prompt 决定使用时机。
DEFAULT_REVIEWERS: tuple[Reviewer, ...] = (
    Reviewer(
        "ThreatModelAgent",
        "threat-model-base.txt",
        source_agent="threat_model",
        tool_allowlist=list(COMMON_REVIEW_TOOLS),
    ),
    Reviewer(
        "BehaviorAgent",
        "behavior-base.txt",
        source_agent="behavior",
        tool_allowlist=list(COMMON_REVIEW_TOOLS),
    ),
    Reviewer(
        "MaintainabilityAgent",
        "maintainability-base.txt",
        source_agent="maintainability",
        tool_allowlist=list(COMMON_REVIEW_TOOLS),
    ),
)


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


_DISCOVERY_CONTEXT_CONTRACT = "discovery-context-contract.txt"
_DISCOVERY_TOOL_CONTRACT = "discovery-tool-contract.txt"
_DISCOVERY_EVIDENCE_CONTRACT = "discovery-evidence-contract.txt"
_DISCOVERY_OUTPUT_CONTRACT = "discovery-output-contract.txt"
_DISCOVERY_OUTPUT_REMINDER = "discovery-output-reminder.txt"


def build_reviewer_system_prompt(reviewer: Reviewer) -> str:
    """组合角色方法论、共享上下文契约与证据引用契约。"""
    return "\n\n".join([
        _load_prompt(reviewer.prompt_file).strip(),
        _load_prompt(_DISCOVERY_CONTEXT_CONTRACT).strip(),
        _load_prompt(_DISCOVERY_TOOL_CONTRACT).strip(),
        _load_prompt(_DISCOVERY_EVIDENCE_CONTRACT).strip(),
        _load_prompt(_DISCOVERY_OUTPUT_CONTRACT).strip(),
    ])


def _attr(value: object) -> str:
    return escape(str(value), quote=True)


def _text(value: object) -> str:
    return escape(str(value), quote=False)


def build_reviewer_user_prompt(
    *,
    task: ReviewTask,
    summary: str = "",
    symbol_context: TaskSymbolContext | None = None,
    task_knowledge: str = "",
    plan_objectives: tuple[str, ...] = (),
    task_scope: str = "current_hunk",
    catalog: Any = None,
    user_prompt_file: str = "threat-model-user.txt",
) -> str:
    """把本次 task 的动态值统一渲染进 user 消息。

    task_scope: "current_hunk"（hunk 级审查）或 "current_file"（文件级审查）。
    catalog:证据目录(EvidenceCatalog);非 None 时给 task_patch/符号事实
    渲染 evidence_id 短别名,供审查员按编号引用证据(Evidence Ledger)。
    """
    fact_aliases = catalog.symbol_aliases() if catalog is not None else []
    coverage = (
        "full_new_file"
        if task.patch_complete
        and task.hunk_header.strip().startswith("@@ -0,0 +")
        else task_scope
    )
    parts = ["<review_input>"]
    if summary.strip():
        parts.extend([
            '  <change_summary role="orientation_not_evidence">',
            _text(summary.strip()),
            "  </change_summary>",
        ])
    parts.extend([
        (
            f'  <task_patch scope="{_attr(task_scope)}" coverage="{_attr(coverage)}" '
            f'task_id="{_attr(task.id)}" file="{_attr(task.file)}">'
        ),
        _text(task.patch),
        "  </task_patch>",
    ])
    if task.deletion_anchors:
        parts.append("  <deletion_anchors>")
        for anchor in task.deletion_anchors:
            parts.extend([
                (
                    f'    <anchor line="{anchor.anchor_line}" '
                    f'kind="{_attr(anchor.anchor_kind)}">'
                ),
                "当前 revision 中与 patch 删除块相邻的有效定位与符号查询入口。"
                "报告该删除引入的问题时使用此 line，location_snippet 保持空字符串。",
                "      <deleted_fragment>",
                _text(anchor.deleted_snippet),
                "      </deleted_fragment>",
                "    </anchor>",
            ])
        parts.append("  </deletion_anchors>")
    if symbol_context is not None:
        parts.append(
            "  <symbol_context "
            f'status="{_attr(symbol_context.status.value)}" '
            f'truncated="{str(symbol_context.truncated).lower()}">'
        )
        for idx, symbol in enumerate(symbol_context.symbols):
            fact_alias = fact_aliases[idx] if idx < len(fact_aliases) else ""
            parts.extend([
                (
                    f'    <symbol symbol_id="{_attr(symbol.symbol_id)}" '
                    f'kind="{_attr(symbol.kind)}" source_set="{_attr(symbol.source_set)}" '
                    f'file="{_attr(symbol.file)}" start_line="{symbol.start_line}" '
                    f'end_line="{symbol.end_line}"'
                    + (f' evidence_id="{_attr(fact_alias)}"' if fact_alias else "")
                    + ">"
                ),
                _text(symbol.model_dump_json()),
                "    </symbol>",
            ])
            domain = Path(user_prompt_file).stem
            if domain == "behavior":
                recommendation = (
                    "caller/入口/影响范围→inspect_change_impact; "
                    "callee/listener/callback→inspect_path(behavior); "
                    "字段读写/一跳关系→inspect_structure; "
                    "条件/顺序/状态赋值→先定位后 get_file_content"
                )
            elif domain == "threat-model":
                recommendation = (
                    "下游敏感调用线索→inspect_path(security); "
                    "上游入口/影响范围→inspect_change_impact; "
                    "参数使用/保护条件→先定位后 get_file_content"
                )
            else:
                recommendation = (
                    "局部耦合/继承/字段/一跳依赖→inspect_structure; "
                    "跨符号执行耦合→inspect_path(behavior); "
                    "受影响调用方→inspect_change_impact"
                )
            parts.append(
                f'    <query_hint symbol_id="{_attr(symbol.symbol_id)}" '
                f'range="{symbol.start_line}-{symbol.end_line}" '
                f'changed_lines="{_attr(",".join(str(line) for line in task.changed_lines if symbol.start_line <= line <= symbol.end_line))}" '
                f'deletion_anchor_lines="{_attr(",".join(str(anchor.anchor_line) for anchor in task.deletion_anchors if symbol.start_line <= anchor.anchor_line <= symbol.end_line))}">'
                f'{_text(recommendation)}</query_hint>'
            )
        for limitation in symbol_context.limitations:
            parts.append(f"    <limitation>{_text(limitation)}</limitation>")
        parts.append("  </symbol_context>")
    if task_knowledge.strip():
        parts.extend([
            '  <knowledge_bundle role="methodology_not_repository_fact">',
            _text(task_knowledge.strip()),
            "  </knowledge_bundle>",
        ])
    if plan_objectives:
        parts.extend([
            '  <review_plan role="review_focus_not_evidence">',
            "    本 task 的 Plan 审查重点：",
            *[f"    - {_text(objective)}" for objective in plan_objectives if objective.strip()],
            "  </review_plan>",
        ])
    parts.append("</review_input>")
    rendered = render_prompt_template(
        (_PROMPT_DIR / user_prompt_file).read_text(encoding="utf-8"),
        {"dynamic_context": "\n".join(parts)},
    )
    return f"{rendered.rstrip()}\n\n{_load_prompt(_DISCOVERY_OUTPUT_REMINDER).strip()}\n"
