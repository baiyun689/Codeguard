# 知识图谱按 RiskTag 拆分注入设计

**日期**: 2026-07-11
**状态**: 已批准,待实施(紧接 [Phase 4 定向发现链设计](./2026-07-11-risk-routed-discovery-phase4-design.md) 之后的子阶段,本次只出设计和实施计划,不在本轮实施)
**前置阶段**: Phase 1-3(风险路由链路)已完成;Phase 4(task 级并发 + 引擎分层)设计已批准
**关联**: ADR-032(ReviewCouncil)、ADR-038、
[Phase 2 风险标签规则与任务排序设计](./2026-07-10-risk-triage-phase2-design.md)(RiskTag→reviewer 路由表)

---

## 1. 背景与目标

三个发现者 prompt(`threat-model.txt` 222 行 / `behavior.txt` 242 行 /
`maintainability.txt` 229 行)各自内嵌了一份~130-150 行的"知识图谱"(漏洞分类/缺陷
模式/判例),**每次调用都全量携带**,不管这次审查的 task 实际风险方向是什么。这带来
两个问题:

1. **prompt 职责混杂**:prompt 应该只是"角色定位+任务说明+输出规范",知识图谱是
   应该按需查阅的领域参考资料,现在两者写死在一起。
2. **上下文稀释**:knowledge 部分占了单个 prompt 60% 以上的篇幅,而一个 task 通常只
   命中 1-3 个 RiskTag,意味着大部分携带的知识和当次审查无关,稀释了模型对"真正
   相关知识"的注意力,也是 Phase 4 分层降本之外**另一个**被浪费的 token 开销。

Phase 4 已经让每个 task 带有明确的 `RiskProfile.tag_scores`,这正好提供了"按需注入
知识"所需的路由信号。本子阶段要把知识图谱按 `RiskTag` 切分,组装 prompt 时只拼接
该 task 命中标签对应的知识片段。

