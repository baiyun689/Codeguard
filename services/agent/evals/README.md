# Codeguard 评测(eval)框架

> 用「带标注的数据集 + 统计指标」量化审查质量,而不是用 `assert` 死磕不确定的 LLM 输出。
> 跑出的指标用于在统一数据集上对照各 profile(ADR-032 smoke / 无工具 / 文件工具 / 调用方工具)的审查质量。

## 为什么需要它

`tests/` 里的 pytest 测的是**工程正确性**(流水线跑不跑得通);
这里测的是**审查质量**(它到底能不能审出真问题、会不会乱报)。两者互补,缺一不可。

## 快速开始

```bash
cd services/agent
pip install -e . pyyaml          # pyyaml 用于读数据集

# 1) 零成本验证评测链路是否打通(不调真实 LLM,指标无业务含义)
CODEGUARD_PROVIDER=mock python -m evals.runner

# 2) 调真实 LLM 跑评测,重复 3 次统计方差
export CODEGUARD_API_KEY=sk-xxx
python -m evals.runner --runs 3

# 3) 额外开 LLM-as-judge(语义复核 + 描述/建议质量打分,更准、更贵)
python -m evals.runner --runs 3 --judge

# 4) 工具开档(审查员走 ReAct,可调 Java 工具);需先起工具服务并配 URL
#    CODEGUARD_TOOL_SERVER_URL=http://localhost:9090 python -m evals.runner --tools
```

报告默认写到 `evals/reports/pipeline.md`,控制台也会打印速览。

## profile:把"被测系统"做成可插拔(统一标准下做对照)

评测的**统一标准 = 固定数据集 + 固定指标**;"用什么配置跑"由 **profile** 描述,见
`evals/profiles.yaml`(`mode` + `orchestration` + 启用工具集 + 可选模型)。**加一个工具 / 换一种编排 = 加一行
profile,数据集与指标零改动。**

```bash
# 按 profile 跑(覆盖 --tools);不指定则用 --tools 合成 ad-hoc(管线 + 工具开/关)
python -m evals.runner --profile pipeline-notools --runs 1
python -m evals.runner --profile adr-032-smoke --runs 1
CODEGUARD_TOOL_SERVER_URL=http://localhost:9090 \
  python -m evals.runner --profile pipeline-file --runs 1   # 工具开档,需先起工具服务
```

每次运行落一份历史归档到 `evals/runs/<时间>_<gitsha>_<profile>.json`(整体指标 + 逐用例 +
按能力聚合,追加不覆盖)。报告从历史渲染三类视图:

- **历史趋势**:同一 profile 跨版本/时间的指标变化(防退化)。
- **profile 横向对照**:各 profile 最近一次的同组指标并排(老的"工具开 vs 关"只是其特例)。
- **按能力切片**:在"需要某能力"的用例子集上各 profile 的 Recall——同一能力行内一比即该能力的
  工具/编排增益,比笼统的"工具开 vs 关"精确。

ADR-032 默认路径还会在报告中追加 **ReviewCouncil 过程统计**:候选数、证据轮次、Challenge 数量、
SelfChecker 移除来源与 trace 事件数。这些中间态只用于诊断和展示,不参与判分,也不进入产品
`ReviewResult`。

Phase 2 的风险路由同样属于诊断链路，不改变 `expected` matcher 契约。每个 hunk 的
`RiskProfile`、`TaskSelection`、reviewer scope 和跳过原因保留在 State/trace 中；产品结果仍然
只比较 `ReviewResult.issues`。因此可以用 `GENERAL_REVIEW` 兜底普通变更，同时用
`CODEGUARD_MAX_REVIEW_TASKS` / `CODEGUARD_MAX_TASKS_PER_FILE` 对大 diff 做预算回归。

Phase 2 最小样本包括：删除 `@PreAuthorize`、新增 repository update、多步共享状态无锁更新、
普通 getter 兜底，以及多 hunk 大 diff。它们用于验证方向信号、三路路由和 TaskRank 选择行为；
不把规则命中本身当作最终漏洞结论。

> 工具增益要测得出,前提是用例**真的需要该能力**(diff-only 看着没问题、读了文件/上下文才暴露)。
> 若一条用例从 diff 本身就能猜中,开/关工具指标会一样——那是用例不够"难",不是工具没用。

## ⚠️ 工具开档(`--tools`)与数据集的现状错配

`--tools` 让审查员走 ReAct、可调 `get_file_content`。但**当前数据集是合成 diff,磁盘上没有对应的真实文件**,所以工具开档下 `get_file_content` 基本只会返回"文件不存在",agent 退回只看 diff——这导致"工具开 vs 关"在本数据集上是**结构性无效**的对照(测的是数据集喂不了工具,不是工具有没有用,见 `DECISIONS.md` ADR-009)。

工具的真实价值已在 repo 上**定性坐实**(审查员会自主读整文件再推理)。要**量化**工具增益,需补一批 **repo-backed 评测用例**(每条用例带一个真实小仓库,diff 改动其中文件,关键上下文在 diff 之外)——这是下一步该补的数据集工作,不是工具本身的问题。`--tools` 的 harness 已就位,数据集补上即可直接量化。

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
| 重复报告率上界 | vuln 用例超出标答数的报告/总报告 | 观察残余重复或额外噪音 |
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

目录约定(放在 `dataset/repo/<case_id>/`):

```
dataset/repo/<case_id>/
├── repo/          # 变更后的最小可解析工程(工具据此读文件;关键上下文应放在被改文件之外)
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
```

设计要点:**`repo/` 是"变更后"的工程**,且要刻意把"判定所需的关键上下文"放在被改文件**之外**
(如被调用方法的定义、父类约定),这样"开工具 vs 关工具"才有可量化的差距。能力标签取值见
`schema.py:VALID_CAPABILITIES`(`diff-only`/`file`/`ast`/`call-graph`/`rag`)。

## 匹配逻辑(怎么判"报对了")

一条报告命中一条标准答案需同时满足:**文件名对上** + **行号在容差内** + **类型关键词命中其一**。
开 `--judge` 时,规则命中的项再过一遍 LLM 语义复核,判定语义不符则不算命中,并给质量打分。

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
和已有 profile(无工具 / 文件工具 / repo-map)并排对比 —— Recall 提升多少、误报降多少,
就是该能力的价值证明。
