# Phase 2 Risk Triage and Reviewer Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不增加主 ReviewState 字段、不改变 LangGraph 主拓扑的前提下，完成 23 个 Java/Spring 风险标签、RiskProfile 聚合、`GENERAL_REVIEW` 兜底、100/10 任务预算，以及 RiskTag 到三类审查员的实际 task-scoped 路由。

**Architecture:** `ReviewTask` 只携带 hunk/fallback diff，规则引擎从路径和 diff 文本变化方向生成 `RiskSignal`。RiskTriage 按语义化 `RiskTag` 聚合为一个 `RiskProfile`，TaskRank 按派生风险分数和预算选择 task，路由器再根据标签注册表计算每个固定 reviewer 节点的 task 范围。路径、增加、删除、修改是信号来源或变化方向，不是最终风险类别；AST、Java Gateway 和 LLM 不进入本阶段。

**Tech Stack:** Python 3.11+, Pydantic 2, LangGraph 1.x, pytest, ruff, mypy；规则只使用 Python 标准库正则和现有 `ReviewTask`。

## Global Constraints

- 不新增或改名 `ReviewState`、`ReviewTask`、`RiskProfile`、`TaskSelection` 的字段；reviewer 分派结果运行时派生，不增加 `assigned_reviewers`。
- Phase 2 不调用 LLM，不读取完整仓库，不调用 AST、调用图、RAG 或 Java Gateway。
- 风险标签是高召回路由信号，不是漏洞结论；EvidenceAgent、CouncilJudge 继续负责后续事实判断。
- 固定三路 `ThreatModelAgent`、`BehaviorAgent`、`MaintainabilityAgent` 仍保留；没有 task 的 reviewer 空运行并记录 trace，不从图中动态增删节点。
- 路径信号只能作为已有文本标签的弱加权和排序依据；没有具体文本信号时生成唯一 `GENERAL_REVIEW`。
- 统一预算默认值为 `max_tasks_to_review=100`、`max_tasks_per_file=10`；预算按 task 计数，不按命中标签数量计数。
- `Issue`、`ReviewResult`、`CandidateIssue` 产品契约不变；候选必须继续携带必填 `task_id`。
- 每个任务先写失败测试，再写最小实现；单元测试使用 `conda run -n codeguard --no-capture-output`。
- 不增加运行时依赖；规则文件只使用现有 Python 标准库、Pydantic 和项目内部模块。

## File Map

**新增:**

- `services/agent/src/codeguard_agent/pipeline/risk_rules/__init__.py`: 规则公共接口、分类结果和导出。
- `services/agent/src/codeguard_agent/pipeline/risk_rules/features.py`: 提取 path、added/deleted/changed 文本特征。
- `services/agent/src/codeguard_agent/pipeline/risk_rules/catalog.py`: 23 个规则元数据和 reviewer 映射。
- `services/agent/src/codeguard_agent/pipeline/risk_rules/security.py`: 安全方向规则。
- `services/agent/src/codeguard_agent/pipeline/risk_rules/behavior.py`: 行为、数据、一致性和资源规则。
- `services/agent/src/codeguard_agent/pipeline/risk_rules/maintainability.py`: 复杂度、重复、可观测性和可测试性规则。
- `services/agent/src/codeguard_agent/pipeline/risk_routing.py`: 从 RiskProfile 和 TaskSelection 派生 reviewer task scope。
- `services/agent/tests/test_risk_rule_features.py`: diff 变化方向测试。
- `services/agent/tests/test_risk_rules.py`: 规则、聚合、兜底和注册表测试。
- `services/agent/tests/test_risk_routing.py`: RiskTag 到 reviewer 的派生路由测试。
- `services/agent/tests/test_config_settings.py`: 预算环境变量测试。

**修改:**

- `services/agent/src/codeguard_agent/models/tasks.py`: 扩充 RiskTag，固定 Phase 2 预算默认值。
- `services/agent/src/codeguard_agent/pipeline/task_prep.py`: 接入规则分类结果和 TaskRank 预算算法。
- `services/agent/src/codeguard_agent/pipeline/graph.py`: 输出规则诊断 trace，按 scope 调用 reviewer，拒绝越界候选。
- `services/agent/src/codeguard_agent/pipeline/orchestrator.py`: 接收配置化 ReviewBudget，State 字段不变。
- `services/agent/src/codeguard_agent/config.py`、`cli.py`: 读取并传递预算。
- `services/agent/tests/test_tasks_models.py`、`test_task_prep.py`、`test_graph_orchestration.py`: 更新和增加回归测试。
- `.env.example`、`README.md`: 增加预算和路由说明。
- `DECISIONS.md`、`docs/ROADMAP.md`、`AGENTS.md`: 记录已落地边界、决策和复盘。
- `docs/superpowers/specs/2026-07-10-risk-routed-review-orchestration-design.md`: 更新 Phase 2 实施台账。

