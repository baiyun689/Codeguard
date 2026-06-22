# 交接清单（2026-06-22）

> 当前进度快照。下次接手从「下次从哪开始」一节读起即可。
>
> **最近一轮(2026-06-22,收尾)做了什么**:**D12 第二刀——审查员从"普通函数节点"升级为 LangGraph 编译子图(ADR-025)**。每个审查员=`StateGraph(ReviewerState)` 内部三节点 `prepare→review→collect`,经薄包装节点 `make_reviewer_node` 在内部 `subgraph.invoke(显式投影输入)`、只回传产出键。**关键坑(已根治)**:第一版把编译子图直接挂作父图节点,三审查员并行 fan-out 时只读共享键 `diff_text` 被回写 → `InvalidUpdateError`;改用"节点内 invoke 子图 + 显式父↔子映射"范式根除(LangGraph 异构 schema + 并行必须选此范式)。create_agent 的 ReAct 图仍封在 review 节点内,内联为子子图留作后续。不变量(错误隔离/mock 不三倍化/侧信道/门面)全保。**182 过(181→182,+1 子图内部节点可见性测),ruff 干净。** 评测未重跑(只改审查员节点内部封装,父图拓扑/引擎/产出契约不变,含真实编译图 fan-in 的集成测试通过)。
>
> **本轮(2026-06-22,主体)做了什么**:**阶段 4 落地——编排迁移到 LangGraph supervisor 状态图(change `langgraph-supervisor-orchestration`,B+档3 一步到位)**。动机明确是**可读性 + 学 LangGraph + 好扩展,不追增益**(甚至预期评测更吵)。新增 `pipeline/graph.py`:`ReviewState`(issues 加法 fan-in、gathered_context 自定义去重 reducer、final_issues 承接聚合/过滤结果避开 reducer 冲突)+ supervisor 节点(`enable_supervisor` 开=LLM 决策动态派发/补派/重派/finish;关或 mock=确定性全派)+ `Send` 动态扇出 + 双重护栏(iteration≤3 + recursion_limit=50)+ 审查员/聚合/误报过滤节点复用现有 stage 逻辑。**门面 `PipelineOrchestrator.run()` 签名不变**,cli/runner 仅各加传一个开关;`gathered_context` 仍只走 trace_sink 不进 ReviewResult(守 ADR-001)。**分路径默认(ADR-024/D9)**:CLI 默认开、评测受控档(notools/file/repomap)经 profile 强制关=确定性保控变量、另设 `pipeline-supervisor` 观测档。删掉旧线性 `build_default_pipeline`(D11,stage 逻辑被节点复用故留)。**全量 Python 181 过(164→181,+17 针对性测:reducer/supervisor 各分支/节点错误隔离/门面侧信道/真实编译图 fan-in 与 supervisor 循环),ruff 干净,原 164 测全绿坐实门面同构**。详见 ADR-024。**未跑 task 6.3**(确定性模式真实评测复确认指标),需起 gateway + 真实模型,留待下次真实环境;工程正确性已坐实。
>
> **上一轮(2026-06-21,第三段)做了什么**:开 change `repomap-include-callers`,补 `get_repo_map` 的**调用方结构盲区**(ADR-020 实证:叶子调用方永不进地图)。实现成立(`findDirectCallers` + 渲染器独立预算 callers 段,Java 38 单测;probe 证地图从缺 caller 变为列出 caller)。但 **eval-first 给出诚实负结论(ADR-022)**:caller 难例 `repomap_npe_caller_001` 在**工作裁判**下 before(无 callers)/after(有 callers)**都 3/3**——审查员纯靠 diff 推理("改成可空→调用方 NPE")即被语义裁判给分,不靠 callers 段。**期间挖出并修了一个 harness bug(ADR-021)**:千问裁判 disable-thinking 发错厂商格式 + 带日期的 3.7-max 强制 thinking 拒 forced tool_choice,致整轮裁判回退规则尺、把"before 0/3"假象误导成真;换 `qwen3.7-max` alias + 按厂商分派格式后 0 失败。**决策:callers 段仍 ship**(满足 spec、补真实盲区、零回归、给"具体可定位"结论),诚实标注"审查员级增益未证出"。**裁判修复独立有价值**。
>
> **最近一轮(2026-06-21,下半场)做了什么**:承 ADR-019 衍生待办①,**构造强隔离跨文件难例 `repomap_npe_isolated_001` 并验证通过——首次测出 repo_map 相对 file 的独有增益(ADR-020)**。用例三重隔离 + 契约撒谎:改动文件持有的是接口 `PriceCatalog`(非具体类)、有 3 实现只 1 个(`legacy.TariffLookupTable`)返 null、接口 javadoc 谎称"永不返回 null"(只读契约的审查员会信任而不报)、~10 诱饵文件(含同名 `lookup`)。实测(`--runs 3 --judge`):file 档该用例 **0/3**、repomap 档 **2/3**;网关日志机制级佐证 repomap 审查员走了"`get_repo_map`→`get_file_content(TariffLookupTable.java)`"导航路径;repomap 整体 F1 0.703 首次反超 file 0.646。纯新增 fixture,未碰 `src/**`。
>
> **最近一轮(2026-06-21,上半场)做了什么**:把 HANDOFF 第 1 优先「量化 repo_map / file 工具增益」(ADR-012 欠账,因 ADR-017 修好 harness 后终于可跑)做实。三档 head-to-head(notools/file/repomap,同 git `de6e037`、同会话、`--runs 3 --judge`、真实 DeepSeek + qwen 裁判)。**结论(ADR-019)**:① **工具开 vs 关有明确增益**——需读 diff 外文件的切片 recall 从 0.762/0.667 拉到 1.000、整体 R 0.833→0.893、F1 0.633→0.679,且误报率不被工具拖坏;证伪了 ADR-009/011"测不出增益"的悬案(根因是当时数据集/harness,非工具无用)。② **repo_map 导航叠加在 file 之上零增量**——现有 `repomap_npe_*` 难例审查员从 diff/import 就能猜到该读哪个文件,`get_file_content` 单独够用,用不上 PageRank 导航;要量化 repo_map 独有增益须补"diff 里猜不到目标文件"的强隔离难例(→ 已由下半场 ADR-020 补上)。纯测量,未碰 `src/**`。
>
> **上一轮(2026-06-20)做了什么**:把 `fp-verify-reviewer-context`(Step 3:给误报复核员喂审查员的 diff 外上下文)从"代码就绪、验证受阻"推到**完成并归档**。期间:① 证伪并订正 ADR-016(工具档崩塌真因不是 `recursion_limit`,而是评测 harness 把工具指向 cwd → ADR-017);② 跑完 Group 6 正式 before/after,证明 Step 3 有效(P/F1/clean 误报率均改善、跨文件真问题未误删);③ 补掉一个 production 健壮性缺口(审查员撞递归上限优雅降级 → ADR-018)。三次提交均已推送 master(`f64ae34` / `1c4c4a2` / `22bdeba`)。

