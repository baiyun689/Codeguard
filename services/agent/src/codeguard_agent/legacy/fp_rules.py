"""误报过滤的规则层(纯函数,不依赖 LLM,可单测)。

职责:加载 YAML 规则文件 + 判断单条 issue 是否命中"确定性排除规则"。
只做可正则化/路径化的确定性判断(路径/扩展名/泛泛建议正则/置信度阈值);
语义型判断(如某框架写法是否安全)是审查员 prompt 的职责,这里不重复。

关键约束:
- 纯函数、确定性、可复现——所有不确定性都不在这一层(便于 pytest 锁死)。
- 规则文件**缺失/为空/解析失败** → 视为"无规则",match_exclusion 永远返回 None(不剔除),
  管线照常运行而不抛错(见 spec「规则文件缺失或为空」场景)。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codeguard_agent.models.schemas import Issue

logger = logging.getLogger("codeguard")

# config/ 在 agent 目录下(services/agent/config/),与包 src/codeguard_agent 平级。
# 本文件: src/codeguard_agent/pipeline/fp_rules.py → parents[3] = services/agent。
_DEFAULT_RULES_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "false-positive-rules.yaml"
)


@dataclass
class FpRules:
    """编译后的误报规则。空实例 = 无规则(不剔除任何东西)。"""

    # (rule_id, 合并后的正则) —— 命中 type/message 即剔除
    message_patterns: list[tuple[str, re.Pattern]] = field(default_factory=list)
    # (rule_id, 子串列表,均已小写) —— 路径含任一子串即剔除
    path_substrings: list[tuple[str, list[str]]] = field(default_factory=list)
    # (rule_id, 合并后的正则) —— 命中文件名(basename)即剔除
    path_filename_patterns: list[tuple[str, re.Pattern]] = field(default_factory=list)
    # 置信度阈值:低于则剔除;None 表示不按置信度过滤
    auto_exclude_below: float | None = None

    @property
    def is_empty(self) -> bool:
        return not (
            self.message_patterns
            or self.path_substrings
            or self.path_filename_patterns
            or self.auto_exclude_below is not None
        )


def _compile_group(group: dict[str, Any] | None) -> list[tuple[str, re.Pattern]]:
    """把 {rule_id: [pattern, ...]} 编译成 [(rule_id, 合并正则)]。

    空列表会被跳过——否则 ``re.compile("")`` 会匹配一切,造成灾难性误删。
    """
    compiled: list[tuple[str, re.Pattern]] = []
    for rule_id, patterns in (group or {}).items():
        pats = [p for p in (patterns or []) if p]
        if not pats:
            continue
        compiled.append((rule_id, re.compile("|".join(pats), re.IGNORECASE)))
    return compiled


def _compile(data: dict[str, Any]) -> FpRules:
    rules = FpRules()
    rules.message_patterns = _compile_group(data.get("message_patterns"))
    rules.path_filename_patterns = _compile_group(data.get("path_filename_patterns"))
    for rule_id, subs in (data.get("path_substrings") or {}).items():
        cleaned = [s.lower() for s in (subs or []) if s]
        if cleaned:
            rules.path_substrings.append((rule_id, cleaned))
    conf = data.get("confidence") or {}
    threshold = conf.get("auto_exclude_below")
    rules.auto_exclude_below = float(threshold) if threshold is not None else None
    return rules


def load_rules(path: Path | None = None) -> FpRules:
    """加载并编译规则。缺失/为空/解析失败一律降级为无规则的空 FpRules。"""
    rules_path = path or _DEFAULT_RULES_PATH
    if not rules_path.exists():
        logger.info("误报规则文件不存在(%s),按无规则处理", rules_path)
        return FpRules()
    try:
        import yaml  # 延迟导入:与评测一致,未装 pyyaml 时也不至于 import 期就崩

        data = yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 规则坏了不该拖垮管线
        logger.warning("误报规则解析失败(%s),按无规则处理: %s", rules_path, exc)
        return FpRules()
    if not isinstance(data, dict):
        logger.warning("误报规则格式非 dict(%s),按无规则处理", rules_path)
        return FpRules()
    return _compile(data)


def match_exclusion(issue: Issue, rules: FpRules) -> str | None:
    """判断单条 issue 是否命中排除规则。命中返回规则 id,否则 None。

    判定顺序(任一命中即返回):置信度阈值 → 路径子串 → 文件名正则 → type/message 正则。
    纯函数、确定性,对同一输入恒定返回。
    """
    if rules.auto_exclude_below is not None and issue.confidence < rules.auto_exclude_below:
        return "low-confidence"

    path = (issue.file or "").replace("\\", "/").lower()
    if path:
        for rule_id, subs in rules.path_substrings:
            if any(s in path for s in subs):
                return rule_id
        basename = path.rsplit("/", 1)[-1]
        for rule_id, pattern in rules.path_filename_patterns:
            if pattern.search(basename):
                return rule_id

    haystack = f"{issue.type or ''} {issue.message or ''}".lower()
    for rule_id, pattern in rules.message_patterns:
        if pattern.search(haystack):
            return rule_id

    return None
