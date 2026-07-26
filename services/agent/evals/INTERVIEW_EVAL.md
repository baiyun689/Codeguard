# Codeguard 面试版真实评测方案

## 目标与边界

这套评测用于回答三个可核验的问题：Codeguard 能否发现真实 Java 缺陷，项目语义图和证据链带来多少增益，结果在重复运行中是否稳定。它不是用合成关键词命中制造高分，也不把模型自己给出的分数直接当最终结论。

首版冻结为 60 个真实仓库版本：25 个 Vul4J 案例和 35 个 GitBug-Java 案例。其中 50 个把上游修复反向还原为缺陷版本，10 个保留修复后的版本作为 clean 对照。每条案例记录仓库 URL、完整 revision、patch 方向、许可证说明和已知根因；准备时校验快照 HEAD、diff、标答文件与清单一致性。自动准备的案例行号为 0，不参与定位准确率；只有人工标注且通过 diff hunk 与 HEAD 文件校验的行号才计入该指标。

首期只评 Java。反射、运行时代理、生成源码和外部依赖方法体仍属于未知覆盖，不应将静态图的 `unknown` 当作“未发现问题”。

## 四档消融

四个 profile 使用相同模型、数据集、轮次与评分器，只改变编排和工具能力：

| Profile | 目的 |
|---|---|
| `eval-direct-diff` | 单模型只看 diff 的最低基线 |
| `eval-council-diff` | 三路 ReviewCouncil，但不开放项目图工具 |
| `eval-council-codegraph` | ContextProvider 使用项目语义图；高风险/预算内发现任务使用图谱工具，EvidenceAgent 不额外补证 |
| `eval-codeguard-full` | ContextProvider 与高风险/预算内发现任务使用项目语义图，EvidenceAgent 共享图谱工具 |

这样可以分别计算 Council、图谱探索和举证链的边际收益，而不是只展示完整系统的孤立分数。

图谱档保留生产环境的风险分层策略：低风险任务和 ReAct 预算外任务会按设计走
Direct。报告用 `direct_tier_task_count` 披露这部分覆盖；严格校验只拒绝图谱
构建/工具失败、发现者失败以及 ReAct 异常降级，不把策略选择的 Direct 任务误判为故障。

## 评分契约

自动评分分两层：确定性的文件/位置/类型候选匹配，以及 temperature=0 的语义 Judge。当前 Judge 使用 `deepseek-v4-pro`，若与被测审查模型同源，归档和报告必须标记为 `automatic-provisional` 与 `same-source`，不能称为最终成绩。

所有 profile、所有轮次的未匹配报告会池化为盲审任务。盲审页隐藏 profile 和运行轮次，只显示主张与 diff；Python 评测层不会绕过 Gateway 直接读取受审仓库源码。两位 reviewer 独立判断：

- `novel-valid`：标答之外的真实问题；
- `invalid`：不成立；
- `duplicate`：与另一任务是同一根因；
- `out-of-scope`：真实但与本次变更无关；
- `uncertain`：证据不足，不能自动固化。

两人一致才自动固化；分歧由第三位仲裁者处理。确认的额外真实问题会成为所有 profile 共享的补充标答：发现它的 profile 得 TP，没发现的 profile 得 FN。这样“多报但合理”不会被当误报，也不会只给最先报告它的系统特殊待遇。重复报告只允许一个 TP，其余计为噪音。

## 稳定性与指标

每轮独立计算 Precision、Recall 和 F1，绝不把三轮结果取并集后冒充单轮成绩。报告同时给出：

- Precision、Recall、F1 和按 case 聚类 bootstrap 95% 置信区间；
- stable recall（多数轮命中）、always-detected recall、最差轮 recall；
- 各轮检出集合的平均 Jaccard；
- clean diff 平均误报、定位准确率、级别准确率；
- 每案例端到端耗时的 mean、P50、P95；
- 按 `whole-file`、`call-path`、`framework-entry`、`inheritance`、`state-impact`、`structural` 能力切片的结果。

自动暂定报告可用于快速回归；面试展示应优先使用 `human-adjudicated-final` 报告，并同时披露数据版本、模型、Git SHA、轮次、工具是否实际启用以及尚未裁决的数量。

## 一次性执行

以下命令均在 `services/agent` 目录执行。先在另一个终端启动 Gateway：

```powershell
cd ..\gateway
mvn package
java -jar ci-webhook\target\codeguard-gateway.jar
```

确认 `.env` 中配置真实模型，并设置工具地址：

```powershell
$env:CODEGUARD_TOOL_SERVER_URL="http://localhost:9090"
```

先跑两个案例的真实冒烟：

```powershell
conda run -n codeguard --no-capture-output python -m evals.interview_eval run `
  --workspace ..\..\.eval-work\interview-pilot `
  --cache ..\..\.eval-cache\interview-v1 `
  --case vul4j-01-input-validation `
  --case gitbug-findmax-empty `
  --runs 1 --judge
```

冒烟通过后跑冻结的 60 例、每档三轮：

```powershell
conda run -n codeguard --no-capture-output python -m evals.interview_eval run `
  --workspace ..\..\.eval-work\interview-v1 `
  --cache ..\..\.eval-cache\interview-v1 `
  --runs 3 --judge
```

命令会依次运行四档，写入各 profile 原始归档、自动暂定报告和 `blind-bundle.json`。每完成一个案例都会原子更新 `checkpoints/<profile>.json`；用相同数据、模型和 profile 重跑命令会跳过已完成案例。严格工具 profile 若 Gateway、repo 快照或图谱会话不可用会直接失败，不允许静默降级成无工具。

## 双人盲审与终评

两位 reviewer 使用同一个 decisions 文件、不同 reviewer ID，顺序执行即可：

```powershell
python -m evals.interview_eval serve --bundle ..\..\.eval-work\interview-v1\blind-bundle.json `
  --decisions ..\..\.eval-work\interview-v1\decisions.jsonl --reviewer alice

python -m evals.interview_eval serve --bundle ..\..\.eval-work\interview-v1\blind-bundle.json `
  --decisions ..\..\.eval-work\interview-v1\decisions.jsonl --reviewer bob
```

若固化时报告分歧，用第三位 reviewer 进入仲裁模式处理对应任务：

```powershell
python -m evals.interview_eval serve --bundle ..\..\.eval-work\interview-v1\blind-bundle.json `
  --decisions ..\..\.eval-work\interview-v1\decisions.jsonl --reviewer chair --resolution
```

最终离线重评分不再调用审查模型：

```powershell
python -m evals.interview_eval finalize `
  --dataset ..\..\.eval-work\interview-v1\dataset `
  --runs-dir ..\..\.eval-work\interview-v1\runs `
  --bundle ..\..\.eval-work\interview-v1\blind-bundle.json `
  --decisions ..\..\.eval-work\interview-v1\decisions.jsonl `
  --output ..\..\.eval-work\interview-v1
```

最终产物是 `final-summary.json`、`final-report.md` 和 `finalized-adjudication.json`。任何缺失或冲突裁决都会阻止最终报告生成。

## 数据迭代规则

`interview-v1` 一旦用于正式结果就冻结。人工确认的新标答和标签修订发布为 `interview-v1.1`，不覆盖 v1；不能一边看测试结果调 prompt，一边继续把同一批案例称为未见测试集。若用 v1 调参，应另外保留未参与调参的 holdout，或明确把结果称为开发集成绩。