## Step 3：把审查员上下文喂给复核员 —— 完成 ✅（详见 ADR-016→ADR-017 / change `fp-verify-reviewer-context` 已归档)

> **结果(ADR-017,`--runs 3 --judge`)**:before(复核关)vs after(复核开+喂上下文):Precision 0.531→**0.640**、F1 0.675→**0.737**、clean 误报率 0.833→**0.500**、Recall 0.929→0.869(−0.06)。**6.3 判定通过**:repomap+file 切片 TP 20→20 守住(4 个跨文件难例全 3/0/0,复核员没误删真问题)、FP 16→9;Recall 那 0.06 代价全在合成 diff-only 的 complex 切片(复核员本就无上下文,偏删)。Step 3 达成设计目标。

让 ReAct 审查员经工具读到的 diff 外上下文(文件/代码地图)传给误报复核员,使其"查"而非"猜",修 ADR-014 跨文件误删。

- **已做(已提交)**:`engines.py` 引擎返回 `ReviewOutcome{result, gathered_context}`、`ToolAgentEngine` 从 `raw["messages"]` 抽 ToolMessage;`PipelineContext.gathered_context`;`reviewer_stage` 按 `(tool,args)` 去重汇总;`fp_filter` 渲染+字符预算注入 `{{context}}`;`fp_verify.txt` 加"据上下文实证判定"段;新 profile `pipeline-repomap-fpverify`。**notools/直连档行为不变(gathered_context 恒空)**。change **已归档**(`openspec/changes/archive/2026-06-20-fp-verify-reviewer-context`,delta spec 已同步进主 specs)。
- **前置 blocker 已解(ADR-017,2026-06-20)**:ADR-016 把工具档崩塌(recall 0.857→0.476)误判为"`recursion_limit=12` 太低"。**实际调到 25 仍崩**——真因是**评测 harness 把工具指向 cwd**(合成用例无快照时回退 `"."`=`services/agent`,扫到数据集自身夹具,审查员对无关文件无界乱逛撞顶)。修复 `case_repo_root`(仅真实 repo 根才建工具会话,绝不隐式回退 cwd)后,`--runs 1` 下 **recursion 失败 25→1、recall 0.429→0.893**。纯 evals 改动,未碰 `src/codeguard_agent/**`。
- **残留已处理(ADR-018)**:repo-backed 难例审查员撞 `recursion_limit=12` 时,`ToolAgentEngine.review` 现降级为无工具直连复审(`DirectEngine`),不再被静默丢弃;该 production 健壮性缺口已补。

