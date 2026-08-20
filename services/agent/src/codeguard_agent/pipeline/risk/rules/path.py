"""Weak path-role signals for deterministic risk triage.

角色 → 标签映射统一收敛在 roles.py 的单一注册表(与知识选择的
file-role 评分共用)。A path role is context, not a finding——
path signals 只给已命中的具体标签加权,绝不单独产出结论。
"""

from __future__ import annotations

from collections.abc import Iterable

from codeguard_agent.models.tasks import RiskSignal, RiskTag
from codeguard_agent.pipeline.risk.rules.features import DiffFeatures
from codeguard_agent.pipeline.risk.rules.roles import matching_roles
from codeguard_agent.pipeline.risk.signals import make_risk_signal


def path_signals(
    features: DiffFeatures, concrete_tags: Iterable[RiskTag]
) -> list[RiskSignal]:
    """Return weak path evidence only for already matched concrete tags."""
    concrete = set(concrete_tags)
    signals: list[RiskSignal] = []
    for spec in matching_roles(features.path):
        for tag in spec.tags:
            if tag in concrete:
                signals.append(
                    make_risk_signal(
                        tag=tag,
                        priority=1,
                        source_kind="path",
                        source=f"path:{spec.role}",
                        reason=f"文件路径角色 {spec.role} 与该风险方向相关，作为弱证据加权",
                    )
                )
    return signals