---

### Task 1: 固定风险枚举、预算和内部结果契约

**Files:**
- Modify: `services/agent/src/codeguard_agent/models/tasks.py`
- Modify: `services/agent/tests/test_tasks_models.py`
- Modify: `services/agent/tests/test_task_prep.py`

**Interfaces:**
- Produces 23 个具体 `RiskTag` 加 `GENERAL_REVIEW`。
- Produces `ReviewBudget(max_tasks_to_review=100, max_tasks_per_file=10, max_context_chars_per_task=None, max_final_issues=None)`。
- Keeps `RiskProfile(task_id, tag_scores, signals)` without `total_score` or reviewer fields.

- [ ] **Step 1: Write the failing contract tests**

把预算测试改为:

```python
def test_review_budget_defaults_to_phase2_limits():
    budget = ReviewBudget()
    assert budget.max_tasks_to_review == 100
    assert budget.max_tasks_per_file == 10
```

增加对以下 RiskTag 集合的精确断言:

```text
AUTHORIZATION, AUTHENTICATION_SESSION, WEB_SECURITY_CONFIG, INPUT_VALIDATION,
INJECTION, SQL_DATA_ACCESS, FILE_PATH_IO, SSRF_OUTBOUND, CONFIG_SECURITY,
DATA_EXPOSURE, TRANSACTION_ATOMICITY, CONCURRENCY_CONSISTENCY, IDEMPOTENCY_RETRY,
CACHE_CONSISTENCY, MESSAGE_DELIVERY, ERROR_HANDLING, NULL_STATE_SAFETY,
RESOURCE_LIFECYCLE, API_CONTRACT, PERFORMANCE, COMPLEXITY_CONTROL_FLOW,
DUPLICATION_DESIGN, OBSERVABILITY_TESTABILITY, GENERAL_REVIEW
```

- [ ] **Step 2: Run the focused tests and verify they fail**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_tasks_models.py -q
```

Expected: FAIL，因为当前枚举只有 Phase 1 的 11 个值且预算仍为 unlimited。

- [ ] **Step 3: Implement the minimal model changes**

扩充枚举，给两个预算字段设置 100/10 默认值，并对非 `None` 值要求大于 0。不要增加 reviewer、assignment 或 `total_score` 字段。

- [ ] **Step 4: Run the focused tests and verify they pass**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_tasks_models.py -q
```

Expected: PASS。

- [ ] **Step 5: Commit**

```powershell
git add services/agent/src/codeguard_agent/models/tasks.py services/agent/tests/test_tasks_models.py services/agent/tests/test_task_prep.py
git commit -m "feat(tasks): 固定 Phase 2 风险标签与预算契约"
```

### Task 2: 实现 Diff 文本变化方向特征

**Files:**
- Create: `services/agent/src/codeguard_agent/pipeline/risk_rules/features.py`
- Create: `services/agent/tests/test_risk_rule_features.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class DiffFeatures:
    path: str
    added_lines: tuple[tuple[int, str], ...]
    deleted_lines: tuple[str, ...]
    context_lines: tuple[str, ...]
    has_added: bool
    has_deleted: bool
    has_changed: bool

def extract_features(task: ReviewTask) -> DiffFeatures:
    raise NotImplementedError
```

`added_lines` 保存新文件行号和文本；删除行没有新文件行号。过滤 `@@`、`+++`、`---` 和 `\\ No newline at end of file` 元数据行。规则不调用 AST。

- [ ] **Step 1: Write the failing feature tests**

覆盖只有新增、只有删除、同一 hunk 同时删除和新增、路径来自 `ReviewTask.file` 四类输入，并断言 header 不进入匹配文本。