**Group 6 已完成**(2026-06-20,见上结果框 + ADR-017):`pipeline-repomap` vs `pipeline-repomap-fpverify` `--runs 3 --judge` 跑完、判定通过、ADR-017 已回填、change 已归档。

**环境**:gateway `mvn package` 可正常构建/启动(9090,`/health` OK;本轮已用毕关闭);`CODEGUARD_JUDGE_*`=qwen3.7-max(跑前探活防 403,免费额度会耗尽)。

---

## 审查质量调优一轮：级别校准 + 误报复核 profile —— 已完成并提交 ✅（详见 ADR-014 / ADR-015）

把 baseline 用 `--runs 3 --judge` 做实后,针对两条短板各打一枪,均守"一次一个变量、可量化":

- **级别 rubric 校准**(已提交 `fix(prompts)`):三审查员 CRITICAL 收窄、WARNING 设默认档,空 catch 等向数据集口径对齐。级别准确率 **0.486 → 0.806**,P/R 不受影响(severity 不进匹配)。
- **误报复核升为 profile**(已提交 `feat(evals)`,openspec change `fp-verify-profile` 已归档):`Profile.fp_verify` + `pipeline-fpverify` 档;runner 据 profile 驱动、不再认全局 env。**独立异源复核员净增益已在 qwen-plus 与 qwen-max 两次复现**(相对 notools:P 0.51→0.72、clean 误报率 0.71→0.375 腰斩、F1 +0.12,Recall −0.06)。原版 `fp_verify.txt` 即最优,未改。
- **harden(复核 prompt 强化)搁置**:premise moot(见下方法论)+ 曾遇 qwen 403 未测成,不留假结论;`fp_verify.txt` 保持原版。openspec change `harden-fp-verify-prompt` 已作废归档。

**方法论(本轮最值钱的一条,已进 ADR-015)**:**需读 diff 外文件才审得准的用例(file/repo-map 能力)不该在 diff-only(notools)档评判 FP 复核**——该档审查员与复核员都只有 diff、都在猜,调复核松紧只是 recall↔precision 跷跷板。要打破它须让复核员"查"而非"猜",即喂 diff 外上下文(=工具档 + Step 3)。

**环境提醒**:`CODEGUARD_JUDGE_*` 现为 **qwen3.7-max**(免费额度会耗尽→全 403,届时复核与裁判都会回退/失效;跑前先探活)。

---

## 评测升级：复杂行为诊断（复杂用例 + 诱饵 + 行为指标族）—— 工程已完成 ✅（实跑 baseline 待补）

openspec change：`evals-complex-behavior`。动机：vuln 用例全是"单 diff / 单问题 / 单类别",每条 recall 非 0 即 1（高方差噪音），且审查器在**真实复杂 diff**下的关键行为——漏次要 / 过度上报 / 级别误判——完全没被度量。目标：把 evals 从"出平均分"升级成"照出 agent 具体弱点"。

