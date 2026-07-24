"""候选主张的证据主题术语表。

该词典只描述候选语义，不复用也不依赖 diff 风险命中规则。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from codeguard_agent.models.tasks import RiskTag


def normalize_candidate_text(value: str) -> str:
    """把候选文本和术语统一到稳定的匹配形式。"""
    normalized = unicodedata.normalize("NFKC", value).lower()
    normalized = normalized.replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", normalized).strip()


@dataclass(frozen=True)
class TagTerms:
    exact_type_aliases: frozenset[str]
    strong_phrases: frozenset[str]
    weak_terms: frozenset[str]


def _terms(
    exact_type_aliases: set[str],
    strong_phrases: set[str],
    weak_terms: set[str],
) -> TagTerms:
    return TagTerms(
        exact_type_aliases=frozenset(
            normalize_candidate_text(item) for item in exact_type_aliases
        ),
        strong_phrases=frozenset(
            normalize_candidate_text(item) for item in strong_phrases
        ),
        weak_terms=frozenset(normalize_candidate_text(item) for item in weak_terms),
    )


CANDIDATE_TAG_TERMS: dict[RiskTag, TagTerms] = {
    RiskTag.AUTHORIZATION: _terms(
        {"越权", "authorization", "access control"},
        {"鉴权", "授权", "越权", "resource ownership", "permission check"},
        {"权限", "role", "owner"},
    ),
    RiskTag.AUTHENTICATION_SESSION: _terms(
        {"认证绕过", "authentication", "session security"},
        {"认证", "登录", "会话", "token", "session", "credential"},
        {"凭据", "有效期", "撤销"},
    ),
    RiskTag.WEB_SECURITY_CONFIG: _terms(
        {"web security config", "csrf", "cors"},
        {"csrf", "cors", "permitall", "security chain", "安全配置"},
        {"公开路由", "跨域"},
    ),
    RiskTag.INPUT_VALIDATION: _terms(
        {"输入校验", "input validation", "validation"},
        {"输入校验", "参数校验", "validation", "untrusted input"},
        {"格式", "范围", "约束"},
    ),
    RiskTag.INJECTION: _terms(
        {"注入", "injection", "sql injection"},
        {"注入", "sql injection", "命令注入", "拼接查询", "动态表达式"},
        {"拼接", "转义", "sink"},
    ),
    RiskTag.SQL_DATA_ACCESS: _terms(
        {"sql 数据访问", "sql data access", "repository"},
        {"sql", "查询条件", "tenant filter", "n+1", "mapper", "repository"},
        {"查询", "分页", "索引"},
    ),
    RiskTag.FILE_PATH_IO: _terms(
        {"路径穿越", "path traversal", "file io"},
        {"路径穿越", "文件读写", "canonical path", "upload", "download"},
        {"路径", "文件", "扩展名"},
    ),
    RiskTag.SSRF_OUTBOUND: _terms(
        {"ssrf", "server side request forgery", "outbound request"},
        {"ssrf", "外部请求", "host allowlist", "redirect", "可控 url"},
        {"url", "host", "出站"},
    ),
    RiskTag.CONFIG_SECURITY: _terms(
        {"不安全配置", "config security", "insecure config"},
        {"密钥", "默认密码", "debug", "配置泄露", "insecure config"},
        {"配置", "环境变量", "开关"},
    ),
    RiskTag.DATA_EXPOSURE: _terms(
        {"数据泄露", "data exposure", "sensitive data exposure"},
        {"敏感数据", "日志泄露", "返回过量", "mask", "pii"},
        {"脱敏", "响应", "日志"},
    ),
    RiskTag.DESERIALIZATION: _terms(
        {"反序列化", "deserialization", "insecure deserialization"},
        {"反序列化", "objectinputstream", "readobject", "xstream", "kryo", "xml serialization"},
        {"类型白名单", "校验", "readresolve"},
    ),
    RiskTag.TRANSACTION_ATOMICITY: _terms(
        {"事务原子性", "transaction atomicity", "partial write"},
        {"事务", "原子性", "回滚", "部分写入", "transaction"},
        {"写入", "补偿", "提交"},
    ),
    RiskTag.CONCURRENCY_CONSISTENCY: _terms(
        {"并发一致性", "race condition", "concurrency"},
        {"并发", "竞态", "锁", "atomic", "共享状态", "race"},
        {"线程", "版本", "同步"},
    ),
    RiskTag.IDEMPOTENCY_RETRY: _terms(
        {"幂等性", "idempotency", "retry safety"},
        {"幂等", "重复提交", "重试", "duplicate", "retry"},
        {"去重", "唯一键", "重复"},
    ),
    RiskTag.CACHE_CONSISTENCY: _terms(
        {"缓存一致性", "cache consistency", "stale cache"},
        {"缓存", "失效", "stale", "evict", "cache consistency"},
        {"cache", "版本", "陈旧"},
    ),
    RiskTag.MESSAGE_DELIVERY: _terms(
        {"消息投递", "message delivery", "messaging"},
        {"消息", "投递", "消费", "ack", "kafka", "rabbit", "dlq"},
        {"outbox", "重放", "队列"},
    ),
    RiskTag.ERROR_HANDLING: _terms(
        {"错误处理", "error handling", "exception handling"},
        {"异常", "吞异常", "错误码", "恢复", "exception handling"},
        {"catch", "传播", "记录"},
    ),
    RiskTag.NULL_STATE_SAFETY: _terms(
        {"空指针", "npe", "null pointer"},
        {"空指针", "null", "未初始化", "nullable", "optional"},
        {"为空", "判空", "非空"},
    ),
    RiskTag.RESOURCE_LIFECYCLE: _terms(
        {"资源泄漏", "resource lifecycle", "resource leak"},
        {"资源泄漏", "关闭", "释放", "try with resources", "lifecycle"},
        {"close", "finally", "句柄"},
    ),
    RiskTag.API_CONTRACT: _terms(
        {"接口契约", "api contract", "breaking change"},
        {"接口契约", "兼容性", "请求响应", "版本", "breaking change"},
        {"签名", "字段", "调用方"},
    ),
    RiskTag.PERFORMANCE: _terms(
        {"性能问题", "performance", "slow query"},
        {"性能", "慢查询", "循环 i/o", "内存", "分页", "复杂度"},
        {"批处理", "缓存", "耗时"},
    ),
    RiskTag.COMPLEXITY_CONTROL_FLOW: _terms(
        {"控制流复杂度", "cyclomatic complexity", "complex control flow"},
        {"圈复杂度", "分支", "嵌套", "控制流", "过长方法"},
        {"早返回", "提取方法", "路径"},
    ),
    RiskTag.DUPLICATION_DESIGN: _terms(
        {"重复代码", "duplication", "copy paste"},
        {"重复代码", "复制粘贴", "共享抽象", "duplication"},
        {"抽象", "漂移", "复用"},
    ),
    RiskTag.OBSERVABILITY_TESTABILITY: _terms(
        {"可观测性", "observability", "testability"},
        {"结构化日志", "指标", "追踪", "可测试性", "mock", "observability"},
        {"日志", "trace", "seam"},
    ),
}


__all__ = ["CANDIDATE_TAG_TERMS", "TagTerms", "normalize_candidate_text"]
