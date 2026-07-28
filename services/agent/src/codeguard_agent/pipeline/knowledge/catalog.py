"""文件系统 Knowledge Catalog：发现、读取、校验 fragment 文件。

只负责 I/O 和基本校验，不负责选择逻辑。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from codeguard_agent.models.knowledge import KnowledgeFragment, KnowledgeKind
from codeguard_agent.models.tasks import ReviewerKind, RiskTag

logger = logging.getLogger("codeguard")

_KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "prompts" / "knowledge"

# 每个 fragment 的专用检索词：用于 patch semantics 匹配
_SELECTION_TERMS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "AUTHORIZATION": (
        ("authorization", "haspermission", "preauthorize", "secured", "rolesallowed", "鉴权", "授权", "越权"),
        ("permission", "role", "access", "owner", "权限"),
    ),
    "AUTHENTICATION_SESSION": (
        ("authentication", "session", "login", "logout", "credential", "token", "认证", "会话"),
        ("password", "jwt", "oauth", "凭据", "登录"),
    ),
    "WEB_SECURITY_CONFIG": (
        ("csrf", "cors", "xss", "securityfilterchain", "permitall", "安全配置"),
        ("filter", "interceptor", "公开"),
    ),
    "INPUT_VALIDATION": (
        ("valid", "validate", "sanitize", "escape", "校验", "参数校验"),
        ("notnull", "notblank", "notempty", "约束", "格式"),
    ),
    "INJECTION": (
        ("injection", "注入", "拼接查询", "动态sql", "命令注入", "exec", "runtime"),
        ("拼接", "转义", "参数化"),
    ),
    "SQL_DATA_ACCESS": (
        ("sql", "jdbc", "jdbctemplate", "mybatis", "jpa", "repository", "query"),
        ("查询", "分页", "索引", "n+1"),
    ),
    "FILE_PATH_IO": (
        ("file", "path", "upload", "download", "traversal", "文件", "路径"),
        ("read", "write", "stream", "扩展名"),
    ),
    "SSRF_OUTBOUND": (
        ("ssrf", "url", "httpclient", "resttemplate", "webclient", "outbound"),
        ("request", "fetch", "connect"),
    ),
    "CONFIG_SECURITY": (
        ("config", "secret", "key", "password", "credential", "配置"),
        ("properties", "yaml", "env"),
    ),
    "DATA_EXPOSURE": (
        ("expose", "leak", "serialize", "json", "log", "泄露", "暴露"),
        ("sensitive", "pii", "mask", "脱敏"),
    ),
    "DESERIALIZATION": (
        ("deserialize", "objectinputstream", "readobject", "反序列化"),
        ("serialize", "byte", "stream"),
    ),
    "TRANSACTION_ATOMICITY": (
        ("transactional", "transaction", "commit", "rollback", "事务"),
        ("save", "update", "delete", "persist"),
    ),
    "CONCURRENCY_CONSISTENCY": (
        ("synchronized", "lock", "concurrent", "atomic", "volatile", "并发", "竞态"),
        ("thread", "race", "mutex"),
    ),
    "IDEMPOTENCY_RETRY": (
        ("idempotent", "retry", "duplicate", "幂等", "重试"),
        ("repeat", "resubmit", "去重"),
    ),
    "CACHE_CONSISTENCY": (
        ("cache", "caffeine", "redis", "ehcache", "缓存"),
        ("evict", "ttl", "invalidate"),
    ),
    "MESSAGE_DELIVERY": (
        ("message", "event", "kafka", "rabbit", "publish", "消息", "事件"),
        ("subscribe", "consume", "queue", "topic"),
    ),
    "ERROR_HANDLING": (
        ("catch", "exception", "error", "throw", "异常", "错误处理"),
        ("try", "finally", "suppress"),
    ),
    "NULL_STATE_SAFETY": (
        ("null", "optional", "nullable", "空指针"),
        ("check", "requirenonnull", "isnull"),
    ),
    "RESOURCE_LIFECYCLE": (
        ("close", "dispose", "try-with-resources", "leak", "资源", "释放"),
        ("stream", "connection", "statement", "resultset"),
    ),
    "API_CONTRACT": (
        ("api", "contract", "interface", "deprecated", "breaking", "接口", "契约"),
        ("version", "compatibility", "signature"),
    ),
    "PERFORMANCE": (
        ("performance", "slow", "optimize", "n+1", "性能"),
        ("loop", "batch", "lazy", "eager"),
    ),
    "COMPLEXITY_CONTROL_FLOW": (
        ("complex", "nesting", "cyclomatic", "复杂度"),
        ("if", "else", "switch", "loop"),
    ),
    "DUPLICATION_DESIGN": (
        ("duplicate", "duplication", "reuse", "重复"),
        ("extract", "refactor", "common"),
    ),
    "OBSERVABILITY_TESTABILITY": (
        ("log", "metric", "trace", "test", "mock", "可测试", "可观测"),
        ("debug", "monitor", "assert"),
    ),
}


def _build_fragment_id(reviewer: ReviewerKind, topic: str, kind: KnowledgeKind) -> str:
    return f"{reviewer.value}/{kind.value}/{topic}"


def _parse_risk_tag(tag_str: str) -> RiskTag | None:
    try:
        return RiskTag(tag_str)
    except ValueError:
        return None


class KnowledgeCatalog:
    """文件系统 Knowledge Catalog。

    发现和读取 fragment 文件，不负责选择。"""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or _KNOWLEDGE_DIR

    # ── public API ──

    def base_fragment(self, reviewer: ReviewerKind) -> KnowledgeFragment | None:
        """读取 reviewer 的 BASE.txt，返回 None 当文件不存在或为空。"""
        path = self._root / reviewer.value / "BASE.txt"
        return self._read_fragment(path, reviewer, KnowledgeKind.BASE, "BASE", None)

    def specialized_fragments(
        self, reviewer: ReviewerKind,
    ) -> Sequence[KnowledgeFragment]:
        """返回 reviewer 目录下所有非 BASE 的专门 fragment，按 RiskTag 稳定排序。"""
        domain_dir = self._root / reviewer.value
        if not domain_dir.is_dir():
            return ()
        fragments: list[KnowledgeFragment] = []
        for path in sorted(domain_dir.iterdir(), key=lambda p: p.name):
            if not path.is_file() or path.suffix != ".txt":
                continue
            topic = path.stem
            if topic == "BASE":
                continue
            tag = _parse_risk_tag(topic)
            if tag is None:
                continue
            fragment = self._read_fragment(path, reviewer, KnowledgeKind.SPECIALIZED, topic, tag)
            if fragment is not None:
                fragments.append(fragment)
        return tuple(fragments)

    # ── internal ──

    def _read_fragment(
        self,
        path: Path,
        reviewer: ReviewerKind,
        kind: KnowledgeKind,
        topic: str,
        tag: RiskTag | None,
    ) -> KnowledgeFragment | None:
        if not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8").strip()
        except Exception:
            logger.warning("Knowledge fragment read failed: %s", path, exc_info=True)
            return None
        if not content:
            return None
        strong, weak = _SELECTION_TERMS.get(topic, ((), ()))
        return KnowledgeFragment(
            fragment_id=_build_fragment_id(reviewer, topic, kind),
            reviewer=reviewer,
            kind=kind,
            topic=topic,
            risk_tag=tag,
            content=content,
            source_path=str(path),
            strong_terms=strong,
            weak_terms=weak,
        )