落地（纯加法，既有 6 指标口径冻结、历史归档向后兼容）:
- **数据形态**：`EvalCase` 加可选 `distractors`（看着像漏洞、实则无害的点，与 `ExpectedIssue` 同构以复用匹配）。删 19 条单问题 vuln，换 **6 条复杂用例**（每条 expected ≥3、跨维度、含主+次 severity、带 1~2 诱饵）；clean(8)/repo(7) 不动。旧 `runs/*.json` 挪 `runs/_pre-reset/` 留档（baseline 重置）。
- **判定层**（`matcher.py`，只加桶不改配对）：未被认领的 FP 过诱饵列表 → 命中即「中诱饵」、其余「凭空乱报」（TP 优先于诱饵）；按 severity 落桶（主=CRITICAL，次=WARNING/INFO，None 不计分层）。
- **指标族**（`metrics.py`/`schema.py`）：诱饵命中率、vuln 噪音/条、报告膨胀比、主/次项 recall、级别准确率·复杂切片、**裁判↔规则一致率**（评测尺自校准）。
- **报告**（`report.py`）：核心表追加新指标行、分歧段补顶层一致率%、新增「过度上报诊断」「主/次项 recall 对照」两段。
- **契约**：复杂用例规则尺判定偏乐观,**指标只有开 `--judge` 才完全可信**（写进 spec/README）。

**工程正确性已坐实**：Python 139 测（117→139,+22）全绿；mock 端到端跑通确认 21 条全加载、报告新段渲染正常。

**首版诊断 baseline(2026-06-17,`pipeline-notools` + `--judge`,真实 DeepSeek 审查 + qwen 裁判,1 跑)**:

| 指标 | 值 | 解读 |
|---|---|---|
| P / R / F1 | 0.535 / 0.821 / 0.648 | — |
| 误报率(clean) | 0.500 | 8 条 clean 上 4 个 FP |
| **主项 recall(CRITICAL)** | **0.600** | ⚠️ **抓小漏大**:漏 ~40% 高危 |
| **次项 recall(WARN/INFO)** | **0.944** | 次要问题几乎不漏 |
| 级别准确率 / 复杂切片 | 0.652 / 0.667 | 8 处判错几乎全"往高判"(坐实 ADR-004 系统性高判) |
| **诱饵命中率** | **0.000** | 6 诱饵零踩——对陷阱很克制 |
| vuln 噪音/条 / 膨胀比 | 1.23 / 1.36 | FP 全是"凭空乱报"非"中诱饵"(import 乱报 4、config 乱报 3) |
| 裁判↔规则一致率 | 92.3% | 评测尺可信(仅 1 分歧) |

能力切片(无工具锚):diff-only 0.857 / file 0.714 / repo-map 1.000。

**三个待优化方向(数据驱动,后续针对性做)**:① 抓小漏大——主项 recall 只 0.60,prompt/编排该优先拉高 CRITICAL 召回;② 系统性高判 severity;③ clean 上"凭空乱报"(诱饵不踩但纯净代码反而乱报)。**注**:本版 `--runs 1`(无方差),定 baseline 前建议补 `--runs 3`;复杂用例为 diff-only,工具增益需另跑 `pipeline-file`/`pipeline-repomap` 对照。

衍生（不在本次范围,留作后续）：跨版本**趋势化新指标**（当前 `archive._metrics_dict` 只序列化既有 6 指标,新指标仅在单次报告出现）；用例从 6 扩到 8~10 + 难度分层 + repo-backed 复杂用例。

## 第二个工具 get_repo_map（借鉴 aider repo map）+ 沙箱放宽 —— 已完成 ✅（量化实跑待补）

openspec change:`add-repo-map-tool`。动机:`get_file_content` 只能读"被改文件本身",审不出跨文件问题;根因是审查员不知道"该读哪个 diff 外文件"(只见 diff 文本)+ 沙箱白名单=diff 文件(读不到 diff 外)。走第三条路线:借鉴 **aider repo map**(tree-sitter→PageRank→预算压缩)给一份"diff 邻域代码地图"做导航,栈换成 Java 原生。详见 ADR-012。

