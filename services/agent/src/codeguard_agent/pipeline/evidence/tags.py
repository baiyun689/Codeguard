"""证据链标签分组常量:安全敏感与维护性标签集合(ADR-046)。

自 verifier 抽出为叶子模块:verifier 与 guard_scan 都从这里导入,
避免两模块互相导入形成部分初始化循环(Task 6 让 verifier 顶层导入 guard_scan)。
"""

from __future__ import annotations

from codeguard_agent.models.tasks import RiskTag

# 配方开关:安全敏感标签加安全路径,维护性标签加结构指标(确定性,零 LLM)。
SECURITY_TAGS = frozenset({
    RiskTag.AUTHORIZATION, RiskTag.AUTHENTICATION_SESSION,
    RiskTag.WEB_SECURITY_CONFIG, RiskTag.INPUT_VALIDATION,
    RiskTag.INJECTION, RiskTag.SQL_DATA_ACCESS, RiskTag.FILE_PATH_IO,
    RiskTag.SSRF_OUTBOUND, RiskTag.CONFIG_SECURITY, RiskTag.DATA_EXPOSURE,
    RiskTag.DESERIALIZATION,
})
MAINTAINABILITY_TAGS = frozenset({
    RiskTag.COMPLEXITY_CONTROL_FLOW, RiskTag.DUPLICATION_DESIGN,
    RiskTag.OBSERVABILITY_TESTABILITY,
})