- [ ] **Step 2: Run the feature tests and verify they fail**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_risk_rule_features.py -q
```

Expected: FAIL，因为 `features.py` 不存在。

- [ ] **Step 3: Implement the deterministic extractor**

按 `task.patch.splitlines()` 单次扫描收集 added/deleted/context；新增行号沿用 hunk header 的新文件行号，删除行只保存文本。不要引入 diff 第三方依赖。

- [ ] **Step 4: Run the feature tests and verify they pass**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_risk_rule_features.py -q
```

Expected: PASS。

- [ ] **Step 5: Commit**

```powershell
git add services/agent/src/codeguard_agent/pipeline/risk_rules/features.py services/agent/tests/test_risk_rule_features.py
git commit -m "feat(risk-rules): 提取 diff 变化方向特征"
```

### Task 3: 实现安全方向规则

**Files:**
- Create: `services/agent/src/codeguard_agent/pipeline/risk_rules/security.py`
- Modify: `services/agent/tests/test_risk_rules.py`

**Interfaces:**

```python
detect_authorization(features: DiffFeatures) -> list[RiskSignal]
detect_authentication_session(features: DiffFeatures) -> list[RiskSignal]
detect_web_security_config(features: DiffFeatures) -> list[RiskSignal]
detect_input_validation(features: DiffFeatures) -> list[RiskSignal]
detect_injection(features: DiffFeatures) -> list[RiskSignal]
detect_file_path_io(features: DiffFeatures) -> list[RiskSignal]
detect_ssrf_outbound(features: DiffFeatures) -> list[RiskSignal]
detect_config_security(features: DiffFeatures) -> list[RiskSignal]
detect_data_exposure(features: DiffFeatures) -> list[RiskSignal]
```

- [ ] **Step 1: Add one failing Java/Spring hunk test per security tag**

| RiskTag | 最小观察模式 | 高风险变化 |
|---|---|---|
| `AUTHORIZATION` | `@PreAuthorize`, `@Secured`, `hasRole`, `hasAuthority` | 删除权限保护 |
| `AUTHENTICATION_SESSION` | `Authentication`, `SecurityContext`, `BearerToken`, `JSESSIONID`, `OAuth2` | 删除 token/session 校验 |
| `WEB_SECURITY_CONFIG` | `csrf`, `cors`, `permitAll`, `anonymous`, `Actuator` | 放宽全局 Web 配置 |
| `INPUT_VALIDATION` | `@Valid`, `@Validated`, `@NotNull`, `@NotBlank`, `BindingResult`, `isBlank` | 删除校验或边界检查 |
| `INJECTION` | SQL/命令/模板拼接，`Runtime.exec`, `ProcessBuilder`, `createNativeQuery`, `${...}` | 外部值进入 sink |
| `FILE_PATH_IO` | `Paths.get`, `new File`, `Files.*`, `FileInputStream`, `MultipartFile` | 路径或文件边界变化 |
| `SSRF_OUTBOUND` | `RestTemplate`, `WebClient`, `HttpClient`, `URL`, `URI`, `Feign` | 外部值参与 URL/URI |
| `CONFIG_SECURITY` | `password`, `secret`, `token`, `apiKey`, `@Value`, `application.yml` | 新增或暴露敏感配置 |
| `DATA_EXPOSURE` | `ResponseEntity`、DTO 返回、日志、`toString`、`password/token/email/phone` | 删除脱敏或新增敏感输出 |

每个 fixture 断言 tag、source 前缀、reason 关键词；删除信号不能伪造新文件行号。

- [ ] **Step 2: Run the focused tests and verify they fail**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_risk_rules.py -q
```

Expected: FAIL，因为 detector 尚未实现。

- [ ] **Step 3: Implement only the security detectors**

使用 added/deleted 文本的小写匹配。新增使用 `text:added:<rule_id>`，删除使用 `text:deleted:<rule_id>`，同一 hunk 前后组合使用 `text:changed:<rule_id>`。每个 detector 只生成自己的 canonical tag；路径不能单独生成标签。

- [ ] **Step 4: Run the focused tests and verify they pass**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_risk_rules.py -q
```

Expected: security fixtures PASS。

- [ ] **Step 5: Commit**

```powershell
git add services/agent/src/codeguard_agent/pipeline/risk_rules/security.py services/agent/tests/test_risk_rules.py
git commit -m "feat(risk-rules): 增加安全风险检测规则"
```

### Task 4: 实现行为和可维护性规则

**Files:**
- Create: `services/agent/src/codeguard_agent/pipeline/risk_rules/behavior.py`
- Create: `services/agent/src/codeguard_agent/pipeline/risk_rules/maintainability.py`
- Modify: `services/agent/tests/test_risk_rules.py`