落地:
- **Java(`services/gateway/agent/repomap/`)**:`TagExtractor`(接口,抽 def/ref;Java 实现 `JavaTagExtractor` 用 JavaParser,按扩展名经 `TagExtractorRegistry` 路由——多语言只需加实现+注册一行)→ `RepoMapRanker`(建图 + 自实现加权 personalized PageRank,diff 改动文件为种子,rank 沿出边分摊到 `(文件,符号)`)→ `RepoMapRenderer`(签名级 + token 预算贪心裁剪)→ `RepoMapBuilder`(扫仓库串联)→ `GetRepoMapTool`(注册进会话,沿 `/tools/{name}` 协议)。
- **沙箱放宽(ADR-012 决策6)**:`FileAccessSandbox` 读授权从"仅 diff 改动文件"→"repo 根内 + 源码扩展名白名单"(保留穿越防御 + 大小上限 + 排除非源码/配置/密钥)。`get_file_content` 由此能读 repo map 指向的 diff 外定义文件。`allowedFiles` 保留作 repo map 种子。
- **Python**:`tool_client.get_repo_map()` + `make_repo_map_tool`(动作触发式描述)挂入 ReAct 工具集(repo_map 在前,file_content 在后);三审查员 prompt 补"导航→细读"纪律(因缺 diff 外上下文致 confidence<0.7 时先 repo_map 定位再 file_content 细读,而非漏报/硬报)。
- **顺带修对照可控性**:发现 ReAct 引擎原本硬编码工具集,会让 `pipeline-file` 也暴露 repo_map、污染对照。补了**工具白名单透传**(`profile.tools → enabled_tools → ToolAgentEngine`),使"开哪些工具"成为对照唯一变量。

**对比 aider repomap.py(task 2.5,进 ADR-012)**:rank 沿出边分摊到 `(文件,符号)` **照搬**(最易写歪处,亲手对照确认);边权**借数值删 Python 特有项**(留驼峰/超高频/种子/×50,删 snake/`_`);token 预算**简化**(aider 二分 → 我贪心,diff-scoped 够用);**不回读源码**(签名直接存 tag)、**不缓存**(每次现算)。

**工程正确性已坐实**:Java **26 单测**(11→26,+15)、Python **118 测**(110→118,+8)全绿。mock 跑通确认 harness 加载新难例(31 条)与 `pipeline-repomap` profile 解析、工具白名单透传。

**量化增益待实跑(诚实记录,不编数字)**:难例 `repomap_npe_crossfile_001`(跨文件 NPE:diff 只见 `codeOf` 调 `repository.findCode().trim()`,缺陷在另一文件 `OrderRepository` 的 `findCode` 返回 null)+ `pipeline-repomap` profile 已就位,但**真实 DeepSeek + 起 gateway 的 before/after 本环境跑不了**(无 key/服务),故不编数字——同 ADR-004/008/009/011。这是阶段 3 继续前该补的实跑。

## 评测升级：repo-backed 回归基建（数据集×profile×统一指标）—— 已完成 ✅

openspec change：`repo-backed-eval-harness`。动机：引入工具后,合成 diff 喂不动 `get_file_content`("工具开 vs 关"结构性无效,ADR-009);更根本的是评测把"被测系统"和"评测标准"耦合,工具一多就组合爆炸、不可复用。

落地:**被测系统与评测标准彻底解耦**——数据集(repo-backed 自包含快照)+ 指标(P/R/F1/误报率/定位/级别)固定不变作统一标准;工具/编排/未来规则引擎都只是可插拔的 **profile**(`evals/profiles.yaml` + `--profile`)。加一个工具 = 加一行 profile,框架零改动。新增:能力标签(`diff-only/file/ast/call-graph/rag`,按地面真值分层)、JSON 历史归档(`evals/runs/`,带 git sha/profile + 按能力聚合)、增强报告(趋势 + profile 对照 + 能力切片)。旧 27 条合成用例零改动仍跑;新增 27 个 pytest(共 110 passed)。

**首个 repo-backed 实测(3 条 `file` 能力用例,真实 DeepSeek,工具实启用)**:

| profile | 工具 | P | R | F1 | 误报率 |
|---|---|---|---|---|---|
| pipeline-notools | 关 | 0.429 | 1.000 | 0.600 | 0.000 |
| pipeline-file | 开 | 0.429 | 1.000 | 0.600 | 0.000 |

