"""统一渲染带命名占位符的 System/User Prompt 模板。"""

from __future__ import annotations

import re
from collections.abc import Mapping

_PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


def render_prompt_template(
    template: str,
    values: Mapping[str, str],
) -> str:
    """严格替换 Prompt 模板，占位符缺失或多余时直接失败。"""
    placeholders = set(_PLACEHOLDER.findall(template))
    provided = set(values)
    missing = placeholders - provided
    extra = provided - placeholders
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing={sorted(missing)}")
        if extra:
            details.append(f"extra={sorted(extra)}")
        raise ValueError("Prompt template values mismatch: " + ", ".join(details))

    rendered = _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)
    unresolved = _PLACEHOLDER.findall(rendered)
    if unresolved:
        raise ValueError(f"Unresolved Prompt placeholders: {sorted(set(unresolved))}")
    return rendered