**Interfaces:** 每个 detector 消费 `DiffFeatures`，返回只包含一个 canonical `RiskTag` 的 `list[RiskSignal]`。

- [ ] **Step 1: Add failing fixtures for every remaining tag**

行为规则必须覆盖 `SQL_DATA_ACCESS`（`@Query`、`JdbcTemplate`、`SELECT/UPDATE/DELETE`、`where`）、`TRANSACTION_ATOMICITY`（`@Transactional`、`save/update/delete`、`commit/rollback`）、`CONCURRENCY_CONSISTENCY`（`synchronized`、`Lock`、`Atomic`、`@Version`、条件更新）、`IDEMPOTENCY_RETRY`（`idempot`、`requestId`、`dedup`、`SETNX`、`@Retryable`）、`CACHE_CONSISTENCY`（`@Cacheable`、`@CacheEvict`、`RedisTemplate`）、`MESSAGE_DELIVERY`（`@KafkaListener`、`@RabbitListener`、`ack/nack`、`deadLetter`）、`ERROR_HANDLING`（`catch`、`throws`、`@ExceptionHandler`、空 catch）、`NULL_STATE_SAFETY`（null、`Optional`、`requireNonNull`、`orElse`）、`RESOURCE_LIFECYCLE`（`Connection`、`InputStream`、`ExecutorService`、`close/shutdown`）、`API_CONTRACT`（`@RequestMapping`、Controller 入参/返回值、DTO 字段）和 `PERFORMANCE`（循环内 IO/查询、`findAll`、`select *`、N+1、`sleep`）。

可维护性规则覆盖 `COMPLEXITY_CONTROL_FLOW`（嵌套控制流）、`DUPLICATION_DESIGN`（hunk 内重复非空语句/调用块）和 `OBSERVABILITY_TESTABILITY`（删除 logger/metric/audit/test 或新增副作用无观测）。每个标签至少一个新增、删除或变化 fixture，并断言不会误生成其他 canonical tag。

- [ ] **Step 2: Run focused tests and verify they fail**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_risk_rules.py -q
```

Expected: remaining-tag cases FAIL。

- [ ] **Step 3: Implement behavior and maintainability detectors**

按标签边界实现规则，不为每个 API 新增标签。事务关注多步写入和事务边界；并发关注共享状态、锁、版本或条件更新；幂等关注重复执行和去重保护。规则命中多个证据时保留多个 signal，不压成模糊标签。

- [ ] **Step 4: Run all rule tests and verify they pass**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_risk_rules.py -q
```

Expected: 23 个具体标签 fixture 全部 PASS。

- [ ] **Step 5: Commit**

```powershell
git add services/agent/src/codeguard_agent/pipeline/risk_rules/behavior.py services/agent/src/codeguard_agent/pipeline/risk_rules/maintainability.py services/agent/tests/test_risk_rules.py
git commit -m "feat(risk-rules): 增加行为与可维护性规则"
```

### Task 5: 建立规则注册表、聚合、兜底和诊断结果

**Files:**
- Create: `services/agent/src/codeguard_agent/pipeline/risk_rules/catalog.py`
- Create: `services/agent/src/codeguard_agent/pipeline/risk_rules/__init__.py`
- Modify: `services/agent/src/codeguard_agent/pipeline/task_prep.py`
- Modify: `services/agent/src/codeguard_agent/pipeline/graph.py`
- Modify: `services/agent/tests/test_risk_rules.py`
- Modify: `services/agent/tests/test_task_prep.py`
- Modify: `services/agent/tests/test_graph_orchestration.py`

**Interfaces:**

```python
RiskRule = Callable[[DiffFeatures], list[RiskSignal]]

@dataclass(frozen=True)
class RiskRuleSpec:
    rule_id: str
    tag: RiskTag
    reviewers: frozenset[str]
    detect: RiskRule

@dataclass(frozen=True)
class RuleDiagnostic:
    task_id: str
    rule_id: str
    detail: str

@dataclass(frozen=True)
class TriageResult:
    profiles: dict[str, RiskProfile]
    diagnostics: tuple[RuleDiagnostic, ...]
```

注册表一次且仅一次注册 23 个具体标签，reviewer 映射固定为:

```text
ThreatModelAgent = AUTHORIZATION, AUTHENTICATION_SESSION, WEB_SECURITY_CONFIG,
  INPUT_VALIDATION, INJECTION, FILE_PATH_IO, SSRF_OUTBOUND, CONFIG_SECURITY, DATA_EXPOSURE
BehaviorAgent = AUTHORIZATION, AUTHENTICATION_SESSION, INPUT_VALIDATION, INJECTION,
  SQL_DATA_ACCESS, FILE_PATH_IO, SSRF_OUTBOUND, DATA_EXPOSURE, TRANSACTION_ATOMICITY,
  CONCURRENCY_CONSISTENCY, IDEMPOTENCY_RETRY, CACHE_CONSISTENCY, MESSAGE_DELIVERY,
  ERROR_HANDLING, NULL_STATE_SAFETY, RESOURCE_LIFECYCLE, API_CONTRACT, PERFORMANCE
MaintainabilityAgent = RESOURCE_LIFECYCLE, API_CONTRACT, PERFORMANCE,
  COMPLEXITY_CONTROL_FLOW, DUPLICATION_DESIGN, OBSERVABILITY_TESTABILITY
GENERAL_REVIEW = 三路
```

- [ ] **Step 1: Add failing registry and aggregation tests**

断言注册表恰好覆盖 23 个标签；同一标签多 signal 聚合成一个分数项且上限为 5；added/deleted 证据同时保留；path-only 只有 `GENERAL_REVIEW`；有具体标签时不生成 fallback；单条规则异常不阻断其他规则并产生诊断。

- [ ] **Step 2: Run tests and verify they fail**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_risk_rules.py tests/test_task_prep.py tests/test_graph_orchestration.py -q
```

Expected: FAIL，因为当前 triage 返回空画像且没有注册表。

- [ ] **Step 3: Implement catalog and classifier**

按稳定注册顺序运行规则，按 `(tag, source, line, reason)` 去重，先确定 text signal 的 concrete tags，再并入同标签 path signal，按 tag 累加并 capped at 5。没有 concrete tag 时生成 `RiskSignal(tag=GENERAL_REVIEW, score=1, source="fallback:unclassified", reason="未命中已有风险规则，执行通用审查")`。

修改 `task_prep.triage_tasks` 返回 `TriageResult`；`_risk_triage_node` 写入 `result.profiles`，并把 diagnostics 转成 `CouncilTrace(node="risk_triage", event="rule_failed", detail=diagnostic.detail)`。不增加 State 字段，更新旧测试调用为 `triage_tasks(tasks).profiles`。

- [ ] **Step 4: Run tests and verify they pass**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_risk_rules.py tests/test_task_prep.py tests/test_graph_orchestration.py -q
```

Expected: PASS，现有图拓扑断言不变。

- [ ] **Step 5: Commit**

```powershell
git add services/agent/src/codeguard_agent/pipeline/risk_rules services/agent/src/codeguard_agent/pipeline/task_prep.py services/agent/src/codeguard_agent/pipeline/graph.py services/agent/tests/test_risk_rules.py services/agent/tests/test_task_prep.py services/agent/tests/test_graph_orchestration.py
git commit -m "feat(pipeline): 接入风险规则聚合与兜底"
```

### Task 6: 实现确定性 TaskRank 和大 diff 预算

**Files:**
- Modify: `services/agent/src/codeguard_agent/pipeline/task_prep.py`
- Modify: `services/agent/tests/test_task_prep.py`

**Interfaces:** 消费 `list[ReviewTask]`、`Mapping[str, RiskProfile]`、`ReviewBudget`，只产生现有 `TaskSelection`；total score 只能是局部派生值。

- [ ] **Step 1: Add failing ranking tests**

覆盖: 不超过 100 全选；超过总预算取前 100；单文件超过 10 时 reason 为 `per_file_limit`；总预算耗尽时 reason 为 `total_limit`；None 表示不限制；分数相同按稳定 task id；`GENERAL_REVIEW` 可参与预算但低于具体高风险标签。