**如实记录(不硬凑增益)**:工具**确实被调用且成功**(网关日志证:`get_file_content(FileController.java) -> ok` ×3),但两档指标**完全一致**——这 3 条用例从 diff 本身就足够"猜中"漏洞(`download(name)`+路径、无校验的 transfer、`find().trim()`),模型不读文件也报得出;而 FP 是 prompt 过度上报所致,读文件并不能压。**结论:harness 已打通、工具真被用,但"量化增益"需要更难的用例——diff-only 看着没问题、读了文件才暴露的那种**(当前同文件 hunk 外的设计仍太好猜)。这与 ADR-004/008/009"不跑会误导的数字"一脉相承。

衍生洞察:**当前 `get_file_content` 只能读"被改文件本身"(沙箱白名单=diff 文件)**,跨文件上下文要等后续 `get_related_files`/扩 scope。这定了后续工具的一个实际优先级。

## 审查员编排治理(前置软路由 + 两段式聚合 + prompt 赛道纪律)—— 已完成 ✅

openspec change:`improve-reviewer-orchestration`。动机:多维度提交上三审查员对一份 diff 共报 18 条、聚合**合并 0 条**,根因是审查员越线多报 + 规则去重因行号漂移失效(详见 ADR-010)。

落地:① 前置 `SummaryStage`(软路由,可 `CODEGUARD_ENABLE_SUMMARY` 开关);② `ReviewerStage` 按 `file_groups` 裁剪 diff("明显更小才用");③ `AggregationStage` 升级两段式(规则去重 + **LLM 语义综合**,LLM 只输出分组、代码来合并,杜绝臆造);④ 三个审查员 prompt 补赛道边界 + 分步方法论 + 置信度阈值(<0.7 不报)+ 扩充判例/排除。全程同步签名,失败一律回退。25 个新增 pytest 全绿(共 83 passed)。

**before/after 实测(同一 fixture:springboot-review-demo `HEAD~1`,工具开档,DeepSeek):**

| | 审查员原始 | 规则去重后 | LLM 综合后 | 误报过滤后 | **最终** |
|---|---|---|---|---|---|
| **before(改造前)** | 18 | 18(合并 0) | — | 17(−1) | **17** |
| **after(全开)** | 12 | 12(合并 0) | 7(−5) | 7 | **7** |

两个杠杆可清晰拆开:
- **prompt 赛道纪律 + 置信度阈值**:把"源头过度上报"从 18 压到 12(before 里资源泄漏被报 3 次、空 catch 2 次、硬编码 3 次、还有 logic/quality 越线报"鉴权缺失";after 各审查员显式声明"该项不归我",越线明显收敛)。
- **LLM 语义综合**:规则去重两轮都是"合并 0"(印证行号漂移使精确指纹失效),真正把 12 收到 7 的是第二段语义合并——它合掉了规则抓不住的"同源、跨审查员、行号相邻"重复。

结果:17 → **7**(−59%),落在目标 ~7~9 区间;且**未丢任何一类真问题**(路径穿越 / 资源泄漏 / 空 catch / 硬编码密钥 / 弱签名 / 魔法字符串均在)。误报过滤对外行为不变。

## 阶段 3 第一步(工具调用 Agent · 双语言登场)—— 已完成 ✅

目标:把审查员从"单次直连"升级成**会用工具的 ReAct Agent**,引入 Java 护栏层,只落地第一个工具 `get_file_content`,跑通整条双语言链路。openspec change:`phase3-tool-calling-agent`(proposal/design/specs/tasks 齐备并 validate)。

| 模块 | 内容 | 状态 |
|---|---|---|
| Java 工具服务 | Javalin Tool Server:`AgentTool`/`ToolResult`/`AgentContext` + `ToolRegistry` + 通用分发 `POST /api/v1/tools/{name}` + `/health`;Maven fat jar 独立启动 | ✅ 11 单测 + 端点 smoke 全过 |
| 工具会话层 | `ToolSessionManager`(create/destroy + `X-Session-Id` + TTL);为后续按 project 共享重资源预留挂载点(本期不填充) | ✅ |
| get_file_content + 护栏 | `FileAccessSandbox`:防穿越 + 限 diff 范围 + 大小上限;四类拒绝结构化返回 | ✅ |
| Python 工具客户端 | 同步 `ToolClient`(httpx)+ `get_file_content` 工具定义(LangChain `StructuredTool`) | ✅ |
| ReAct 引擎与分流 | `ToolAgentEngine`(v1 `create_agent` + `response_format`)vs `DirectEngine`(直连基准);`ReviewerStage` 按 `tool_client` 分流 | ✅ |
| 管线接线 | diff→改动文件集合纯函数;`PipelineContext` 增 repo_path/allowed_files/tool_client;CLI 建/销会话 | ✅ |
| 配置 | `CODEGUARD_TOOL_SERVER_URL` + `.env.example` | ✅ |
| 评测两档开关 | runner `--tools` harness 就位 | ✅(见下方限制) |

