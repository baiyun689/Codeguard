# Codeguard 评测(eval)框架

> 用「带标注的数据集 + 统计指标」量化审查质量,而不是用 `assert` 死磕不确定的 LLM 输出。
> 这是阶段 1「无 Agent 基准版」的配套设施,跑出的指标就是 **baseline**(见 `DECISIONS.md` ADR-002)。

## 为什么需要它

`tests/` 里的 pytest 测的是**工程正确性**(流水线跑不跑得通);
这里测的是**审查质量**(它到底能不能审出真问题、会不会乱报)。两者互补,缺一不可。

## 快速开始

```bash
cd services/agent
pip install -e . pyyaml          # pyyaml 用于读数据集

# 1) 零成本验证评测链路是否打通(不调真实 LLM,指标无业务含义)
CODEGUARD_PROVIDER=mock python -m evals.runner

# 2) 调真实 LLM 跑 baseline,重复 3 次统计方差
export CODEGUARD_API_KEY=sk-xxx
python -m evals.runner --runs 3

# 3) 额外开 LLM-as-judge(语义复核 + 描述/建议质量打分,更准、更贵)
python -m evals.runner --runs 3 --judge

# 4) 阶段3 工具开档(审查员走 ReAct,可调 Java 工具);需先起工具服务并配 URL
#    CODEGUARD_TOOL_SERVER_URL=http://localhost:9090 python -m evals.runner --mode pipeline --tools
```

报告默认写到 `evals/reports/baseline.md`,控制台也会打印速览。

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

阶段 3 把审查器从「单次调用」改成「工具调用 Agent」后,**用同一条命令再跑一份报告**,
和这份 baseline 并排对比 —— Recall 提升多少、误报降多少,就是 Agent 的价值证明。