- [ ] **Step 2: Run ranking tests and verify they fail**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_task_prep.py -q
```

Expected: FAIL，因为当前 `rank_tasks` 全选且不看预算。

- [ ] **Step 3: Implement the stable ranking algorithm**

使用确定性排序键:

```python
rank_key = (
    -max(profile.tag_scores.values(), default=0),
    -sum(profile.tag_scores.values()),
    -int(any(signal.score == 3 for signal in profile.signals)),
    -int(any(signal.source.startswith("text:deleted:") for signal in profile.signals)),
    -int(is_production_path(task.file)),
    task.id,
)
```

`is_production_path` 将 `src/main/`、非 test、非 docs、非 generated 文件视为生产代码。遍历排序结果，先检查总预算，再检查单文件计数；选择成功后更新计数器。跳过项保存 `risk_score=max(tag_scores, default=0)`。

- [ ] **Step 4: Run ranking tests and verify they pass**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_task_prep.py -q
```

Expected: PASS，fallback task 和 candidate mapping 回归测试仍通过。

- [ ] **Step 5: Commit**

```powershell
git add services/agent/src/codeguard_agent/pipeline/task_prep.py services/agent/tests/test_task_prep.py
git commit -m "feat(pipeline): 启用风险排序与大 diff 预算"
```

### Task 7: 接入预算配置并保持 State 契约不变

**Files:**
- Modify: `services/agent/src/codeguard_agent/config.py`
- Modify: `services/agent/src/codeguard_agent/cli.py`
- Modify: `services/agent/src/codeguard_agent/pipeline/orchestrator.py`
- Create: `services/agent/tests/test_config_settings.py`
- Modify: `.env.example`
- Modify: `README.md`

**Interfaces:**

```python
Settings.max_review_tasks: int = 100
Settings.max_tasks_per_file: int = 10
PipelineOrchestrator(enable_summary: bool = True, review_budget: ReviewBudget | None = None)
```

- [ ] **Step 1: Add failing Settings tests**

```python
def test_phase2_budget_defaults(monkeypatch):
    monkeypatch.delenv("CODEGUARD_MAX_REVIEW_TASKS", raising=False)
    monkeypatch.delenv("CODEGUARD_MAX_TASKS_PER_FILE", raising=False)
    settings = Settings.from_env()
    assert settings.max_review_tasks == 100
    assert settings.max_tasks_per_file == 10

def test_phase2_budget_env_override(monkeypatch):
    monkeypatch.setenv("CODEGUARD_MAX_REVIEW_TASKS", "17")
    monkeypatch.setenv("CODEGUARD_MAX_TASKS_PER_FILE", "3")
    settings = Settings.from_env()
    assert settings.max_review_tasks == 17
    assert settings.max_tasks_per_file == 3
```

另测 orchestrator 初始 State 使用传入的 `ReviewBudget(max_tasks_to_review=17, max_tasks_per_file=3)`，并断言 `ReviewState.__annotations__` 没有新字段。

- [ ] **Step 2: Run config tests and verify they fail**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_config_settings.py -q
```

Expected: FAIL，因为 Settings 和 CLI 没有这两个配置。

- [ ] **Step 3: Implement configuration wiring**

读取两个环境变量，非整数或小于等于 0 时抛出包含变量名的 `ValueError`。CLI 构造 `ReviewBudget` 并传给 orchestrator；orchestrator 在初始 State 继续写入已有 `review_budget` 字段。同步 `.env.example` 和 README。

- [ ] **Step 4: Run focused tests and mock smoke**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_config_settings.py tests/test_tasks_models.py tests/test_task_prep.py -q
conda run -n codeguard --no-capture-output python -m codeguard_agent review --help
```

Expected: PASS，CLI help 正常退出。

- [ ] **Step 5: Commit**

```powershell
git add services/agent/src/codeguard_agent/config.py services/agent/src/codeguard_agent/cli.py services/agent/src/codeguard_agent/pipeline/orchestrator.py services/agent/tests/test_config_settings.py .env.example README.md
git commit -m "feat(config): 配置 Phase 2 任务预算"
```

### Task 8: 实现 RiskTag 到 reviewer 的实际路由

**Files:**
- Create: `services/agent/src/codeguard_agent/pipeline/risk_routing.py`
- Create: `services/agent/tests/test_risk_routing.py`
- Modify: `services/agent/tests/test_risk_rules.py`

**Interfaces:**

```python
def reviewers_for_profile(profile: RiskProfile) -> frozenset[str]:
    raise NotImplementedError
def routed_task_ids(reviewer_source_agent: str, tasks: list[ReviewTask], profiles: Mapping[str, RiskProfile], selection: TaskSelection) -> tuple[str, ...]:
    raise NotImplementedError
def render_task_scope(reviewer_source_agent: str, tasks: list[ReviewTask], profiles: Mapping[str, RiskProfile], selection: TaskSelection) -> str:
    raise NotImplementedError
```