## 这轮关键结论(都已写进 ADR-009 / design.md)

- **职责边界钉死(统领后续所有阶段)**:Python 编排/推理,Java 护栏/地面真值;四条不变量见 ADR-009 / design.md D0。后续加任何功能(工具/记忆/换编排)都能机械归位。
- **工具价值已端到端定性坐实**:构造"被改方法调用了 diff 外定义的 `sanitize`"的用例,真实 DeepSeek 下**两个审查员自主调用 `get_file_content` 读整文件并据其实现推理**(Java 日志可证)。这就是阶段 3 的核心命题。
- **量化对照本期测不出,如实记(不硬凑)**:评测数据集是合成 diff、磁盘无对应文件,工具档下 `get_file_content` 必"文件不存在"——对照是结构性无效,故不跑会误导的数字(同 ADR-004/008 原则)。
- **实现期修正**:ReAct 框架按环境实装(langchain 1.3)从 0.3 的 `AgentExecutor` 改用 v1 `create_agent`,反而更优(内置结构化收口 + 对齐阶段4 LangGraph)。详见 design.md D5。

## 复盘(理解了什么 / 踩了什么坑 / 下一步)

- **理解**:Agent ≠ 更强的 prompt,而是"能自主获取上下文"。亲眼看到审查员为搞清 `sanitize` 到底做了什么而去读整文件,这一刻 Agent 的价值才具体。双语言的合理性也清楚了:Python 适合编排不确定性,Java 适合做确定性护栏与重计算。
- **坑**:① 实装 langchain 是 v1,0.3 的 agent API 已移除——规划阶段对依赖版本的假设要在落地时校验。② 后台进程端口复用:`kill %1` 跨 Bash 调用失效,旧服务占着 9090 导致"改了代码却没生效",靠按端口 kill 才定位(教训:验证前先确认跑的是新构建)。③ 评测数据集与新能力错配——合成 diff 喂不了文件工具。
- **下一步**:补 **repo-backed 评测用例**(量化工具增益的前提);再沿通用协议 + 会话接缝逐个加重型工具(method/call-graph/RAG)。

## 阶段 2(管线化)—— 已完成 ✅(存档)

并行审查 → 聚合去重 → 误报过滤。指标(`evals/reports/pipeline.md`,默认管线 = 误报验证关):
Precision ≈ 0.40 / Recall ≈ 0.97 / clean 误报率 ≈ 0.67(3 跑);开异源 FP 验证可达 P 0.459 / 误报率 0.417。
关键决策见 ADR-005~008(评测台校准、多标答、prompt 判例、两段式误报过滤)。

## 跑测速查

```powershell
# Java 工具服务:打包 + 单测 + 启动
cd services/gateway; mvn package          # 跑 11 个单测 + 出 fat jar
java -jar target/codeguard-gateway.jar    # 默认端口 9090(CODEGUARD_TOOL_SERVER_PORT 可覆盖)

# Python 单测(工程正确性,当前应 155 passed)
cd services/agent; conda run -n codeguard --no-capture-output python -m pytest tests/ -q

# 真实 ReAct 审查(先起工具服务,再设 URL)
$env:CODEGUARD_TOOL_SERVER_URL="http://localhost:9090"
conda run -n codeguard --no-capture-output python -m codeguard_agent review --repo <repo> --mode pipeline

# 评测(默认管线);--judge 开裁判;--tools 工具开档(需起服务 + 配 URL;合成数据集下工具读不到文件)
conda run -n codeguard --no-capture-output python -m evals.runner --mode pipeline --judge --runs 3
```

> 裁判 / FP 验证用独立模型:`.env` 配 `CODEGUARD_JUDGE_*`(本机用通义千问 qwen,推理模型需 `CODEGUARD_JUDGE_DISABLE_THINKING=true`)。