**内容现状(如实记录,不回避)**:现有知识图谱是按"漏洞类别"(如"注入类""并发与
线程安全")组织的,和 Phase 2 已经定型的 23 个 RiskTag 不是干净的一一对应——部分
标签在对应 domain 里**完全没有现成知识**(例如 `behavior.txt` 没有
`TRANSACTION_ATOMICITY` / `IDEMPOTENCY_RETRY` / `CACHE_CONSISTENCY` /
`MESSAGE_DELIVERY` 对应内容,尽管 Phase 2 规则引擎已经在识别这些标签;
`maintainability.txt` 没有明确的 `PERFORMANCE` / `RESOURCE_LIFECYCLE` 小节)。
本次不是纯粹的机械切分,而是要**重写/扩写**,尽可能让每个 reviewer 需要的每个
RiskTag 都有对应的详细知识片段,内容缺口由实施阶段编写补齐。

---

## 2. 范围:每个 domain 需要哪些标签

直接复用 Phase 2 已定型的 RiskTag→reviewer 路由表(不重新定义),按"该 domain 是否
在某标签的 reviewer 集合里"确定它需要哪些知识片段:

| Domain | 需要知识片段的 RiskTag(数量) |
|---|---|
| threat_model(T) | AUTHORIZATION, AUTHENTICATION_SESSION, WEB_SECURITY_CONFIG, INPUT_VALIDATION, INJECTION, FILE_PATH_IO, SSRF_OUTBOUND, CONFIG_SECURITY, DATA_EXPOSURE(9 个) |
| behavior(B) | AUTHORIZATION, AUTHENTICATION_SESSION, INPUT_VALIDATION, INJECTION, SQL_DATA_ACCESS, FILE_PATH_IO, SSRF_OUTBOUND, DATA_EXPOSURE, TRANSACTION_ATOMICITY, CONCURRENCY_CONSISTENCY, IDEMPOTENCY_RETRY, CACHE_CONSISTENCY, MESSAGE_DELIVERY, ERROR_HANDLING, NULL_STATE_SAFETY, RESOURCE_LIFECYCLE, API_CONTRACT, PERFORMANCE(18 个) |
| maintainability(M) | RESOURCE_LIFECYCLE, API_CONTRACT, PERFORMANCE, COMPLEXITY_CONTROL_FLOW, DUPLICATION_DESIGN, OBSERVABILITY_TESTABILITY(6 个) |

`GENERAL_REVIEW` 不生成知识片段——它代表"规则不认识",没有方向可定向,继续只用
base prompt(与 Phase 3 ContextProvider"GENERAL_REVIEW 只拿 Level0"的原则一致)。

同一个标签在不同 domain 下是**独立的两份内容**(不是共享文本):同一个
`AUTHORIZATION`,ThreatModelAgent 关心"能不能被绕过、越权访问",BehaviorAgent 关心
"业务权限判断逻辑本身对不对",视角不同,各自成文。

---

## 3. 文件组织

```
prompts/
  threat-model-base.txt        # 角色+方法论+严重级别+置信度+误报判例+排除项+输出规范
                                # (原 threat-model.txt 去掉知识图谱小节后的剩余部分)
  behavior-base.txt
  maintainability-base.txt
  knowledge/
    threat_model/
      AUTHORIZATION.txt
      AUTHENTICATION_SESSION.txt
      WEB_SECURITY_CONFIG.txt
      INPUT_VALIDATION.txt
      INJECTION.txt
      FILE_PATH_IO.txt
      SSRF_OUTBOUND.txt
      CONFIG_SECURITY.txt
      DATA_EXPOSURE.txt
    behavior/
      ...(18 个,文件名=RiskTag 枚举值)
    maintainability/
      ...(6 个)
```

- 文件名直接用 `RiskTag` 枚举值(如 `AUTHORIZATION.txt`),**不额外维护一份
  tag→文件名映射表**——路径按约定 `knowledge/<domain>/<tag.value>.txt` 直接拼出来,
  避免映射表和文件系统状态不一致的风险。
- 完整性校验放进单测(见 §6),不放运行时:遍历 Phase 2 路由表覆盖到的每个
  `(domain, tag)` 组合,断言对应文件存在;means 如果实施时漏写某个标签的知识文件,
  单测会挂,而不是运行时静默拿到空知识。
- 原 `threat-model.txt` / `behavior.txt` / `maintainability.txt` 三个文件废弃,
  `_load_prompt(reviewer.prompt_file)` 的取值改指向 `*-base.txt`。

---

## 4. 每个知识片段的内容模板

现有知识图谱的信息密度和组织方式参差不齐(有的标签有现成小节可以改写,有的完全
空白需要新写)。为了"尽可能详细、和标签强对应",每个知识片段统一用以下模板编写
(不是每节都必须写满,但结构固定,便于以后增补):

```
## <RiskTag 中文名>(<该标签在 Phase2 词典里定义的"唯一审查问题">)

### 典型模式
该标签在 Java/Spring 代码里通常长什么样(API、注解、代码结构),用具体代码片段
或伪代码示例说明,不用抽象描述。

### 判定要点
命中这个模式后,还需要确认哪些条件才真正构成问题(不是"看到关键字就报")。

### 严重级别参考
这个标签下,什么情况打 CRITICAL、什么情况打 WARNING/INFO——给该标签专属的校准
参考,而不是套用一份全局笼统的严重级别表。

### 已知误报判例
命中该标签特征、但实际不构成问题的具体场景(如"已经在上游做过校验""框架自动
处理了这个情况"),从现有 prompt 里对应但混杂在全局误报列表中的判例里拆出、
或补充新判例。

### 排除项
明确不属于这个标签、容易被误当成这个标签的相邻情况(和相邻标签划清边界,呼应
Phase 2 §4"标签之间的边界")。
```

`base` prompt 保留的内容:角色赛道边界、分析方法论(分步推理的通用步骤)、审查
范围约束、工具使用纪律、输出格式要求——这些和具体标签无关,每次调用都通用。

---

## 5. 组装机制

新增 `pipeline/knowledge_rules.py`:

```python
def load_knowledge(domain: str, tags: Iterable[RiskTag]) -> str:
    """按 domain + 命中的 tag 集合,拼接对应知识片段;找不到文件的 tag 静默跳过
    (理论上不应发生,由单测兜底覆盖率)。GENERAL_REVIEW 不查表,调用方不传即可。"""
```

在 Phase 4 已经改造的 `_prepare` 里(单 task 调用,已知该 task 的 `RiskProfile`),
额外拼接:

```
system_prompt = base_prompt + "\n\n" + load_knowledge(domain, active_tags)
```

`active_tags` = 该 task `RiskProfile.tag_scores` 中 `score > 0` 的标签集合。多个
标签命中同一 task 时,对应知识片段按标签词典的固定顺序拼接、去重(理论上不会重复,
因为一个 domain 下每个标签只有一个文件)。

这是纯查表 + 字符串拼接,不引入 LLM 判断、不做成工具——原因见此前讨论:Direct 档
(Phase 4 低危 task,无工具循环)如果知识做成工具就彻底拿不到,而固定注入对两档
都有效;且"该查哪个标签"已经是确定性信息(来自 `RiskProfile`),不需要模型自己
决策要不要查。

---

## 6. 测试与验证

- **完整性测试**:遍历 Phase 2 路由表(`reviewers_for_tag` 覆盖到的每个 `(domain,
  tag)` 组合),断言 `prompts/knowledge/<domain>/<tag>.txt` 存在且非空。
- **组装测试**:`load_knowledge(domain, tags)` 给定标签集合,验证拼接顺序稳定、
  内容包含预期片段、空标签集合返回空字符串。
- **`_prepare` 集成测试**:验证 system_prompt 确实包含 base + 命中标签的知识,
  不包含未命中标签的知识。
- **内容质量不做自动化断言**(和项目一贯的"tests 测工程正确性、evals 测审查质量"
  原则一致,ADR 系列多次重申):知识内容是否写得好、够不够详细,只能靠实施时人工
  通读 + 后续跑 eval 观察 CRITICAL/WARNING recall 有没有变化来判断,不编造质量数字。
- mock CLI 冒烟确认 prompt 加载路径改了之后链路仍能跑通。

---

## 7. 明确不做的事

- 不新增/修改 RiskTag 枚举(23 个标签 + GENERAL_REVIEW 维持 Phase 2 定型的词典)。
- 不做成工具或 ReAct 内的按需查询——固定注入(见 §5 理由)。
- 不改变 Phase 4 已确定的 tier 分层规则、并发模型、State 契约。
- 不对旧 `threat-model.txt` / `behavior.txt` / `maintainability.txt` 保留兼容——
  直接替换为 `*-base.txt` + `knowledge/`,原文件删除(内容已迁移,不是死代码
  防御性保留)。

---

## 8. 实施台账

| 阶段 | 当前状态 | 已落地内容 | 验证证据 | 刻意未做 |
|---|---|---|---|---|
| 知识图谱按标签拆分 | planned | — | — | RiskTag 收窄工具白名单(仍留待更后续) |
