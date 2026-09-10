# 审查质量评测

当前 profile 针对变更驱动的有界审查。历史归档中的同名 profile 可能使用旧编排，比较时必须核对 git revision、模型、工具是否实际启用及标答版本。行号/关键词匹配只是自动诊断口径，不等于语义正确率。历史 case 限制见 `../ARCHITECTURE.md`。

| Profile | 配置 |
|---|---|
| `eval-direct-diff` | 无工具 diff 直审对照 |
| `eval-codeguard-full` | 完整有界审查、源码与关系工具、证据验证与裁决 |
| `eval-controlled-codegraph` | 当前 Full 的同配置别名 |
| `eval-source-only` | 有界审查仅开放源码工具 |
| `eval-no-evidence` | 有界取证，跳过证据验证，直接裁决候选 |

先运行 `tests/` 的离线工程回归，再按授权选择单 case 或更多付费评测。Windows 命令加 `conda run -n codeguard --no-capture-output` 前缀，在 `services/agent` 执行。

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/ -q
# 以下会调用模型；仅在获得对应评测授权后执行。
conda run -n codeguard --no-capture-output python -m evals.runner --profile eval-codeguard-full --runs 1
```

图谱 profile 需要工具服务 URL、真实仓库与可用模型。严格工具模式不把工具不可用静默当作无工具成功。比较不同 profile 时固定数据集、标答、模型与运行轮数；保留逐 case 检出、候选来源、token、延迟、未完成状态及原始报告。

State/Trace 记录任务路由、符号解析、声明分组、预取/动态查询、调查结果和证据裁决；不再包含 Plan、三维分派、初筛种子或固定步骤评估。产品匹配仍以 `ReviewResult.issues` 为准。

## 合成回归集与真实仓库集

`evals/dataset` 中的旧合成案例继续用于廉价工程回归，其中没有 `repo_path` 的案例不能量化项目图工具增益。严格工具 profile 遇到这类案例会直接失败，不会静默降级。

`dataset/` 下有 **60 例真实仓库素材库**(gitbug + vul4j,每例
`repo/ + changes.diff + case.yaml`,其中 `repo/` 是干净基线快照，运行器会在临时 clone
中应用 `changes.diff` 后再启动工具会话)。它是本地素材库,不参与 `load_cases`
(见 `dataset.py:_LOCAL_ONLY_DIRS`),选材/造 diff 时参考。`dataset/selected-20-v2/` 是已跑评测集
(`manifest.yaml + cases/<case_id>/`,含 planted-bugs.diff 与 checkpoint 数据)。

`selected-20-v2` 的正式评测口径是当前启用 case 的 `case.yaml` 中的 `expected`，共 76 条登记标答，尚未全部验证。
历史用例仍可能包含 `evidence_required`、`evidence_anchors` 和 `evidence_scope` 元数据；旧的
[`selected-20-v2-evidence.yaml`](selected-20-v2-evidence.yaml) 仅保留用于兼容历史归档，当前加载器和评测都不读取它们。
评测命中只读取文件、行号和类型/语义匹配；`Issue.evidence_locations` 与 `root_cause` 不阻断 TP/FN/FP，
内部 Txx/Cxx 编号也不参与评分。Codeguard 最终报告仍保留证据表述，案例级 `--judge` 可将其作为语义上下文。
无工具 Direct 基线最多只有 `changed_code` 位置，不会伪装成已验证的跨文件根因。
`cases/_bugs_gt.json` 是从启用 case 的 `planted-bugs.diff` 按 hunk 生成的 87 条变更区域诊断记录，包含未单独确认的
附带改动，只用于旧的 hunk/跨文件分析，不作为正式 Recall 分母。`recall_analyzer` 默认使用正式标答；
只有显式传 `--gold hunks` 时才读取 hunk 诊断数据。

该评测集当前启用的 15 个 case 全部是 `known-issue-only` 的漏洞/回归样本，没有 clean case 或 distractor，
因此报告中的 Precision 只能表示“未匹配已知标答的报告比例”，不能替代真实误报率。使用严格工具 profile
时，运行器还会在建工具会话前校验 repo-backed 快照的内容是否干净且与 provenance 源码树一致。
从正式集合暂时移出的 case 保存在 `dataset/selected-20-v2/excluded-cases/`，不参与加载，便于后续恢复或扩展。

## 指标含义

| 指标 | 公式 | 看什么 |
|---|---|---|
| Precision | TP/(TP+FP) | 报出的里有多少是真的(噪音) |
| Recall | TP/(TP+FN) | 该审出的审出了多少(漏报) |
| F1 | 2PR/(P+R) | 综合 |
| 误报率 | clean 样本 FP 总数 / clean 样本数 | 干净代码上平均误报几个 |
| 定位准确率 | 命中项里行号对上的比例 | `Issue.line` 准不准 |
| 级别准确率 | 命中项里 severity 对上的比例 | 严重级别判得准不准 |

### 行为诊断指标族(复杂用例)

复杂用例(一份 diff 多个植入问题 + 诱饵)专门照出审查器在真实场景下的行为,见下表。**这些指标只有在开 `--judge` 时才完全可信**(见下方契约)。

| 指标 | 公式 | 看什么 |
|---|---|---|
| 诱饵命中率 | Σ中诱饵 / Σ诱饵总数 | 过度上报里「被似是而非的点骗」的比例(越低=越克制) |
| vuln 噪音/条 | vuln 用例 FP 总数 / vuln 用例数 | 脏代码上的噪音(区别于只看 clean 的误报率) |
| 报告膨胀比 | vuln 用例上 报告数/标答数 的均值 | >1 偏过度上报 |
| 候选压缩率 | 归并移除候选数/原始候选数 | 观察去重强度 |
| 重复报告率上界 | vuln 用例未匹配报告/总报告 | 观察残余重复或额外噪音，包括等量重复替换其他标答 |
| 疑似误归并用例率 | 发生归并且仍漏标答的用例/发生归并的用例 | 定位需人工复核的归并，不直接断言因果 |
| 主项 recall | 命中主项 / 主项总数(主=CRITICAL) | 高危问题漏不漏(抓不抓得住大的) |
| 次项 recall | 命中次项 / 次项总数(次=WARNING/INFO) | 次要问题漏不漏 |
| 级别准确率·复杂用例 | 复杂用例(标答>1)子集的级别准确率 | 多问题场景下级别判得准不准 |
| 裁判↔规则一致率 | 两尺判定全等的 LLM 主判用例 / LLM 主判用例 | 评测尺自身健康度(低=规则尺在飘,靠裁判纠偏) |

## 加用例

往 `dataset/vuln/`(有漏洞)或 `dataset/clean/`(无问题、测误报)丢一个 YAML 即可,无需改代码。格式:

```yaml
id: 唯一标识
category: SQL注入            # clean 样本写 clean
language: java
description: 这条考什么
diff: |                     # 喂给 reviewer 的 unified diff
  diff --git a/X.java b/X.java
  ...
