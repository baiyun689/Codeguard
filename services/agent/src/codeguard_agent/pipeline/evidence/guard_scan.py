"""确定性 guard 注解扫描(Evidence Ledger 保留的确定性反证)。

只覆盖两个确定场景:@PreAuthorize 等鉴权注解、@Transactional 事务边界。
命中产出 direct 反证供 Judge 前零成本淘汰;未命中不产出
(不替代 Judge 的支持/反驳判定)。
"""

from __future__ import annotations

import json
import re

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.context import rules as context_rules
from codeguard_agent.pipeline.evidence.planner import CandidateDossier
from codeguard_agent.pipeline.evidence.tags import SECURITY_TAGS

_AUTHZ_ANNOTATIONS = ("PreAuthorize", "PostAuthorize", "Secured", "RolesAllowed")

# 以下四个私有函数自 agent.py 原样迁移(docstring 与语义逐行保留,旧副本待 T12 删除):
# 扫描锚定被审方法(dossier 的 symbol_context 方法解析 + legacy ast_structure 兜底),
# 不按"文件首个方法"直扫,避免多方法文件误报/漏报与字段初始化器劫持锚点。


def _strip_comments_and_strings(source: str) -> str:
    """移除 Java 注释/字符串内容并保持字符位置和换行。"""
    result = list(source)
    index = 0
    state = "code"
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if state == "code" and char == "/" and nxt == "/":
            result[index] = result[index + 1] = " "
            state = "line_comment"
            index += 2
            continue
        if state == "code" and char == "/" and nxt == "*":
            result[index] = result[index + 1] = " "
            state = "block_comment"
            index += 2
            continue
        if state == "code" and char in {'"', "'"}:
            result[index] = " "
            state = "string" if char == '"' else "char"
            index += 1
            continue
        if state == "line_comment":
            if char == "\n":
                state = "code"
            else:
                result[index] = " "
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and nxt == "/":
                result[index] = result[index + 1] = " "
                state = "code"
                index += 2
            else:
                if char != "\n":
                    result[index] = " "
                index += 1
            continue
        if state in {"string", "char"}:
            quote = '"' if state == "string" else "'"
            if char == "\\" and nxt:
                result[index] = " "
                if nxt != "\n":
                    result[index + 1] = " "
                index += 2
            elif char == quote:
                result[index] = " "
                state = "code"
                index += 1
            else:
                if char != "\n":
                    result[index] = " "
                index += 1
            continue
        index += 1
    return "".join(result)


_METHOD_RANGE = re.compile(r"\b(\w+)\([^)]*\).*\[L(\d+)-L(\d+)\]\s*$")


def _resolved_method(dossier: CandidateDossier) -> tuple[str, int, int, str] | None:
    bundle = dossier.context_bundle
    if bundle is None:
        return None
    for context_fact in bundle.facts:
        if context_fact.kind == "symbol_context" and not context_fact.truncated:
            try:
                payload = json.loads(context_fact.content)
                if payload.get("kind") not in {"method", "constructor"}:
                    continue
                symbol_id = str(payload.get("symbol_id", ""))
                method_name = symbol_id.rsplit("#", 1)[-1].split("(", 1)[0]
                return (
                    method_name,
                    int(payload.get("start_line", 0)),
                    int(payload.get("end_line", 0)),
                    " ".join(
                        [
                            *(
                                f"@{name}"
                                for name in payload.get("annotations", [])
                            ),
                            str(payload.get("signature", "")),
                        ]
                    ).strip(),
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        if context_fact.kind != "ast_structure" or context_fact.truncated:
            continue
        legacy_method_name = context_rules.resolve_method_name(
            context_fact.content, dossier.task
        )
        if legacy_method_name is None:
            continue
        task_span = context_rules._task_span(dossier.task)
        if task_span is None:
            return None
        for line in context_fact.content.splitlines():
            match = _METHOD_RANGE.search(line.strip())
            if not match or match.group(1) != legacy_method_name:
                continue
            start, end = int(match.group(2)), int(match.group(3))
            if start <= task_span[1] and end >= task_span[0]:
                return legacy_method_name, start, end, line.strip()
    return None


def _matching_brace(source: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _scoped_annotation(
    dossier: CandidateDossier,
    source: str,
    annotation_names: tuple[str, ...],
) -> str | None:
    resolved = _resolved_method(dossier)
    if resolved is None:
        return None
    method_name, start, end, ast_signature = resolved
    annotation_pattern = re.compile(
        r"@(" + "|".join(re.escape(name) for name in annotation_names) + r")\b"
    )
    ast_match = annotation_pattern.search(ast_signature)
    if ast_match:
        return f"当前方法 AST 声明含 @{ast_match.group(1)}"

    sanitized = _strip_comments_and_strings(source)
    lines = sanitized.splitlines()
    start_index = max(0, start - 1)
    end_index = min(len(lines), end)
    method_line = next(
        (
            index
            for index in range(start_index, end_index)
            if re.search(rf"\b{re.escape(method_name)}\s*\(", lines[index])
        ),
        None,
    )
    if method_line is None:
        return None
    method_declaration = "\n".join(lines[start_index : method_line + 1])
    method_match = annotation_pattern.search(method_declaration)
    if method_match:
        return f"当前方法声明含 @{method_match.group(1)}"

    line_offsets: list[int] = []
    offset = 0
    for line in sanitized.splitlines(keepends=True):
        line_offsets.append(offset)
        offset += len(line)
    if method_line >= len(line_offsets):
        return None
    method_offset = line_offsets[method_line]
    class_pattern = re.compile(r"\b(?:class|interface|record|enum)\s+\w+[^\{]*\{")
    owner = None
    for match in class_pattern.finditer(sanitized, 0, method_offset + 1):
        open_index = sanitized.find("{", match.start(), match.end())
        close_index = _matching_brace(sanitized, open_index)
        if close_index is not None and open_index < method_offset < close_index:
            owner = match
    if owner is None:
        return None
    class_line = sanitized.count("\n", 0, owner.start())
    declaration_start = class_line
    while declaration_start > 0:
        previous = lines[declaration_start - 1].strip()
        if not previous or previous.startswith("@") or previous.endswith(")"):
            declaration_start -= 1
            continue
        break
    class_declaration = "\n".join(lines[declaration_start : class_line + 1])
    class_match = annotation_pattern.search(class_declaration)
    if class_match:
        return f"当前所属类声明含 @{class_match.group(1)}"
    return None


def scan_guard_content(
    dossier: CandidateDossier,
    content: str,
    tag: RiskTag,
) -> str | None:
    """在证据内容(patch/文件)中确定性扫描 guard 注解;命中返回观察说明,否则 None。"""
    if content.strip() and tag in SECURITY_TAGS:
        observation = _scoped_annotation(dossier, content, _AUTHZ_ANNOTATIONS)
        if observation and observation.strip():
            return observation
    if content.strip() and tag is RiskTag.TRANSACTION_ATOMICITY:
        observation = _scoped_annotation(dossier, content, ("Transactional",))
        if observation and observation.strip():
            return observation
    return None