- [ ] **Step 1: Add failing routing tests**

断言 `AUTHORIZATION` 只到 ThreatModel/Behavior，`SQL_DATA_ACCESS` 只到 Behavior；多标签取并集但 task 不重复；`GENERAL_REVIEW` 到三路；skipped task 不出现；scope 包含 task id、文件、RiskTag、reason 和 patch 且不含未分派 patch；相同输入渲染结果一致。

- [ ] **Step 2: Run routing tests and verify they fail**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_risk_routing.py -q
```

Expected: FAIL，因为 `risk_routing.py` 不存在。

- [ ] **Step 3: Implement derived routing**

路由函数只读取 `profile.tag_scores` 中 score 大于 0 的标签，通过 catalog 元数据取 reviewer 并集，不修改 profile、task 或 State。scope 使用确定性 task 顺序并渲染:

```text
<review_scope reviewer="behavior">
<task id="src/main/java/OrderService.java#h0" file="src/main/java/OrderService.java">
<risk_tags>TRANSACTION_ATOMICITY</risk_tags>
<risk_signals>text:added:transaction_write:新增 save 调用</risk_signals>
<patch>the selected task patch is inserted here</patch>
</task>
</review_scope>
```

原始 patch 继续经过既有 prompt 防注入包装；scope 元数据只作审查背景。

- [ ] **Step 4: Run routing tests and verify they pass**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_risk_routing.py tests/test_risk_rules.py -q
```

Expected: PASS。

- [ ] **Step 5: Commit**

```powershell
git add services/agent/src/codeguard_agent/pipeline/risk_routing.py services/agent/tests/test_risk_routing.py services/agent/tests/test_risk_rules.py
git commit -m "feat(pipeline): 根据 RiskTag 分派 reviewer task"
```

### Task 9: 把 task scope 接入三路 reviewer 和现有图

**Files:**
- Modify: `services/agent/src/codeguard_agent/pipeline/graph.py`
- Modify: `services/agent/tests/test_graph_orchestration.py`
- Modify: `services/agent/src/codeguard_agent/observability/collector.py` only if existing event normalization drops new event names

**Interfaces:** 消费 `review_tasks`、`risk_profiles`、`task_selection` 和 `render_task_scope`；继续产生既有 candidate/evidence/summary/context reducers；不增加 `ReviewState` 或 `ReviewerState` 字段。

- [ ] **Step 1: Add failing graph tests**

测试 SQL-only task 时 ThreatModel/Maintainability 记录 `no_tasks_routed` 且不调用 LLM；Behavior 只收到自己的 scope；`GENERAL_REVIEW` 三路运行；selected 但未分派的候选记录 `candidate_rejected_unrouted`；skipped 候选仍记录 `candidate_rejected_unselected`；CandidateIssue.task_id、Evidence 首次必经和图节点数量不变。

- [ ] **Step 2: Run graph tests and verify they fail**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_graph_orchestration.py -q
```

Expected: 新增 scope、空 reviewer 和 unrouted candidate 测试 FAIL。

- [ ] **Step 3: Implement reviewer integration**

在 `make_reviewer_node` 调用 subgraph 前计算 routed ids。没有 routed task 时直接返回空 issues 和 `CouncilTrace(node=reviewer.source_agent, event="no_tasks_routed")`，不得创建 ReAct/Direct LLM 调用。存在 routed task 时，把 `render_task_scope` 作为 subgraph 现有 `diff_text` 输入，并继续传递现有 context、摘要和工具白名单。

candidate 收集先过 selected gate，再过 routed gate；越界时拒绝并记录 `candidate_rejected_unrouted`。RiskTriage trace 保留 `profiled` 和 `rule_failed`；reviewer trace 保留现有 discover/candidate 事件。

- [ ] **Step 4: Run graph tests and mock smoke**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_graph_orchestration.py -q
$env:CODEGUARD_PROVIDER = "mock"
conda run -n codeguard --no-capture-output python -m codeguard_agent review --repo . --base HEAD --format json
```

Expected: graph tests PASS；mock CLI 返回合法 JSON，三路固定 fan-out/fan-in 和 Evidence 首次必经不变。

- [ ] **Step 5: Commit**

