"""确定性 guard 注解扫描:把文件内容事实转成 direct contradicts 先验(ADR-046)。

只覆盖两个确定场景:@PreAuthorize 等鉴权注解、@Transactional 事务边界。
命中产出 direct 反证,供门控①零成本淘汰;未命中不产出(不抢关系分析的活)。
"""

from __future__ import annotations

import re

from codeguard_agent.models.council import CandidateFact, FactRelation
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence.verifier import SECURITY_TAGS

_AUTHZ_ANNOTATIONS = ("PreAuthorize", "PostAuthorize", "Secured", "RolesAllowed")

# 以下四个私有函数自 agent.py 迁移(_strip_comments_and_strings / _resolved_method /
# _matching_brace / _scoped_annotation):文本扫描部分语义逐行保留;
# _resolved_method/_scoped_annotation 去掉 dossier 依赖,改为直扫文件内容
# (无 AST symbol_context 可用,仅需标准库 re,无需 json)。


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


_METHOD_DECL = re.compile(r"\b(\w+)\s*\(")


def _resolved_method(source: str) -> tuple[str, int, int] | None:
    """从文件内容解析首个方法声明(文本直扫版,无 dossier 上下文)。

    原 agent.py 版依赖 AST symbol_context 解析方法名与行区间;本模块只有
    文件内容事实,退化为扫描整份内容中首个方法声明行,行区间覆盖全文件。
    """
    lines = _strip_comments_and_strings(source).splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("@"):
            continue
        match = _METHOD_DECL.search(line)
        if match is not None:
            return match.group(1), index, len(lines)
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
    source: str,
    annotation_names: tuple[str, ...],
) -> str | None:
    """在文件内容中确定性扫描方法声明块与所属类声明块上的目标注解。

    原 agent.py 版以 dossier 定位待审方法并用 AST 签名先行检查;本模块退化为
    文本直扫:先找首个方法声明行,检查其声明块(含上方注解行)是否含目标注解,
    未命中再检查该方法所属类声明的注解块。
    """
    resolved = _resolved_method(source)
    if resolved is None:
        return None
    method_name, start, end = resolved
    annotation_pattern = re.compile(
        r"@(" + "|".join(re.escape(name) for name in annotation_names) + r")\b"
    )
    # AST 签名检查需要 dossier 上下文,文本直扫版跳过,直接扫声明块。

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
    # 声明块向上含注解行(与 AST start_line 含注解的语义对齐)。
    while start_index > 0:
        previous = lines[start_index - 1].strip()
        if not previous or previous.startswith("@") or previous.endswith(")"):
            start_index -= 1
            continue
        break
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


def scan_guard_fact(
    fact: CandidateFact, tag: RiskTag,
) -> FactRelation | None:
    """在文件内容事实中确定性扫描 guard 注解;命中返回 direct contradicts,否则 None。"""
    if fact.raw.strip() and tag in SECURITY_TAGS:
        observation = _scoped_annotation(fact.raw, _AUTHZ_ANNOTATIONS)
        if observation is not None:
            return FactRelation(
                fact_id=fact.fact_id,
                relation="contradicts",
                strength="direct",
                observation=observation,
            )
    if fact.raw.strip() and tag is RiskTag.TRANSACTION_ATOMICITY:
        observation = _scoped_annotation(fact.raw, ("Transactional",))
        if observation is not None:
            return FactRelation(
                fact_id=fact.fact_id,
                relation="contradicts",
                strength="direct",
                observation=observation,
            )
    return None