expected:                   # 标准答案;clean 样本留空 []
  - type_keywords: ["sql", "注入", "injection"]   # 报告 type/message 命中其一即类型对上
    file: X.java            # 按文件名匹配,无需完整路径
    line: 13                # 期望行号;0 表示不校验
    tolerance: 3            # 行号容差
    severity: CRITICAL      # 可选,仅统计级别准确率
    note: 给人看的说明
    evidence_anchors: ["X.java:13"]  # 历史兼容字段，当前评测不读取
    evidence_scope: local             # 历史兼容字段，当前评测不读取
```

## 复杂用例与诱饵(量"复杂场景下的行为")

单问题用例测不出审查器的真实行为(漏次要 / 过度上报 / 级别误判)。**复杂用例**为此而生:一份 diff 里植入**多个真问题**(`expected` ≥3 条,跨维度、跨严重级别),并显式标注**诱饵**——看着像漏洞、实则无害的点(已校验的拼接、用了安全 API 的反序列化、具名常量……)。审查器若报到诱饵处即「中诱饵」(被骗),区别于「凭空乱报」。

```yaml
id: complex_xxx_001
category: 复杂混合·XXX
dimension: security        # 该用例的主维度
expected:                  # 多条真问题,高低 severity 搭配
  - type_keywords: ["路径", "traversal"]
    file: X.java
    line: 14
    severity: CRITICAL     # 主项
  - type_keywords: ["资源泄漏", "leak"]
    file: X.java
    line: 15
    severity: WARNING      # 次项
distractors:               # 诱饵:报了就是"中诱饵"误报
  - type_keywords: ["魔法数字", "magic"]   # 审查器误报此处大概率会用的词
    file: X.java
    line: 16
    note: 4096 已抽成具名常量 BUFFER,非魔法数字——务必写清"为什么这是诱饵而非真问题"