> ⚠️ **本机 SSL 坑**:conda 环境 `SSL_CERT_FILE` 指向不存在的 `…/envs/codeguard/ssl/cacert.pem`,任何真实 HTTP(工具会话 / LLM / 裁判)一构造 `httpx.Client()` 就 `FileNotFoundError`。跑**真实** eval 前先:`export SSL_CERT_FILE=$(python -c "import certifi;print(certifi.where())")`(纯单测不受影响,除 test_tool_client 那 5 条构造 httpx 的会挂)。

## 👉 下次从哪开始

**本轮(2026-06-21)已收口**:三段——~~量化 repo_map/file 增益~~(ADR-019)、~~强隔离难例证 repo_map 独有增益~~(ADR-020,file 0/3 vs repomap 2/3)、~~补 repo_map 调用方盲区~~(change `repomap-include-callers`:实现成立但 eval 未证出审查员级增益 ADR-022;顺带修裁判 harness ADR-021)。下面是新的起点:

0. **⚠️ 跑判图前先探活裁判到"结构化调用层"**(承 ADR-021):千问带日期版会强制 thinking、拒 forced tool_choice → 裁判整轮回退规则尺并系统性误导结论。普通对话 HTTP 200 不够,要探到 `with_structured_output(function_calling).invoke`。裁判模型用不带日期的 `qwen3.7-max` alias。
1. **(承 ADR-022,可选,难)** 若要证 callers 段**必要**,需"危险无法从 diff 推断、只有读具体 caller 才暴露"的难例(比 ADR-020 契约撒谎更窄)。当前"改成可空→调用方 NPE"类 caller bug 可被 diff 推理 catch,证不出 callers 段增益。优先级低——callers 段已按 spec-completeness ship。
2. **逐个加重型工具**:`get_method_definition`(JavaParser AST,可复用本期 `JavaTagExtractor`)→ `get_call_graph` → `semantic_search`(RAG),沿通用协议 + 会话接缝叠加。`get_definition` 暂缓的边界理由见 ADR-012。**注**:`repomap_npe_isolated_001` 那套"接口多实现 + 契约撒谎 + 诱饵填充"配方已验证能逼出工具增益;但 ADR-022 的教训是——**先确认新能力不会被"diff 推理 + 语义裁判"绕过**(否则同样"加了测不出")。
3. ~~工具利用率纳入评测报告 + 回头核 ADR-022~~ —— **已完成 ✅**(ADR-023:`evals/tool_usage.py` + 报告「工具使用」表 + 归档持久化;侧信道 `orchestrator.run(trace_sink=…)` 不污染 ReviewResult)。**已实跑验证**:`repomap_npe_caller_001` after 态 `repomap_caller_section_read=✓`、读了 GreetingService → 坐实 ADR-022"after 经 callers 段导航",但不靠它 diff 也能蒙对。验证中顺手修掉"ReviewResult 伪工具混进画像"的 bug。**耗时纳入仍待办**(优先级低)。

```powershell
# 复现本轮三档对照:
cd services/gateway; mvn package; java -jar target/codeguard-gateway.jar   # 起工具服务(9090)
$env:CODEGUARD_TOOL_SERVER_URL="http://localhost:9090"
cd ../agent
conda run -n codeguard --no-capture-output python -m evals.runner --profile pipeline-notools --runs 3 --judge
conda run -n codeguard --no-capture-output python -m evals.runner --profile pipeline-file    --runs 3 --judge
conda run -n codeguard --no-capture-output python -m evals.runner --profile pipeline-repomap --runs 3 --judge
```
4. repo map 若在大仓库慢,补按 mtime 的 tags 缓存(对齐 aider)。
5. **(可选,承 ADR-018)** 撞顶降级现在退到 diff-only、丢已得上下文;若要保住,改"流式留存 last state + 撞顶强制无工具收尾"。优先级低(降级已够兜底)。

## 衍生待办(散落在各 ADR)

- ~~级别准确率长期 ~0.6,模型系统性高判 severity~~ → 已由 ADR-015 级别 rubric 校准治到 0.806;ADR-004 老账可视为收敛,数据集扩量后再复查。
- `.env.example` 已补 `CODEGUARD_TOOL_SERVER_URL`;`CODEGUARD_JUDGE_*` 仍未进示例,需要时补。
