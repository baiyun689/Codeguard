"""受控模式的工具证明能力注册表。

Prompt、PlanValidator 和 ProofMatcher 共用这里的声明，避免工具描述在多处
漂移。注册表只描述事实能力，不做问题判断。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolProofContract:
    name: str
    can_prove: tuple[str, ...]
    cannot_prove: tuple[str, ...]
    required_arguments: tuple[str, ...]
    path_kinds: tuple[str, ...] = ()


TOOL_PROOF_CONTRACTS: dict[str, ToolProofContract] = {
    "get_file_content": ToolProofContract(
        name="get_file_content",
        can_prove=(
            "local source mechanism",
            "local control flow and conditions",
            "state reads and writes",
        ),
        cannot_prove=(
            "cross-file reachability",
            "caller or callee existence",
            "runtime path absence",
        ),
        required_arguments=("symbol_id",),
    ),
    "inspect_structure": ToolProofContract(
        name="inspect_structure",
        can_prove=(
            "one-hop structural relationships",
            "inheritance and interface implementation",
            "fields and direct coupling",
        ),
        cannot_prove=(
            "multi-hop behavior paths",
            "execution order",
            "runtime reachability",
        ),
        required_arguments=("symbol_id",),
    ),
    "inspect_path": ToolProofContract(
        name="inspect_path",
        can_prove=(
            "bounded downstream relationship facts",
            "behavior complete paths when path_kind=behavior",
            "sensitive call hits when path_kind=security",
        ),
        cannot_prove=(
            "path absence",
            "upstream callers",
            "complete security data-flow or parameter propagation",
            "business impact by itself",
        ),
        required_arguments=("symbol_id", "path_kind", "max_depth"),
        path_kinds=("behavior", "security"),
    ),
    "inspect_change_impact": ToolProofContract(
        name="inspect_change_impact",
        can_prove=(
            "bounded upstream callers",
            "framework entry points",
            "reverse impact paths",
        ),
        cannot_prove=(
            "downstream propagation",
            "sink reachability",
            "path absence",
        ),
        required_arguments=("symbol_id",),
    ),
}


def get_tool_proof_contract(tool: str) -> ToolProofContract | None:
    """按工具名返回唯一证明契约。"""

    return TOOL_PROOF_CONTRACTS.get(tool)


__all__ = ["TOOL_PROOF_CONTRACTS", "ToolProofContract", "get_tool_proof_contract"]