```

**造数据三铁律**:① 诱饵必须**真无害**(形似而非),`note` 写清理由,绝不能是"其实也算问题但没标";② 真问题**高低 severity 搭配**,否则分层 recall 切不出"抓大漏小";③ diff 写成像样的 PR hunk(几十行、有上下文),过度上报/优先级行为才被真正触发。

> ⚠️ **`--judge` 可信契约**:复杂用例植入多问题 + 诱饵时,规则尺(关键词匹配)的错配会被放大、判定**偏乐观**。复杂用例的指标**只有开 `--judge`(LLM 语义配对为权威)才完全可信**;未开 `--judge` 时仅规则尺判定,仅供快速回归参考。报告顶部的「裁判↔规则一致率」即评测尺自身的健康度——一致率低先修尺,而非据此判 agent。

## repo-backed 自包含快照用例(让工具有用武之地)

内联合成用例磁盘上没有真实文件,工具读不到 —— 量化不了"读 diff 之外上下文"的增益。
**repo-backed 用例**为此而生:每条用例自带一个可解析的最小工程,工具能真读到文件。

目录约定(素材库 `<case_id>/` 与评测集 `dataset/selected-20-v2/cases/<case_id>/`):

```
<case_id>/
├── repo/          # 干净基线工程快照(运行器应用 changes.diff 后供工具读取)
│   └── src/main/java/...
├── changes.diff   # 被审查的 unified diff(diff 来源,优先于 case.yaml 内联)
└── case.yaml      # 标答 + 能力标签等元数据(无需写 diff,由 changes.diff 提供)
```

`case.yaml` 模板:

```yaml
id: file_path_traversal_001
category: 路径穿越
dimension: security
capability: [file]          # 审准它至少需要哪类上下文;repo-backed 缺省即 [file]
description: 被改方法调用了 diff 之外定义的校验/拼接逻辑,只看 diff 判不准
expected:
  - type_keywords: ["路径", "traversal", "path"]
    file: FileController.java
    line: 14
    tolerance: 3
    severity: CRITICAL
    note: filename 经 diff 外的 PathUtil.join 拼接,未规范化
    evidence_anchors: ["PathUtil.java", "PathUtil#join"]
    evidence_scope: cross_file
```

设计要点:**`repo/` 是"变更前"的干净工程**,运行器会把 `changes.diff` 应用到临时 clone；
并刻意把"判定所需的关键上下文"放在被改文件**之外**
(如被调用方法的定义、父类约定),这样"开工具 vs 关工具"才有可量化的差距。能力标签取值见
`schema.py:VALID_CAPABILITIES`(`diff-only`/`file`/`ast`/`call-graph`/`rag`)。

## 匹配逻辑(怎么判"报对了")

一条报告命中一条标准答案需同时满足:**文件名对上** + **行号在容差内** + **类型关键词命中其一**。
开 `--judge` 时,规则命中的项再过一遍 LLM 语义复核,判定语义不符则不算命中,并给质量打分。
`--judge` 负责案例级语义配对，报告中的 `root_cause` 与 `evidence_locations` 会作为语义上下文输入；它们不构成额外的
证据评分门槛。最终用户报告只展示文件、symbol、行号和关系，不展示内部证据编号；完整原文仍保留在 Trace/Evidence Ledger。

## 模块

| 文件 | 职责 |
|---|---|
| `schema.py` | 数据结构:用例 / 标准答案 / 指标 |
| `dataset.py` | 加载 `dataset/**/*.yaml` |
| `matcher.py` | 规则匹配 + LLM-as-judge,产出 TP/FP/FN |
| `metrics.py` | 聚合 precision/recall/F1/误报率/方差 |
| `report.py` | 渲染 Markdown 报告 |
| `runner.py` | CLI 跑批入口 |

## 路线图衔接

每加一个工具 / 换一种编排,只需新增一个 profile,**用同一条命令再跑一份报告**,
和已有 profile(直接 diff / Council / 代码图谱 / 完整举证)并排对比 —— Recall 提升多少、误报降多少,
就是该能力的价值证明。

## 验证后的本地 pilot（2026-09-10）

`dataset/verified-pilot-v1` 独立保留 1 个已验证多缺陷开发样本、2 个等价代码变更 clean 样本和 2 个新的 prospective 缺陷变体。全部来自一个已知仓库，编译与行为 oracle 已离线验证；不代表跨项目留出测试，首轮评测及两个指定 case 的追加一轮结果见 [评测准备与验证记录](EVALUATION_PREPARATION.md)。详情见该目录 README.md。`python -m evals.frozen_dataset <目录>` 校验冻结输入；`python -m evals.adjudication prepare/score` 保留独立于 matcher 的逐条语义复核，pending 不生成最终分数。旧 76 标答不追认全部有效。

`dataset/verified-codec-holdout-v1` 另外提供 2 个来自 Commons Codec 固定版本的 repository-holdout 变体（现有其他 case 来源中未出现该仓库）。MAIN 编译和实际行为 oracle 已验证，首轮规则检出 2/2；小样本不代表稳定效果。与 pilot 的开发、多次调试样本分开统计。公开项目是否出现在模型预训练中未知。