```powershell
git add services/agent/src/codeguard_agent/pipeline/graph.py services/agent/tests/test_graph_orchestration.py services/agent/src/codeguard_agent/observability/collector.py
git commit -m "feat(graph): 接入风险标签 reviewer 路由"
```

### Task 10: 完成阶段文档、评测样本和全量验证

**Files:**
- Modify: `DECISIONS.md`
- Modify: `docs/ROADMAP.md`
- Modify: `AGENTS.md`
- Modify: `docs/superpowers/specs/2026-07-10-risk-routed-review-orchestration-design.md`
- Modify: `services/agent/evals/README.md`
- Create or modify: `services/agent/evals/dataset/clean/` and `services/agent/evals/dataset/vuln/` risk-tag fixtures

- [ ] **Step 1: Add deterministic risk-routing eval fixtures**

至少增加删除 `@PreAuthorize`、新增 `repository.update`、共享状态无锁更新、无具体规则命中的普通 getter，以及一份多 hunk 大 diff 样本。评测 `expected` 仍描述最终 Issue；RiskTag、路由和 skip 原因通过 trace/metadata 诊断，不改变 matcher 契约。

- [ ] **Step 2: Record the Phase 2 decisions and ledger**

在 `DECISIONS.md` 记录 signal 来源是 `path` 和 `text:added/deleted/changed`；删除不是独立业务风险类别；path-only 进入 `GENERAL_REVIEW`；23 个 canonical RiskTag 聚合后路由；默认预算 100/10；Phase 2 直接实现 task-scoped reviewer 路由，Phase 4 不重复首次路由。同步总设计稿台账、`AGENTS.md`、`docs/ROADMAP.md` 和 eval README，内容只记录已实现能力。

- [ ] **Step 3: Run complete engineering verification**

从 `services/agent` 执行:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/ -q
conda run -n codeguard --no-capture-output ruff check src/
conda run -n codeguard --no-capture-output mypy src/
git diff --check
```

Expected: pytest 全绿，ruff/mypy 无错误，`git diff --check` 无输出。

- [ ] **Step 4: Run zero-cost smoke and eval baseline**

```powershell
$env:CODEGUARD_PROVIDER = "mock"
conda run -n codeguard --no-capture-output python -m codeguard_agent review --repo . --base HEAD --format json
conda run -n codeguard --no-capture-output python -m evals.runner --profile pipeline-notools --runs 1
```

记录 mock 退出码、命中/兜底数量、selected/skipped 数量、各 reviewer 路由数量和报告路径；没有 API key 时不强行运行真实模型，并在复盘中说明。

- [ ] **Step 5: Commit the phase record**

```powershell
git add DECISIONS.md docs/ROADMAP.md AGENTS.md docs/superpowers/specs/2026-07-10-risk-routed-review-orchestration-design.md services/agent/evals/README.md services/agent/evals/dataset
git commit -m "docs(orchestration): 记录 Phase 2 风险路由落地"
```

## Execution Order and Checkpoints

```text
Task 1 models
  → Task 2 diff features
  → Task 3 security rules + Task 4 behavior/quality rules
  → Task 5 catalog/triage/fallback
  → Task 6 ranking/budget
  → Task 7 config wiring
  → Task 8 pure routing
  → Task 9 graph reviewer integration
  → Task 10 docs/eval/full verification
```

每完成一个任务保留独立 commit。Task 5 之前不得让图依赖未完成的规则返回值；Task 8 之前不得改 reviewer 输入；Task 9 之前不得改变 reviewer 节点数量或 Evidence/Judge 路由。

## Phase 2 Done Criteria

- 23 个具体 RiskTag 和 `GENERAL_REVIEW` 均有确定性规则、稳定 source/reason 和 Java/Spring fixture。
- `RiskProfile` 按 tag 聚合，同一标签多信号不重复路由，path-only 不制造具体风险。
- 默认预算为 100 task、单文件 10 task，所有 skip 有明确 reason，排序可复现。
- 三路 reviewer 按 RiskTag 派发 task-scoped diff；`GENERAL_REVIEW` 进入三路；无任务 reviewer 不调用 LLM。
- 没有新增主 State 字段，没有改变固定三路 fan-out/fan-in、Evidence 首次必经和 CandidateIssue.task_id 契约。
- AST、Java Gateway、上下文策略和 Evidence/Judge 的 RiskTag 证据裁决仍留在后续阶段。
- pytest、ruff、mypy、mock smoke 和 `git diff --check` 均通过，阶段决策和复盘已记录。
