"""配置加载。

阶段 1 保持极简:所有配置从环境变量读取(可配合 .env 文件)。
后续阶段需要更复杂的配置(YAML、多层覆盖)时再演进,现在不要过度设计。
"""

from __future__ import annotations
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("codeguard")
_DEFAULT_MODELS = {"openai": "gpt-4o-mini", "claude": "claude-sonnet-4-20250514"}


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}")
    return value


def _nonnegative_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {raw!r}")
    return value


def _load_dotenv() -> None:
    """从项目里就近向上查找并加载 .env 文件。

    设计要点:
    - override=False:已显式设置的环境变量优先于 .env,方便临时覆盖。
    - 没装 python-dotenv 时静默跳过,不影响"纯环境变量"用法。
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return
    load_dotenv(find_dotenv(usecwd=True), override=False)


@dataclass
class Settings:
    """运行时配置。"""

    provider: str
    model: str
    api_key: str
    api_base_url: str
    max_retries: int
    structured_method: str
    disable_thinking: bool
    llm_timeout_seconds: int = 60
    tool_server_url: str = ""
    tool_server_token: str = ""
    graph_build_timeout_seconds: int = 120
    evidence_mode: str = "full"
    max_review_tasks: int = 100
    max_tasks_per_file: int = 10
    checkpoint_backend: str = ""
    checkpoint_db: str = "codeguard_checkpoints.db"
    reasoning_effort: str = ""
    trace_enabled: bool = False
    trace_dir: str = "trace"
    trace_max_llm_content: int = 0
    discovery_mode: str = "controlled"
    controlled_max_path_depth: int = 3
    controlled_execute_concurrency: int = 3
    controlled_subtask_max_tool_calls: int = 8
    controlled_subtask_max_rounds: int = 6
    controlled_subtask_timeout_seconds: int = 120
    controlled_task_max_tool_calls: int = 32
    controlled_max_subtasks_per_task: int = 8

    @property
    def needs_api_key(self) -> bool:
        """是否为需要真实 API 密钥的 provider(mock 不需要)。"""
        return self.provider in _DEFAULT_MODELS

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量构造配置(会先就近加载 .env 文件)。

        provider 默认 'openai':开箱即用调真实 API。
        想零成本验证流水线连通时,可显式设 CODEGUARD_PROVIDER=mock 走假数据分支。
        """
        _load_dotenv()
        provider = os.environ.get("CODEGUARD_PROVIDER", "openai").strip().lower()
        model = os.environ.get("CODEGUARD_MODEL", "").strip() or _DEFAULT_MODELS.get(
            provider, ""
        )
        structured_method = os.environ.get(
            "CODEGUARD_STRUCTURED_METHOD", "function_calling"
        ).strip()
        disable_thinking = os.environ.get(
            "CODEGUARD_DISABLE_THINKING", "false"
        ).strip().lower() in ("1", "true", "yes", "on")
        evidence_mode = (
            os.environ.get("CODEGUARD_EVIDENCE_MODE", "full").strip().lower()
        )
        if evidence_mode not in ("full", "off"):
            logger.warning(
                "未知 CODEGUARD_EVIDENCE_MODE '%s',回退 'full'", evidence_mode
            )
            evidence_mode = "full"
        max_review_tasks = _positive_int_env("CODEGUARD_MAX_REVIEW_TASKS", 100)
        max_tasks_per_file = _positive_int_env("CODEGUARD_MAX_TASKS_PER_FILE", 10)
        graph_build_timeout_seconds = _positive_int_env(
            "CODEGUARD_GRAPH_BUILD_TIMEOUT_SECONDS", 120
        )
        checkpoint_backend = (
            os.environ.get("CODEGUARD_CHECKPOINT_BACKEND", "").strip().lower()
        )
        checkpoint_db = os.environ.get(
            "CODEGUARD_CHECKPOINT_DB", "codeguard_checkpoints.db"
        ).strip()
        reasoning_effort = (
            os.environ.get("CODEGUARD_REASONING_EFFORT", "").strip().lower()
        )
        if reasoning_effort not in ("high", "max"):
            reasoning_effort = ""
        trace_enabled = os.environ.get(
            "CODEGUARD_TRACE_ENABLED", "false"
        ).strip().lower() not in ("0", "false", "no", "off")
        trace_dir = os.environ.get("CODEGUARD_TRACE_DIR", "trace").strip()
        trace_max_llm_content = int(
            os.environ.get("CODEGUARD_TRACE_MAX_LLM_CONTENT", "0")
        )
        discovery_mode = (
            os.environ.get("CODEGUARD_DISCOVERY_MODE", "controlled").strip().lower()
        )
        if discovery_mode not in {"controlled", "direct"}:
            logger.warning(
                "未知 CODEGUARD_DISCOVERY_MODE '%s',回退 'controlled'", discovery_mode
            )
            discovery_mode = "controlled"
        controlled_max_path_depth = _positive_int_env(
            "CODEGUARD_CONTROLLED_MAX_PATH_DEPTH", 3
        )
        if controlled_max_path_depth > 3:
            raise ValueError("CODEGUARD_CONTROLLED_MAX_PATH_DEPTH must be <= 3")
        controlled_execute_concurrency = _positive_int_env(
            "CODEGUARD_CONTROLLED_EXECUTE_CONCURRENCY", 3
        )
        controlled_subtask_max_tool_calls = _nonnegative_int_env(
            "CODEGUARD_CONTROLLED_SUBTASK_MAX_TOOL_CALLS", 8
        )
        controlled_subtask_max_rounds = _positive_int_env(
            "CODEGUARD_CONTROLLED_SUBTASK_MAX_ROUNDS", 6
        )
        controlled_subtask_timeout_seconds = _positive_int_env(
            "CODEGUARD_CONTROLLED_SUBTASK_TIMEOUT_SECONDS", 120
        )
        controlled_task_max_tool_calls = _nonnegative_int_env(
            "CODEGUARD_CONTROLLED_TASK_MAX_TOOL_CALLS", 32
        )
        controlled_max_subtasks_per_task = _nonnegative_int_env(
            "CODEGUARD_CONTROLLED_MAX_SUBTASKS_PER_TASK", 8
        )
        return cls(
            provider=provider,
            model=model,
            api_key=os.environ.get("CODEGUARD_API_KEY", "").strip(),
            api_base_url=os.environ.get("CODEGUARD_API_BASE_URL", "").strip(),
            max_retries=int(os.environ.get("CODEGUARD_MAX_RETRIES", "3")),
            llm_timeout_seconds=_positive_int_env("CODEGUARD_LLM_TIMEOUT_SECONDS", 60),
            structured_method=structured_method,
            disable_thinking=disable_thinking,
            tool_server_url=os.environ.get("CODEGUARD_TOOL_SERVER_URL", "").strip(),
            tool_server_token=os.environ.get("CODEGUARD_TOOL_SERVER_TOKEN", "").strip(),
            graph_build_timeout_seconds=graph_build_timeout_seconds,
            evidence_mode=evidence_mode,
            max_review_tasks=max_review_tasks,
            max_tasks_per_file=max_tasks_per_file,
            checkpoint_backend=checkpoint_backend,
            checkpoint_db=checkpoint_db,
            reasoning_effort=reasoning_effort,
            trace_enabled=trace_enabled,
            trace_dir=trace_dir,
            trace_max_llm_content=trace_max_llm_content,
            discovery_mode=discovery_mode,
            controlled_max_path_depth=controlled_max_path_depth,
            controlled_execute_concurrency=controlled_execute_concurrency,
            controlled_subtask_max_tool_calls=controlled_subtask_max_tool_calls,
            controlled_subtask_max_rounds=controlled_subtask_max_rounds,
            controlled_subtask_timeout_seconds=controlled_subtask_timeout_seconds,
            controlled_task_max_tool_calls=controlled_task_max_tool_calls,
            controlled_max_subtasks_per_task=controlled_max_subtasks_per_task,
        )

    @classmethod
    def judge_from_env(cls) -> "Settings":
        """评测裁判模型的配置:优先读 CODEGUARD_JUDGE_*,未设则回退主 CODEGUARD_*。

        评测应尽量用与被测审查器**不同/更强**的模型当裁判,降低"自己评自己"的偏差
        (见 DECISIONS.md ADR-005)。典型用法:审查器用 DeepSeek,裁判另配一家:
            CODEGUARD_JUDGE_PROVIDER=claude
            CODEGUARD_JUDGE_MODEL=claude-sonnet-4-20250514
            CODEGUARD_JUDGE_API_KEY=sk-ant-...
        只设了 JUDGE_PROVIDER 而没给 MODEL 时,回退到该 provider 的默认模型。

        注意"同端点"而非"同 provider":DeepSeek 和通义千问都借 `provider=openai` 这条路,
        但 base_url 不同、是两家厂商。只有 provider **且** base_url 都与主配置一致时,才算同一个
        端点、才沿用主配置的密钥/地址/thinking 开关;否则密钥必须单独给,thinking 默认关
        (那个 `disable_thinking` 的 extra_body 是 DeepSeek 专用,塞给千问会出错)。
        """
        base = cls.from_env()
        provider = (
            os.environ.get("CODEGUARD_JUDGE_PROVIDER", "").strip().lower()
            or base.provider
        )
        api_key = os.environ.get("CODEGUARD_JUDGE_API_KEY", "").strip()
        api_base_url = os.environ.get("CODEGUARD_JUDGE_API_BASE_URL", "").strip()
        same_endpoint = provider == base.provider and api_base_url in (
            "",
            base.api_base_url,
        )
        model = os.environ.get("CODEGUARD_JUDGE_MODEL", "").strip()
        if not model:
            model = (
                base.model
                if same_endpoint
                else _DEFAULT_MODELS.get(provider, base.model)
            )
        if same_endpoint:
            api_key = api_key or base.api_key
            api_base_url = api_base_url or base.api_base_url
        explicit_dt = (
            os.environ.get("CODEGUARD_JUDGE_DISABLE_THINKING", "").strip().lower()
        )
        if explicit_dt:
            disable_thinking = explicit_dt in ("1", "true", "yes", "on")
        else:
            disable_thinking = base.disable_thinking if same_endpoint else False
        return cls(
            provider=provider,
            model=model,
            api_key=api_key,
            api_base_url=api_base_url,
            max_retries=base.max_retries,
            llm_timeout_seconds=base.llm_timeout_seconds,
            structured_method=base.structured_method,
            disable_thinking=disable_thinking,
        )
