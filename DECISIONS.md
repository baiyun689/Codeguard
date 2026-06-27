# 架构决策记录(ADR)

> 每做一个技术选择就在这里写一条:**选了什么、为什么、放弃了什么备选。**
> vibe coding 容易"跟着感觉一路改下去",回头说不清为什么。这份文档就是用来对抗这一点:把每个选择的理由留痕,让"凭感觉"变成"有依据的感觉"。坚持写。

格式建议:编号 + 标题 + 背景 + 决策 + 理由 + 放弃的备选 + 日期。

---

## ADR-001 · 核心数据结构 Issue 的字段设计

**背景**:整个项目所有阶段都围绕"审查问题"这个数据单元流转,必须先定好。

**决策**:`Issue` 包含 severity / file / line / type / message / suggestion / confidence 七个字段。
其中前五个为必需(定位 + 是什么问题),后两个(suggestion / confidence)可选。

**理由**:
- `confidence` 是为后续阶段(误报过滤、排序)预留的——可以用阈值过滤低置信度问题来控误报。
- `severity` 用枚举而非字符串,约束 LLM 输出范围,避免级别名五花八门。

**放弃的备选**:一开始想加 `code_snippet`(问题代码片段)字段,但阶段 1 用不到,
按"先做减法"原则砍掉,需要时再加。

**日期**:2026-06-11

---

## ADR-002 · 阶段 1 用单次直接 LLM 调用,而非一上来就做 Agent

> **⚠️ 已废弃(2026-06-16)**:`--mode single` 的无 Agent 基线(`pipeline/reviewer.py` 的
> `review()`)在完成"有工具 vs 无工具"对比、达成其历史使命后已移除。当前审查统一走多阶段
> 管线(`PipelineOrchestrator`),管线内仍保留无工具直连引擎(`DirectEngine`)作为工具档的
> 对照基准。本 ADR 记录的当时决策与对比结论(见 ADR-004 引用的 baseline 0.500 等)作为历史保留。

**背景**:目标是做"以 Agent 为核心"的项目,但 Agent 复杂度高。

**决策**:阶段 1 先做"无 Agent 基准版"——diff 进、一次 LLM 调用、结构化结果出。

**理由**:
- 需要一个"无工具"的质量基准,后面加工具调用 Agent 后才能量化"好了多少"。
- 按演化顺序走,先把骨架立稳,避免一上来背负 Agent 的复杂度。

**放弃的备选**:直接上 LangChain AgentExecutor。放弃原因:没有基准就无法证明 Agent 的价值。

**日期**:2026-06-11

---

## ADR-003 · 提供 mock 模式,无需 API 密钥即可跑通流水线

**背景**:阶段 0/1 重点是验证"骨架是否打通",不是验证 LLM 效果。

**决策**:`CODEGUARD_PROVIDER=mock`(且为默认值)时返回假审查结果,不调真实 LLM。

**理由**:让流水线在零成本、零配置下就能端到端跑通,降低起步门槛、方便写测试。

**放弃的备选**:强制要求配密钥才能运行。放弃原因:抬高了起步和测试成本。

**日期**:2026-06-11

---

## ADR-004 · severity 保持三级,并给 prompt 加"锚定动作"的判定标准

**背景**:阶段 1 baseline 的级别准确率只有 0.500——命中的问题里一半 severity 判错。
排查发现 `prompts/security.txt` 里**完全没有 severity 判定标准**,模型在凭"严重感"猜级别。
同时引出两个设计问题:severity 字段到底干什么用、三级够不够。

**决策**:
1. severity **保持 CRITICAL / WARNING / INFO 三级,继续用枚举**(不扩级、不换裸字符串)。
2. 在 `security.txt` 增加一段判定标准,把每一级**锚定到"开发者该采取什么动作"**,而非主观严重感:
   - CRITICAL = 完整危害在 diff 内即可自证、可直接利用(注入/鉴权缺失/反序列化/硬编码密钥)→ 建议阻断合并。
   - WARNING = 风险成立但完整危害依赖 diff 之外的条件(SSRF/XXE/XSS/弱加密/随机数/敏感日志)。
   - INFO = 无明确攻击路径的加固建议。
3. 明确 severity(影响多大)与 confidence(我多确定)是**两条独立的轴**,不准用 confidence 的不确定性去压低 severity。

**理由**:
- **级数应匹配"下游动作数"而非"重要性渐变"**。当前只有两种动作:阻断(CRITICAL)vs 仅报告。
  三级已略微超供,扩到五级只会让本就 0.5 的准确率更差(桶越多边界争议越多)。等出现第三种
  真实动作(如阶段4"可自动修复 vs 需人工")再考虑加级。
- severity 的真正用途是**门禁决策**(`cli.py` 靠 CRITICAL 决定退出码/卡不卡合并),其次是排序分诊、
  噪音治理、聚合时保留最高级。它是"怎么处置"的轴,和 type("是什么")正交。
- rubric 的 CRITICAL/WARNING 划分**特意与 evals 数据集标注对齐**(数据集把 SSRF/XXE/XSS 标为 WARNING),
  否则 prompt 再对、评测也测不出来。对齐时发现数据集隐含的原则"危害是否在 diff 内自证"是个比
  "严重感"更清晰的判据,遂采纳进 prompt。

**效果(诚实记录)**:
- 改动后单轮级别准确率 0.667,但 **3 轮复测回落到 0.576**,相比 baseline 0.500 仅 +0.076,
  **落在 P/R 的方差(±0.05~0.07)量级内,无法干净地归因于本次改动**。单轮的 0.667 是运气。
- 根因是**评测台精度不足**:数据集太小(漏洞仅 12 条,severity 只在 ~10-12 个 TP 上算,
  一两条翻转就 ±0.08)+ DeepSeek 的 function_calling 偶发返回不可解析结果(多次把
  path_traversal/missing_authz 直接漏报),把 recall 与 severity 的分母搅成噪音。
- 结论:**改动在设计上更优且与数据集自洽,予以保留;但其价值在当前小数据集 + 模型抖动下
  无法显著验证,留待阶段 2 扩充数据集后复测。** 诚实记下"测不出"本身,比硬说"提升了"更有价值。

**放弃的备选**:
- 扩成五级(Critical/High/Medium/Low/Info):放弃,无对应下游动作,且会进一步拉低准确率。
- 把 severity 改成裸字符串(`str = "INFO"`):放弃,枚举能约束 LLM 输出范围,
  避免满世界写 `.upper()` 防御——这是我们刻意做的设计取舍。
- 继续在小数据集上调 prompt/加 few-shot 把级别准确率刷上去:放弃,在精度不足的评测台上调参
  是过拟合噪音。正确顺序是先把数据集(阶段2 Step 0)和模型稳定性补上。

**衍生待办(转入阶段 2)**:
- 评测报告增加逐用例"期望级别 vs 报告级别"诊断列,否则无法定位是哪几条在错。
- 数据集扩量(security 加量 + 新增 logic/quality 维度)。
- 应对 DeepSeek 结构化输出偶发 None(重试 / 更稳的 method)。

**日期**:2026-06-11

---

## ADR-005 · 评测判分改为「LLM 裁判主判 + 规则尺交叉校验」,裁判用独立模型

**背景**:原 matcher 默认是**纯规则匹配**(文件 + 行号邻近 + 类型关键词三者皆中才算命中),
LLM-as-judge 只能对规则已命中的项做"语义复核 + 降级"。这把尺子有系统性问题:
- **偏乐观**:关键词命中任一即算类型对、`line=0` 时连行号都不校验,"撞词但不是一回事"会被
  计成 TP,抬高 P/R;
- **单向**:judge 只能把规则命中项降级,**捞不回**规则因关键词没撞上而漏配的真命中;
- 而评测台是整个"小步迭代看效果"方法论的**尺子**,尺子不准 → ADR-004 那种"测不出"会反复出现。

**决策**:
1. **判分主判改为案例级 LLM 裁判**(`judge_case` → `_llm_pairing`):一次调用把**全部报告**对到
   **全部标准答案**,做**双向语义配对**(能捞回关键词没撞的真命中,也能踢掉撞词的假命中)。
2. **裁判只做"配对"这一件难事**;TP/FP/FN、定位、级别全由 `_build_outcome` **据配对确定性算出**。
   把 LLM 的不确定性收缩到"配对"一处,其余仍可复现、可 pytest。
3. **规则尺不删,降级为确定性交叉校验**:每条用例并行算一份规则尺 TP/FP/FN 存入 `rule_*` 字段,
   报告渲染"规则尺 vs 裁判尺"分歧表。规则尺零成本、可当廉价回归哨兵。
4. **裁判用独立模型**(`CODEGUARD_JUDGE_*`,`temperature=0`):优先与被测审查器**异源**,
   降低"自己评自己"的偏差;同源时 runner 显式打 ⚠️ 警告。
5. **clean 用例不调裁判**:标准答案为空,报出来的按定义全是误报,直接信数据集标签——
   这恰好避开最容易被自我评判偏差污染的那次调用(误报率指标)。

**理由**:
- 用户判断对:**尺子不准比省 token 严重得多**。但全 LLM 打分真正的代价不是 token,是
  **可复现性**与**自我评判偏差**——所以不是"放飞交给 LLM",而是 LLM 主判 + 三道约束
  (确定性算分 / 规则交叉校验 / 独立裁判 + temp0)。
- 规则尺与裁判尺**并行**,分歧表既能暴露规则尺的关键词偏差(裁判在纠偏),也能在分歧为 0 时
  反过来给"用廉价规则尺做回归"背书。两把尺互为对方的 sanity check。
- 把"配对"和"算分"切开:LLM 只干语义难点,级别是否相等这种 trivial 判断留给代码,
  既省 token 又减少裁判出错面。

**放弃的备选**:
- **纯 LLM、删掉规则尺**:放弃。会让 `baseline.md` 的冻结基线失去可比性(尺子自己每次都在抖),
  且无法用 pytest 锁判分逻辑。我们测的常是几个百分点的 delta,尺子自带方差会把信号淹没。
- **裁判沿用同一个 DeepSeek**:作为默认保留(零额外配置),但**强烈不推荐**——同源会系统性
  美化误报率。故设计成独立配置 + 同源告警,把选择权和风险显式交给使用者。
- **保留旧的逐对 judge + 质量打分(1~5)**:暂搁置。`JudgeScore` 模型留作兼容但不再走主路径;
  质量打分是次要 nicety,优先解决"配得准不准"。需要时再折进案例级裁判一并产出。

**效果(诚实记录)**:
- 工程正确性已用 `tests/test_matcher.py`(配对/算分/裁判脏数据防御/回退)+ mock 链路跑通验证;
- **真实质量影响尚未量化**:需配好独立裁判模型的 key 后,用 `--judge` 在数据集上复测,
  对照规则尺分歧表看裁判是否合理。在拿到这组数据前,不宣称"更准了"——只宣称"尺子设计更稳健"。

**衍生待办**:
- 配独立裁判模型(建议另一家/更强档位)跑一轮 `--judge`,核对分歧表与裁判离谱率。
- 若分歧长期接近 0,说明规则尺已够用,可省下裁判调用只在里程碑跑;反之则把裁判设为默认。

**补记(2026-06-12,拿到真实数据后)**:
配好异源裁判(DeepSeek 审查 + 通义千问 `qwen3.7-plus` 裁判,`temperature=0`)跑了 `--mode pipeline --judge --runs 3`。
结果:**规则尺与裁判尺分歧恒为 0**——裁判在当前数据集上没有纠正规则尺任何一处(既没捞回漏配、也没踢掉错配)。
根因是数据集每条用例的标答都好认、无歧义,撑不起两尺分歧。**结论:裁判工程上稳定可用,但在当前数据集上相对规则尺无可测增益;
规则尺已够用,裁判暂只在里程碑 / 数据集变难后再启用**(上面衍生待办第二条由此落定为前者)。这也再次印证 ADR-005 当初没硬吹"更准了"是对的。
(踩坑备记:`qwen3` 等推理模型走 `function_calling` 必须关思考,否则 DashScope 报 `tool_choice ... not support ... in thinking mode`;
已在裁判配置加 `CODEGUARD_JUDGE_DISABLE_THINKING=true`。原因是 `with_structured_output` 会强制 `tool_choice=required`,与思考模式冲突。)

**日期**:2026-06-11

---

## ADR-006 · 评测标答从「单标答」改为「多标答」,让 eval 能正确判多维度审查结果

**背景**:Step 2 加三领域审查员(安全/逻辑/质量)后 Precision 明显下降(符合预期);但 Step 3 加聚合去重后
Precision **未按预期回升**,一度怀疑去重无效。排查发现真正的病在**评测台**:数据集每条用例只标 **1 个**标准答案
(单维度),但三个审查员对**每条**用例都跑——于是审查员在某条 diff 上**正确发现的其它维度真问题,被系统性算成 FP**。

**决策**:给漏洞用例补全**多维度真实标答**。把真实报告打出来逐条核对后,采用「强+中」两档共补 4 条:
- `sql_injection_001`:+ 资源泄漏(Statement/ResultSet 未关)+ rs.next() 返回值未检查;
- `hardcoded_config_001`:+ 硬编码 DB 口令 `"report"`(CRITICAL,原标答只盯着 url 还标成 INFO,漏了真凭证);
- `magic_number_001`:+ amount 未判空必抛 NPE。
只标**资深 reviewer 在 PR 里真会提**的(资源泄漏、硬编码凭证、必然 NPE),**排除** util/private 方法上的参数判空之类理论性边界。

**理由**:
- **证据驱动**:把审查员的实际输出打印出来看,vuln 用例上的"FP"**绝大多数不是幻觉,是 diff 真有的其它维度问题**。
  单标答 = 拿单维度答案表去考多维度审查员,数学上必然系统性低估 Precision。
- **一举两得**:① Precision 重新可信(0.264 → **0.401(±0.032)**,3 跑);② "同一问题被换措辞/换行号重报"现在会**正确**显示成 FP,
  反过来给 Step 3 去重 / Step 4 误报过滤一把准尺子。
- **Recall 从恒 1.0 落到 0.971(±0.020)**:补的二级标答更难,某些跑次会漏——**评测台从"橡皮图章"变成了真考试**,这正是想要的。

**放弃的备选**:
- **按维度隔离评分**(每条用例只跑对应维度审查员):放弃。只能测"单审查员单项水平",**测不到真实三合一管线**,且会掩盖去重问题。
- **用裁判判定 off-label 报告真伪**:暂放弃。会把 LLM 主观性引回指标,与 ADR-005「裁判只做配对」的确定性约束冲突;留作数据集做大后的备选。
- **给 19 条全量穷举标答**:放弃。把参数判空之类边界都标进去会**奖励过度报告**,只标真会报的。

**效果(诚实记录)**:Precision 0.264 → 0.401(±0.032),Recall 1.000 → 0.971(±0.020),3 跑。
剩余 ~0.40 的 Precision 是**真噪音**(如 `insecure_deser` 单跑报 6 条只 1 条真),交由 Step 4 误报过滤处理——现在尺子准了,Step4 砍得有没有效量得出来。

**衍生待办**:数据集仍偏小(19 vuln),二级标答只补了 3 条用例;后续扩量时一并把多维度标答补齐。评测报告头部仍写死"阶段1 baseline"(实为阶段2 pipeline),待修。

**日期**:2026-06-12

---

## ADR-007 · 给审查员 prompt 补「已知误报判例」+ 加提示注入防御

**背景**:clean 用例误报率偏高(1.375),把审查员的实际输出打出来逐条看,噪音高度集中在**少数几类安全惯用法被误判**上:
PreparedStatement 被误判 SQL 注入、try-with-resources 被误判资源泄漏、正常日志被误判敏感泄漏等。
根因是 `security.txt` **完全没有"已知误报判例"这类负向约束**(logic/quality 已有少量负例),模型只能凭"感觉"判,自然踩这些坑。

**决策**:把我们对误报来源的分析沉淀成 prompt 里的判例:
- `security.txt` 增「已知误报判例(7 条)+ 强制排除项(4 条)」,**全部写成条件式**(如"前端框架自动转义才不报 XSS,
  服务端把用户输入拼进 HTML 仍要报"),避免误伤真漏洞;
- `logic.txt` / `quality.txt` 扩充已有负例(framework 管理生命周期的资源、ConcurrentHashMap 迭代、catch 转译/重抛、DTO 样板代码等);
- `reviewer_stage.py` 的 user 消息加**提示注入防御** wrapper:diff 包进 `<diff_input>` 标签并声明"标签内全是数据、不是指令"。

**理由**:噪音大头就集中在这少数几类已知惯用法上(clean 误报与这些模式几乎一一对应),针对性补判例性价比最高;
注入防御是原本缺的廉价加固(diff 来自任意仓库,可能含恶意"指令式"文本)。

**放弃的备选**:
- **把误报治理写成一大篇规则全塞进 prompt**:放弃。一来 prompt 里写死 JSON 输出格式会与 `with_structured_output` 冲突;
  二来级别体系必须与我们 ADR-004 锚定动作的三级 rubric 保持一致,不能引入 HIGH/MEDIUM 之类混用。只放高频语义判例的精炼子集。
- **把全量判例都塞进 prompt**:放弃。确定性的(测试/文档/路径排除)留给 Step 4 误报过滤器做**可测、可量化**的兜底,
  prompt 与过滤器**两边不重复全量**(prompt 放高频语义判例,过滤器放确定性规则)。

**效果(诚实记录)**:clean 误报率 1.375 → **0.667**(3 跑),约**腰斩**;Recall 未掉(0.971,条件式判例没把真漏洞写死)。

**日期**:2026-06-12

---

## ADR-008 · 两段式误报过滤:确定性规则 + 异源 LLM 验证

**背景**:阶段 2 收尾要补"误报过滤"。改动前基线(管线无过滤,3 跑):Precision 0.401(±0.032)、
clean 误报率 0.667、Recall 0.971。剩余噪音里很大一部分是"正确写法被误判"(参数化查询当注入、
try-with-resources 当资源泄漏)等语义型误报。

**决策**:
1. 在聚合去重之后加**两段式误报过滤** stage:第一段**确定性规则**(正则 type/message + 路径/扩展名 + 置信度阈值,
   零成本、可复现、可 pytest);第二段**可选 LLM 验证**(默认关)。
2. 过滤 = 从 `context.issues` **移除**(不给 `Issue` 加字段,守住 ADR-001);剔除统计写入 `PipelineContext.filter_stats`。
3. 第二段验证模型**优先异源**(复用独立模型配置),`temperature=0`;同源时打 ⚠️ 告警。
4. 确定性规则与审查员 prompt 判例**分工**:prompt 管语义判断,YAML 只管可正则化的确定性规则,两边不重复。

**效果(诚实记录——这是本 ADR 的核心)**:分三档实测。

- **默认确定性档**:当前数据集上几乎无效(81 个 case-run 仅剔除约 5 条)。根因:残留噪音是**语义型、高置信**,
  确定性规则按设计抓不住——真注入也叫"注入"、真泄漏也叫"泄漏",用正则去删会连真漏洞一起误删。
  Precision/误报率与基线持平(落在噪音带内)。
- **同源 LLM 验证(DeepSeek 自查)**:也几乎无效(约 1 条/跑)。根因:**自我确认偏差**——让模型核查
  自己刚报出的结论,它几乎全保留。这是 ADR-005「裁判别用同源模型」的教训在 FP 验证上原样重演。
- **异源 LLM 验证(qwen 复核 DeepSeek)**:**这是真有效的杠杆**。剔除量约为同源的 10 倍(约 10 条/跑),
  3 跑指标:Precision 0.401→**0.459(±0.012)**、clean 误报率 0.667→**0.417(−37%)**、Recall 0.971→0.986
  (不降,未误删真问题)。方差带几乎不重叠,可判定为真改进而非噪音。

**结论**:
- 误报过滤架构正确、就位、可测。**确定性档与同源验证在当前数据集上无效,如实记下,不假装有效。**
- **异源 LLM 验证是被验证有效的杠杆**(与 ADR-005 同一原理),但每条 issue 多一次 LLM 调用、成本高,故**默认关**;
  作为"要高精度且有独立模型时"的 opt-in,且**必须异源**(同源无意义)。
- 零成本压住误报大头的仍是审查员 prompt 判例(ADR-007:1.375→0.667);后置异源验证是在此之上再把
  Precision 推一截(0.40→0.46)。两者叠加,而非互替。

**放弃的备选**:
- 给确定性规则硬塞"参数化查询→不是注入"这类**语义规则**:放弃,会与真漏洞同词、误删真问题——
  这正是它该留在 prompt / LLM 验证、而非确定性规则里的原因。
- 把同源验证设为默认:放弃,自我确认偏差使其形同虚设,还白烧 token。
- 默认开异源验证:放弃,成本高;留作 opt-in。

**衍生待办**:评测报告渲染 `filter_stats`(剔除分布与命中规则);数据集扩量后补确定性规则、复测异源验证的稳定增益;
给 FP 验证一套独立配置(现复用评测独立模型的那套配置)。

**日期**:2026-06-12

---

## ADR-009 · 阶段 3 引入双语言:工具调用 Agent + Java 护栏层,与无工具基准按配置分流

**背景**:阶段 1–2 的审查员只能看到 diff 文本,审不出"需要 diff 之外上下文"的问题(被改方法的完整定义、调用方、相关类)。阶段 3 要把审查员升级成**能自主获取上下文的 ReAct Agent**,并引入双语言架构。这是 Agent 与"更花哨的 LLM 调用"的本质分界:**能自主决定去读什么**。本期遵循"先做减法 / 一次只加一个工具",只落地 `get_file_content`。

**决策**:

1. **职责边界(统领后续所有阶段)**:Python = 智能编排层(推理 / 编排 / 对审查结论的加工);Java = 护栏 + 地面真值层(安全沙箱 / 重静态计算 / 把 Agent 断言钉到真实代码)。判定规则:功能本质是"获取/校验代码事实 + 安全隔离 + 重计算"→ Java;否则要发起 LLM 调用/非确定判断 → Python,纯规则/装配 → Python 编排侧。四条不变量:① 单向依赖(Python 调 Java,Java 不回调、不碰 LLM);② 代码探索只走 Java 沙箱(Python 除采集 diff 外不直接读被审仓库其它文件);③ 不确定性只在 Python;④ Java 不判断"是不是问题"。

2. **agent 放在 `ReviewerStage` 内按配置分流,而非新增 `--mode`**:`ToolAgentEngine`(ReAct)vs `DirectEngine`(直连,= 阶段 2 行为)按 `tool_client` 是否存在二选一。对照实验因此只变"工具开/关"一个变量,无工具 pipeline 天然留作基准。引擎抽象也是阶段 4 换 LangGraph 的接缝。`--mode single`(`reviewer.py`)冻结基准完全不碰。

3. **通用工具协议 + 注册表**:`POST /api/v1/tools/{name}` 单路由 + Java `ToolRegistry`/`AgentTool`,加工具 = 注册一个实现,协议两端不动。比"一工具一路由"更省扩展成本。

4. **现在就立工具会话层,本期不填充共享**:create/destroy 会话端点 + `X-Session-Id`;会话持 repo 路径 + 改动文件集合 + 沙箱。为后续按 project 共享重资源(调用图/RAG/记忆)预埋挂载点,但 `get_file_content` 是无状态只读,本期只立结构不做共享缓存。

5. **`get_file_content` 受 `FileAccessSandbox` 护栏**:防路径穿越(规范化后须仍在 repo 根内 + 拒 `..`)+ 仅限本次 diff 改动文件集合 + 大小上限;四类拒绝都以结构化错误返回,绝不抛断。

6. **保持同步**:`recursion_limit` 约束的 langgraph 图在现有线程池里 fan-out,不引入 async(守 ROADMAP "async 留到 chunking" 的岔路口)。

**实现期修正(诚实记录)**:

- **ReAct 框架从 0.3 API 改用 v1 `create_agent`**:proposal/design 初稿按 `create_tool_calling_agent` + `AgentExecutor`(langchain 0.3)设计,但实装环境是 langchain 1.3 / core 1.4,这两个符号已移除。改用 v1 `create_agent(model, tools, *, system_prompt=, response_format=ReviewResult)`。这是**更优解**:`response_format` 内置结构化收口,免去"逼 prompt 吐 JSON 再正则解析";且 langgraph 基础与阶段 4「LangGraph 重构编排」同源,是提前铺路;改动被引擎抽象封死在 `ToolAgentEngine` 一个类内。详见 design.md D5。

**效果(诚实记录——本 ADR 核心)**:

- **端到端定性证据(真有效)**:构造一个"被改方法调用了文件别处定义的 `sanitize`、其实现不在 diff 上下文里"的用例,真实 DeepSeek 跑 pipeline+tools:**两个领域审查员自主调用 `get_file_content` 读了整文件、读懂了 `sanitize` 的真实实现并据此推理**(Java 日志可证)。这正是阶段 3 的核心命题——agent 自主获取 diff 之外的上下文——被坐实。
- **量化对照(本期测不出,如实记)**:现有评测数据集是**合成 diff、磁盘上无对应文件**,工具开档下 `get_file_content` 必返回"文件不存在",agent 退回只看 diff。在此数据集上跑"工具开 vs 关"是**结构性无效**(测的是数据集喂不了工具,不是工具有没有用),故**不跑**这组会误导的数字——与 ADR-004/008 "测不出就如实记、不硬凑" 同一原则。`--tools` 评测开关已实现就位(harness ready)。

**放弃的备选**:

- 新增 `--mode agent` 独立链路:放弃,对照实验会掺多个变量,且偏离"基准 = 同管线关工具"的本意。
- 一工具一路由:放弃,每加工具改两端,样板重复。
- 完全无状态(不立会话):放弃,后续重资源共享需要会话挂载点(用户拍板现在就预埋)。
- 锁 `langchain<1.0` 降级环境贴合初稿:放弃,退回废弃 API 且扰动可用环境。
- 在合成数据集上硬跑对照拿"P/R 基本不变":放弃,结构性无效且易被误读成"工具没用"。

**衍生待办**:

- **repo-backed 评测用例**:造一批带真实多文件仓库的评测用例,才能量化工具调用的真实增益(本期定性已证,量化待此)。这是阶段 3 继续深入前最该补的一环。
- 逐个加重型工具(`get_method_definition`/`get_call_graph`/`semantic_search` 等),沿通用协议 + 会话接缝叠加;届时按需在会话层填"按 project 共享重资源"。
- 工具利用率/耗时纳入评测报告(现仅 Java 日志可见单次调用)。

**日期**:2026-06-13

## ADR-010 · 审查员编排治理:前置软路由 + 两段式聚合 + prompt 赛道纪律

**背景**:在 springboot-review-demo 的多维度提交上实测,三个领域审查员对一份 diff 共报 **18 条**,聚合阶段**合并 0 条**,最终原样 18 条——而真问题约 7~9 个。根因有二:**(1)审查员越线**——logic/quality 看到明显安全漏洞仍顺嘴报,同一处问题被多个审查员各报一次;**(2)规则去重失效**——现去重按 `(文件,行号,type)` 精确指纹,而不同审查员对同一处问题报的行号天然漂移(68/69/70),指纹不碰撞,一条都合并不了。这是**编排问题**(每个审查员是独立、无共享状态的 LLM 调用,边界泄漏是结构性产物),不是缺 RAG——给更多检索上下文只会更吵。且当前管线**缺摘要阶段**,三审查员都吃整份 diff,重叠被最大化。

**决策**:

1. **前置摘要/分派阶段,软路由而非门控**:新增 `SummaryStage`,审查前用一次结构化调用产出 `{summary, changed_files, change_types, risk_level, file_focus}`。`file_focus` 把改动文件分派给 security/logic/quality。**软路由**:三审查员**始终全跑**,`file_focus` 只用于裁剪各自看到的 diff;未分派的文件**默认发给所有审查员**(兜底)。**为什么不门控**:代码审查里漏报远比重复严重,浅层分类一旦判错、被跳过的审查员留下的漏洞无人兜底,故调度只用于"缩范围"不用于"砍人"。摘要由 `CODEGUARD_ENABLE_SUMMARY` 控制(默认开),失败/mock 一律退回无摘要路径。

2. **按域裁剪 diff,但"明显更小才用"**:`ReviewerStage` 依 `file_groups[name]` 拼该域相关 diff;仅当裁剪结果显著小于整份(< 85%)才采用,否则回退整份,避免裁剪丢上下文。

3. **两段式聚合 = 规则去重(保留)+ LLM 语义综合(新增)**:沿用本项目"先确定性、再 LLM"惯例(同 ADR-008 的 FP 过滤)。第一段保留精确规则去重(零成本、可复现);第二段把第一段结果喂给一次 LLM 做**语义合并**——识别"同源、跨审查员、措辞不同、行号相邻"的项合并为一条。**关键纪律**:LLM **只输出分组(`groups`)**,最终条目由确定性代码从原始 issue 里挑(保留最高 severity),**从结构上杜绝臆造新问题**;输出不可解析/为空/合并后反而变多 → 回退第一段结果并告警(沿用 None 防御)。**为什么两段而非 LLM-only**:保留确定性第一段使"精确重复"零成本可复现,LLM 只处理它解决不了的近邻/跨措辞场景;两段增量还能分别量化。

4. **审查员 prompt 补赛道纪律**:三个 prompt 统一补"显式赛道边界(不属于本维度的发现交对口审查员、本审查员不报)+ 分步分析方法论(输入点/数据流/路径/状态的思维链脚手架)+ 置信度阈值(< 0.7 不报)+ 扩充的判例/排除清单"。目标:单一维度问题尽量只由对口审查员报出;跨维度真问题仍允许报(保 recall),由第二段聚合兜底去重。保留既有约束:severity 按"开发者该采取什么动作"判、宁缺毋滥、confidence 与 severity 两条独立轴。

5. **保持同步**:摘要、聚合(含 LLM 段)均用现有 `execute(context)->context` 同步签名 + `invoke_with_retry`,不引入 async(守 ROADMAP "async 留到 chunking" 的岔路口)。

**效果(诚实记录)**:

- **before 基准已知**:同一多维度 fixture 上 18 条、规则合并 0 条(根因如上)。
- **after(待 repo-backed 实跑量化)**:目标总条数降至 ~7~9 且**不丢任何一类真问题**(SQL/路径穿越/鉴权/资源泄漏/空 catch/硬编码均在),越线重复明显减少。工程正确性已用 pytest 锁定(摘要软分派的"未分派→全发"兜底、裁剪"明显更小才用"、第二段"按分组确定性合并 + 各类异常回退");LLM 质量增量按本项目惯例(ADR-005/008)以实跑数字为准,**不在此假装已测**。

**放弃的备选**:

- **门控式路由**(按分类跳过审查员):放弃,漏报代价远高于重复,浅层分类误判会留下无人兜底的漏洞。
- **LangGraph supervisor 显式分派 / 多轮回溯**:过重,留阶段 4(创新点 A)。
- **纯静态规则按文件后缀分派**:无法理解语义,放弃。
- **聚合第二段做 LLM-only(让 LLM 直接吐合并后的 issues)**:放弃,既丢了确定性第一段的零成本可复现,又给了 LLM 改写/臆造问题的口子;改为"LLM 只分组、代码来合并"。
- **靠 RAG/向量检索"划职责"**:正交问题,且给更多上下文会更吵,留阶段 4 记忆线。

**衍生待办**:

- repo-backed 实跑 before/after,把数字回填 HANDOFF.md 与本 ADR。
- 置信度阈值统一取 0.7,看效果再按维度微调。
- diff 行号→文件行号精确重映射(CLI 场景非必需,另案)。

**日期**:2026-06-13

---

## ADR-011 · 评测升级为可持续回归基建:被测系统与评测标准解耦(数据集×profile×统一指标)

**背景**:阶段 3 引入工具后,评测台暴露两层问题。**(1)数据集层**:现有用例是内联合成 diff,磁盘无对应文件,工具开档时 `get_file_content` 必返回"文件不存在"(ADR-009),"工具开 vs 关"是结构性无效对照,工具价值只能定性、无法量化。**(2)框架层(更根本)**:评测把"被测系统"和"评测标准"耦合在一起——继续用 `--tools` 这种二元开关,后续逐个加 AST/调用图/RAG、乃至将来加规则引擎检测器或换编排时,会组合爆炸、测不出"哪个能力在哪类场景起作用",不构成可复用的统一标准。

**决策**:把评测重构为 **稳定资产(数据集 + 指标)× 可插拔被测目标(profile)**。

1. **数据集与指标固定不变,作为统一审查标准**:沿用既有指标(P/R/F1/误报率/定位/级别),它们与被测系统的具体能力无关。换检测范式(LLM→规则→Agent)、加工具,尺子都不变,结果才可纵向比、可防退化。

2. **repo-backed 自包含快照用例**:每条用例 = `repo/`(变更后的最小可解析工程)+ `changes.diff` + `case.yaml`。文件树代表"变更后"状态,工具据此读到真实上下文。**为什么自包含快照而非真实 git 仓库 commit 范围**:回归基建要确定性、可复跑、与环境解耦;commit 范围会把用例与仓库 git 状态耦合。schema 向后兼容扩展(`repo_path`/`capability`),现有 27 条合成用例零改动。

3. **能力标签按"地面真值来源"分层**:每条用例标 `capability`(`diff-only`/`file`/`ast`/`call-graph`/`rag`),表示"审准它至少需要哪类上下文"。这让评测能**按能力切片归因**——在"需要 X 能力"的用例子集上比开/关对应工具的指标差,比笼统的"工具开 vs 关"精确。

4. **profile = 可插拔被测目标**:`evals/profiles.yaml` 列出每个 profile(`mode` + 启用工具集 + 可选模型),runner `--profile` 选择;不指定则用 `--mode/--tools` 合成 ad-hoc(等价旧行为)。**加一个工具/换一种编排 = 加一行 profile,数据集与指标零改动**。规则引擎检测器(后续独立 change)亦表现为新增 profile——本次仅在结构上留零成本扩展点,不写规则代码、不加 `Issue.source` 字段(守"一次只加一个")。

5. **历史归档 + 三视图报告**:每次运行落 `evals/runs/<时间>_<gitsha>_<profile>.json`(追加不覆盖)作趋势底座;报告从历史渲染"趋势 / profile 对照 / 能力切片"。

6. **工具不可用自动降级**:profile 想开工具但缺真实 LLM 或工具服务地址时,降级为无工具并如实记录"工具实际启用状态",评测不中断。

**效果(诚实记录)**:框架打通,110 个 pytest 全绿(+27)。首个 repo-backed 实测(3 条 `file` 用例,真实 DeepSeek):`pipeline-notools` 与 `pipeline-file` 指标**完全一致**(P0.429/R1.0/F1.6)。**工具确实被调用且成功**(网关日志:`get_file_content -> ok` ×3),但这 3 条用例从 diff 本身就够"猜中",模型不读文件也报得出,故无增益。**这是诚实的负结果,不硬凑**(同 ADR-004/008/009 原则):它说明 harness 与工具链路都通,差的是**更难的用例**——diff-only 看着没问题、读了文件才暴露的那种。

**衍生洞察**:当前 `get_file_content` 沙箱白名单 = diff 改动文件,**只能读"被改文件本身"**(读整文件=比 hunk 多看该文件未改部分),跨文件上下文要等后续 `get_related_files`/扩 scope。这为后续工具定了一个实际优先级。

**放弃的备选**:

- **继续堆 `--tools`/`--ast`/`--graph` 开关**:组合爆炸、不可复用,放弃。
- **真实 git 仓库 + commit 范围做用例**:与外部 git 状态耦合、复跑不确定,放弃。
- **本次就扩沙箱支持跨文件读**:那是扩大 Agent 护栏能力的行为变更,超出 eval change 范围,且会让评测测到生产尚不具备的能力,另案。
- **HTML dashboard / CI 门禁**:留阶段 5,本次只做增强 Markdown。

**衍生待办**:

- 设计更难的 `file` 用例(diff-only 真会误判)再跑增益;`ast/call-graph/rag` 用例随对应工具落地补。
- `evals/runs/` 已 gitignore;趋势数据本地累积。

**日期**:2026-06-14

## ADR-012 · 第二个工具 get_repo_map:借鉴 aider repo map(换 Java 栈 + diff-scoped)+ 放宽沙箱

**背景**:`get_file_content` 落地后(ADR-009),审查员能读"被改文件本身",但审不出**跨文件**问题——被改方法调用了 diff 之外定义的函数、改动是否破坏上游调用方。根因有二:**(1)审查员不知道该读哪个文件**(只看到 diff 文本,无从得知 `sanitize()` 定义在哪);**(2)沙箱白名单=diff 改动文件**,即便知道也读不到 diff 外文件(ADR-011 衍生洞察已点名这是后续工具的实际优先级)。此前规划过两条补法——route①(调用方可控 `allowed_files`)、route②(完整调用图/RAG)——均因过重/边界不清被搁置未开 change。

**决策**:

1. **走第三条路线:借鉴 aider repo map 给审查员一份"diff 邻域代码地图"做导航**。aider 的 repo map 用 tree-sitter 抽全仓库符号 → 建依赖图 → PageRank 排重要性 → token 预算压成"签名级"摘要塞给 LLM。借**算法思路**,实现栈换成 Java 原生:**JavaParser 抽 def/ref tag → 自实现加权 personalized PageRank → 签名级渲染**。

2. **作用域 diff-scoped,而非 aider 的全仓库**:用本次 diff 改动符号作 personalization 种子,只产出"diff 邻域"。审查只关心改动周围,全库地图既大又稀释相关性。

3. **形态是工具(on-demand),而非 aider 的被动注入**:代码审查锚定在已知 diff、范围小且确定,且现有架构是 `/tools/{name}` 协议;做成工具按需付费、与协议一致。

4. **实现栈 JavaParser 而非 tree-sitter**:MVP 仅审 Java,用不上 tree-sitter 多语言;且 JavaParser 是后续 `get_method_definition` 本就要引入的 AST 引擎,提前复用。ref 按**简单名**匹配(不上 SymbolSolver 全限定解析),省 classpath 配置,精确性由审查员后续 `get_file_content` 细读兜底。

5. **PageRank 自实现幂迭代,不引图库**(JGraphT 等):守"先做减法",一个算法不值得引重依赖;幂迭代确定性强、易 pytest、易加边权启发式。

6. **放宽 `FileAccessSandbox`:diff 白名单 → repo 根内 + 源码扩展名白名单**。有了导航能力后再限制只读 diff 文件就使工具失去意义。放宽仍受三重约束(repo 根内 + 源码类型白名单排除二进制/配置/密钥 + 大小上限)+ 路径穿越防御——放宽边界,不等于任意读。`allowedFiles`(diff 集合)保留作 repo map 种子,不再用于读授权。

7. **职责边界仍守 ADR-009**:建图(AST+图+PageRank+渲染)是确定性重计算/地面真值 → Java;何时调、diff 种子怎么传、结果如何注入 → Python。Java 扫仓库建图是护栏层内部事实采集(只对外暴露签名,不外泄文件内容),与 `get_file_content` 的逐文件授权是两件事。

8. **显式不做 `get_definition`**(按符号名直接返回单个定义体):它横跨 `get_repo_map`(定位)与 `get_file_content`(读取),边界糊、LLM 易在两个"读"工具间犹豫。"repo_map 定位 + file_content 读取"这对**无重叠**组合已覆盖需求(aider 和 open-code-review 也都没有单独的 get_definition)。待评测暴露具体痛点(如读整文件浪费 token)再加,且届时用"必填参数差异"(路径 vs 符号名)划清边界。

**对比 aider `repomap.py`(task 2.5 复盘 —— 重写后逐点对照)**:

- **rank 沿出边分摊到 `(文件,符号)`**:**完全照搬**。aider 末段 `ranked_definitions[(dst,ident)] += src_rank*weight/total_out_weight`,我实现一致——这是最易写歪的点(若只做文件级 PageRank,排出来是"哪个文件重要"而非"哪个定义该进地图")。亲手对照确认没踩坑。
- **边权启发式**:**借数值、删 Python 特有项**。保留:种子符号 ×10、驼峰长名(≥8)×10、超高频(定义>5 处)×0.1、引用方在种子文件 ×50、`sqrt(引用次数)`。删掉:snake_case/kebab 与 `_` 前缀降权(Python 命名习惯,Java 用修饰符表私有而非命名,不适用)。
- **token 预算**:**简化**。aider 用**二分查找**塞最多 tag(`get_ranked_tags_map_uncached`);我用**贪心线性累加**(按排名加到超预算即停)。取舍:地图已 diff-scoped 故小,贪心足够且更简单确定;若将来地图变大、要把预算填满再换二分。
- **渲染**:aider 用 tree-sitter `TreeContext` 回读源码展示真实代码行;我把签名直接存进 DEF tag 渲染,**免去回读**,更简单。
- **缓存**:aider 用 sqlite 按 mtime 缓存 tags;本期**不缓存**(每次现算),diff-scoped 限了规模;慢了再补(design.md Risks,留后续)。

**效果(诚实记录)**:

- **工程正确性已坐实**:Java 26 单测(+15:tag 抽取/PageRank 确定性/渲染预算/工具端到端/沙箱放行 diff 外源码+拒非源码)、Python 118 测(+8:repo_map 客户端/工具定义/工具白名单透传)全绿。mock 跑通确认 harness 加载新难例(31 条)与 `pipeline-repomap` profile 解析。
- **量化增益已实跑(2026-06-21,见 ADR-019)**:三档 head-to-head(notools/file/repomap,de6e037,`--runs 3 --judge`,真实 DeepSeek+qwen 裁判)。结论:**工具开 vs 关有明确增益**(file/repo-map 切片 recall 0.762/0.667→1.000),但 **repo_map 导航叠加在 file 之上零增量**——现有 `repomap_npe_*` 难例审查员从 diff/import 就能猜到该读哪个文件,用不上 PageRank 导航。要量化 repo_map 独有增益,需补"diff 里猜不到目标文件"的强隔离难例(ADR-019 衍生待办①)。
- **顺带修了对照可控性**:发现 ReAct 引擎原本硬编码工具集,会让 `pipeline-file` 也暴露 repo_map、污染对照。补了**工具白名单透传**(profile.tools → `enabled_tools` → engine),使"开哪些工具"成为对照唯一变量。

**放弃的备选**:

- **tree-sitter 多语言**:MVP 仅 Java,JavaParser 足矣,放弃。
- **引 JGraphT 跑 PageRank**:为单一算法引重依赖,违"先做减法",放弃(自实现幂迭代,出数值/收敛问题再换)。
- **被动全量注入(aider 原形态)**:偏离现有工具协议、每审查员每次都付 token,放弃;留作"若评测显示该调没调频发"的优化项。
- **route①(每次把要读文件加进 allowed_files)**:把"决定读哪"从 Java 护栏推回 Python/LLM,削弱护栏确定性,放弃。
- **本期就做 `get_definition`**:与现有两工具重叠、边界糊,放弃(见决策 8)。
- **SymbolSolver 全限定 ref 解析**:引 classpath 复杂度,放弃;按名匹配 + 细读兜底。

**衍生待办**:

- **真实 before/after 实跑**(最该先做):`pipeline-file` vs `pipeline-repomap` 在跨文件难例上量化增益,回填 HANDOFF 与本 ADR。
- repo map 若在大仓库慢,补按 mtime 的 tags 缓存(对齐 aider)。
- ref 抽取是否需扩到字段访问/注解、token 预算默认值与建图文件上限按真实仓库标定(design.md Open Questions)。

**日期**:2026-06-15

## ADR-013 · 评测升级为「复杂行为诊断」:复杂混合用例 + 诱饵 + 行为指标族

**背景**:ADR-011 把评测重构为可复用回归基建后,数据集仍是"一条 diff = 一个植入问题 = 一个类别"的单问题用例。两个后果:**(1)指标是高方差噪音**——每条用例 recall 非 0 即 1;**(2)测不出审查员在真实复杂 diff 下的关键行为**——多问题混合 + 似是而非的干扰点同时存在时,会不会漏掉次要问题、会不会过度上报、严重级别判得准不准,完全没被度量。诉求从"出一个平均分"变为"照出 agent 具体在哪类问题上弱",好做针对性优化。

**决策**:

1. **聚焦"复杂混合行为"这一刀(漏次要 / 过度上报 / 优先级)**。这是信息量最大的诊断维度;"分维度强弱""工具增益"另有切片,本次不铺开。

2. **内联先行,repo-backed 留后(③ 先①后②)**:过度上报/漏次要/优先级用内联合成多问题 diff 就能充分暴露,不必等昂贵的 repo-backed。先把"测量基建"(诱饵 + 诊断指标 + 匹配尺加固)做对、跑出第一版诊断;repo-backed 复杂用例(复杂行为 × 工具增益的交集)留作独立后续 change。

3. **诱饵显式标注,而非"凡非 expected 的 FP 都算"**:新增可选 `Distractor`(与 `ExpectedIssue` 同构,复用匹配函数),把误报拆成「中诱饵(被似是而非的点骗)」与「凭空乱报」。只有显式标注才能区分这两类——这是过度上报最有价值的诊断。诱饵必须**真无害**(形似而非),`note` 写清理由。

4. **优先级取轻口径(级别判得准),不动输出契约**:`Issue` 是扁平 list、聚合后顺序本就无意义,"真排序"需让管线产出可信 order(动 `Issue`/输出契约,违"别轻改核心数据结构")。轻口径=多问题场景下 severity 判得准 + 高危不被噪音淹,已覆盖 80% 的"优先级"直觉。

5. **复杂用例以裁判为权威判定,并把「裁判↔规则一致率」提为一等健康指标(Z 方案)**:植入 4~5 问题 + 诱饵时,规则尺(关键词匹配)错配被放大、判定偏乐观;语义配对才可信。复用现有 matcher 骨架(已"vuln+裁判→LLM 主判、否则回退规则"且记 `rule_*`),只补:报告暴露顶层一致率% + 文档明确"复杂用例指标只有开 `--judge` 才完全可信"。一致率顺带量了裁判自身漂移(评测尺自校准)。

6. **指标纯加法,既有 6 指标口径冻结**:新增诱饵命中率 / vuln 噪音/条 / 报告膨胀比 / 主项 recall(CRITICAL)/ 次项 recall(WARNING+INFO)/ 级别准确率·复杂切片 / 裁判↔规则一致率,全部新字段带默认值;报告用 `.get()` 读归档,老归档缺新键渲染 "—"。守住历史趋势可比性。severity 主/次切分点 = `CRITICAL` vs `{WARNING,INFO}`,正对"抓大漏小"。

7. **数据集 baseline 重置**:删 19 条单问题 vuln,换首批 6 条复杂用例(每条 expected ≥3、跨维度、主+次搭配、1 诱饵);clean(8)/repo(7)不动。旧 `evals/runs/*.json` 挪 `runs/_pre-reset/` 留档(`load_archives` 用 `glob("*.json")` 非递归,自动不计入趋势),从复杂数据集这版重新起 baseline。

**效果(诚实记录)**:

- **工程正确性**:Python 139 测(117→139,+22)全绿;mock 端到端确认 21 条全加载、报告新段渲染正常。
- **首版诊断 baseline**(`pipeline-notools --judge --runs 1`,真实 DeepSeek 审查 + qwen 裁判):P 0.535 / R 0.821 / F1 0.648,clean 误报率 0.500。新指标照出三处真问题:**① 抓小漏大**——主项 recall(CRITICAL)仅 **0.600** vs 次项 recall **0.944**,漏掉约 40% 高危却几乎不漏次要(方向反了);**② 系统性高判 severity**——级别准确率 0.652、复杂切片 0.667,8 处判错几乎全"往高判"(再次坐实 ADR-004);**③ 过度上报是"凭空乱报"非"中诱饵"**——诱饵命中率 **0.000**(6 诱饵零踩、对陷阱很克制),但 vuln 噪音 1.23/条来自无中生有,且反差是"埋在脏代码里的诱饵不踩、纯 clean 代码反而乱报"。**裁判↔规则一致率 92.3%**——评测尺本身可信,这些数字可据以优化。
- **诚实标注**:`--runs 1` 无方差,定稳 baseline 应补 `--runs 3`;6 条复杂用例为 diff-only,工具增益需另跑 `pipeline-file`/`pipeline-repomap` 对照(同 ADR-004/011 原则)。

**放弃的备选**:

- **收紧规则尺关键词/行号**(X 方案):治标,对措辞脆,放弃;改让裁判主判复杂用例(Z)。
- **让裁判全面取代规则尺**(Y 方案):丢掉廉价回归与交叉校验,放弃;保留规则尺作交叉校验。
- **重口径优先级(管线输出带 rank)**:动 `Issue`/输出契约、牵连 prompt/CLI/归档,超出评测 change,放弃,取轻口径。
- **诱饵不标注、全部 FP 视为噪音**:测不出"被骗 vs 乱报",放弃。
- **首版就上难度分层 / category 细分 / 扩到 8~10 条 / repo-backed 复杂用例**:第一刀过度工程,放弃,留后续按诊断结果针对性补。
- **把新指标写进归档供趋势化**:`archive._metrics_dict` 暂只序列化既有 6 指标(趋势视图本就只展示这些),新指标仅在单次报告出现;跨版本趋势化新指标留后续。

**衍生待办**:

- **针对性优化「抓小漏大」**:主项 recall 0.60 偏低,prompt/编排优先拉高 CRITICAL 召回,用本指标验证。
- **补 `--runs 3`** 定稳 baseline;跑 `pipeline-file`/`pipeline-repomap --judge` 对照量复杂场景工具增益。
- **开 `repo-backed-complex-cases` 新 change**:repo 用例又少又单一(7 条全 expected:1、零诱饵),补"复杂行为 × 工具增益"交集(框架已就绪,纯灌数据 + 跨文件诱饵)。
- 跨版本趋势化新指标(扩 `archive._metrics_dict`)。

**日期**:2026-06-17

## ADR-014 · 误报复核升为 profile 受控变量,实测「独立复核员」净增益

**背景**:复杂数据集 baseline(ADR-013)+ 级别 rubric 校准(级别准确率 0.49→0.81,不动 P/R)后,当前主短板是**过度上报**:`pipeline-notools --runs 3` 下 Precision ≈ 0.51、clean 误报率 ≈ 0.71,而 Recall 0.857 不构成瓶颈。压误报最 Recall-安全的杠杆是管线末端的"独立异源复核员"(误报过滤第二段 LLM 验证)——它只删不增。该机制代码层早已端到端接好(`CODEGUARD_FP_LLM_VERIFY` + `judge_from_env()` 异源 qwen + `fp_verify_llm` 优先 + None/失败保留防御),但默认关、且只受全局 env 控制、**不是 profile 变量**,两次同名 profile 开/关复核在归档里同名混淆,违反"一个变量=一个 profile"的对照纪律(ADR-009 D3),净增益无法干净量化。

**决策**:

1. **`fp_verify` 升为 `Profile` 字段**(`evals/profiles.py`)+ 新增 `pipeline-fpverify`(= `pipeline-notools` + `fp_verify: true`);runner 据 `profile.fp_verify` 驱动复核,evals 不再认全局 env(被测目标全由 profile 描述)。归档元数据记录本次 `fp_verify` 实际状态。
2. **本档 prompt 不动**(`fp_verify.txt` 原样):本 change 只度量"打开现有复核员"本身的净增益,隔离单一变量;prompt 强化留作条件触发的 Step 2(另开 change)。

**效果(诚实记录,`--runs 3 --judge`,DeepSeek 审查 + 异源 qwen 复核;同 prompt,仅差 fp_verify)**:

| 指标 | notools(复核关) | fpverify(复核开) | Δ |
|---|---|---|---|
| Precision | 0.507 | **0.733** | +0.226 |
| 误报率(clean) | 0.708 | **0.375** | 腰斩 |
| F1 | 0.637 | **0.759** | +0.122 |
| Recall | 0.857 | 0.786 | −0.071 |
| 主项 recall(CRITICAL) | 0.900 | 0.833 | −0.067 |

- **净增益明确**:Precision/误报率大幅改善,F1 +0.12,代价是 Recall 温和下降(主项仍 0.83)。差距远超 `--runs 3` 的 ±0.03~0.05 噪音,且两次只差 fp_verify 一个变量。
- **复核确实生效(直接坐实,非推断)**:本次后台日志被 `tail` 截断,改由逐用例报告数下降验证——`clean_logged_exception` 2→0、`clean_bounded_loop` 1→0、`complex_import_002` 报告 4→3(FP 4→0),无别的机制能删 issue。
- **关键副作用(能力切片照出)**:Recall 损失**集中在跨文件**——repo-map 0.833→**0.583**、file 0.857→0.762,而 diff-only 仅 0.857→0.794。根因:`fp_verify.txt` 复核员**只收到 diff**,对"证据在 diff 之外"的发现(如 `repomap_npe_*` 的跨文件 NPE)看不到依据 → 误删真问题。即复核员对跨文件发现 **context-blind**。

**结论**:在 `pipeline-notools`(diff-only 为主)上独立复核员**净赚、值得默认开**;但**不能 naively 叠加到工具档(file/repomap)**——它会删掉正是工具帮发现的跨文件问题,除非先给复核员同等上下文。

**Step 2(后续 change,条件触发)**:强化 `fp_verify.txt`——① 举证制(说不清攻击路径/安全写法判例即删,反之保留);② severity 敏感(INFO/WARNING 更敢删、CRITICAL 更谨慎);③ **给复核员跨文件上下文**或仅对"diff 内即可判定"的发现复核,修掉 context-blind 误删;可借 project-codeguard(CC-BY-4.0)的安全写法范例当"安全长啥样"的参照。

**放弃的备选**:

- **保留纯 env 开关、跑两次**:归档同名,净增益不可干净归因,违 D3,放弃。
- **收紧审查员上报门槛(置信度阈值/默认不报)**:从源头砍但直接威胁 Recall,且模型自评置信度不准;作为另一根独立轴,不在本 change。
- **确定性规则硬抑制固定安全写法**:过拟合那 8 条 clean、脆、天花板低,放弃单独用。

**日期**:2026-06-18

## ADR-015 · 审查质量调优一轮:级别 rubric 校准 + 误报复核基线复现(并厘清 harden 搁置)

`--runs 3 --judge` 把 baseline 从单跑(ADR-013)做实后,数据指向两条独立短板:级别系统性高判、过度上报。本轮各打一枪,均守"一次一个变量、可量化、留痕"。

**决策 1 · 三审查员级别 rubric 校准**(已提交 `fix(prompts)`)

ADR-013 实测级别准确率仅 0.486(`--runs 3`),12/23 判错且几乎全是 WARNING/INFO→CRITICAL 系统性高判(坐实 ADR-004)。归因发现两类:① 模型无视 rubric 往高判(资源泄漏、条件性 NPE);② **prompt 自身与数据集口径打架**——`quality.txt` 原把"空 catch 吞异常"写进 CRITICAL 定义,而数据集标 WARNING,模型照 prompt 执行反被扣分。

- 改动:三审查员统一加"**就低不就高**"默认,CRITICAL 收窄到"卡合并"窄口径、WARNING 设默认档;明确把资源泄漏 / 条件性 NPE / 空 catch / 硬编码归 WARNING(其中空 catch 由 CRITICAL 降 WARNING,**prompt 向数据集口径对齐**)。
- 口径裁定:CRITICAL 留给"必然崩溃 / 直接可利用、需卡合并"的窄集合;"空 catch 该几级"取数据集的 WARNING(可维护性问题不卡合并)。
- 效果(`--runs 3 --judge`,**severity 不参与 TP/FP/FN 匹配,故 P/R 不受影响**,实测亦印证):级别准确率 **0.486 → 0.806**,Recall 不变。属隔离极干净的单变量改动。

**决策 2 · 误报复核净增益经 qwen-max 复现(补 ADR-014)**

ADR-014 的 Step-1 增益(plus 验证模型)换 **qwen3.7-max** 重跑 `pipeline-fpverify --runs 3 --judge`:P 0.720 / 误报率 0.375 / F1 0.757,与 plus 版(0.733 / 0.375 / 0.759)**几乎重合**。结论:**独立复核员的净增益不挑验证模型、稳健**(相对 notools:P +0.21、误报率腰斩、F1 +0.12,Recall −0.06)。"误报复核做成 profile"这一成果在新模型上钉实。

**决策 3 · harden(复核 prompt 强化)搁置,不留假结论**

曾试图强化 `fp_verify.txt`(举证制 + severity 敏感 + 缺上下文即保留)以救 ADR-014 的跨文件误删,但:① 关键的"缺上下文即保留"版反而把 clean 噪音放回(P 0.733→0.603);② "举证制"版那一跑遭遇 **qwen 免费额度耗尽全 403**,复核根本没运行(兜底"失败即保留"=未测),**不能据此判 prompt 优劣**;③ 更根本——按"diff-only 不评判跨文件用例"的方法论(见下),原版 prompt 在其正当地盘(diff-only + clean)**已是最优**,harden 的 premise moot。故 `fp_verify.txt` **回退原版**,harden change **搁置**,不写"负结果"ADR(它从未被有效测过)。

**方法论留痕(本轮最有价值的一条)**:**需要读 diff 之外文件才能审准的用例(file/repo-map 能力标签),不应在 diff-only(notools)档评判 FP 复核**——该档下审查员与复核员同样只有 diff、都在"猜",调复核员的松紧只是在"keep 对的猜测"与"删错的噪音"间跷跷板(实测:严格版 repo-map 切片 0.583 / 宽松版 0.833,Precision 此消彼长)。打破跷跷板要让复核员"查"而非"猜"——即给它 diff 外上下文(工具档 + Step 3),那才是跨文件复核的正确战场。

**放弃 / 推迟**:harden 的 prompt-only 强化(搁置);"把审查员获取的上下文喂给复核员"(Step 3,需工具档 + gateway,独立 change);引入 project-codeguard(CC-BY-4.0)安全写法判例(独立 change)。

**日期**:2026-06-18

## ADR-016 · 工具档首次真跑:ReAct 撞递归上限致 recall 崩塌(Step 3 代码就绪、真值验证受阻)

为验证 Step 3(把审查员经工具获取的 diff 外上下文喂给误报复核员,见 change `fp-verify-reviewer-context`),**首次真正起 Java gateway 跑工具档**(HANDOFF 一直欠的"工具增益量化"):`mvn package` 起 gateway(9090)→ `pipeline-repomap --runs 3 --judge`(工具开、复核关),DeepSeek 审查 + qwen3.7-max 裁判。

**实跑结果(诚实记录,工具真被用:gateway 侧 704 次工具调用,`get_file_content` 526 + `get_repo_map` 178,qwen 0 个真 403)**:

| | recall | precision | F1 | 误报率 |
|---|---|---|---|---|
| pipeline-notools(无工具) | 0.857 | 0.51 | 0.64 | 0.71 |
| **pipeline-repomap(工具开)** | **0.476** | 0.580 | 0.523 | 0.167 |

**开工具反而把 recall 砍半。根因坐实**:`Recursion limit of 12 reached without hitting a stop condition` —— ReAct 审查员(DeepSeek + langchain `create_agent`)不停调工具、12 步内收不了口就报错,**79 次审查员失败被跳过(189 个域调用里约 42%)**,那些域的发现全丢。这不是复核/Step 3 的问题,是**审查员侧 ReAct 不收敛**;复核只能删、救不了"根本没产出"。

**结论**:
- "工具增益"在当前实现下**为负**(ReAct 不稳),这是 HANDOFF 欠账的诚实答案——不是工具无用,是 ReAct 执行层没跑通。
- **Step 3 的真值对照(`pipeline-repomap` vs `pipeline-repomap-fpverify`)被此前置问题挡住**:在 0.476 的崩塌 recall 上叠复核,只会被递归失败的噪音淹没,跑了也读不出"喂上下文"的净效果。故**未跑 after**,不烧无意义的 qwen(同 ADR-004/009 原则)。
- **Step 3 代码已实现并单测通过**(151 passed,notools/直连档行为不变),只待 ReAct 跑通后再验。

**下一步(留痕)**:先修 ReAct 健壮性——最可能的直接修法是把 `ToolAgentEngine` 的 `recursion_limit`(现 12)调高到 ~25(repo_map 1 次 + 多次 file 读轻松超 12 步);这属 `tool-calling-review` 健壮性,另开 change。修通后重跑 `pipeline-repomap` 拿到正常工具档基线,再跑 `pipeline-repomap-fpverify` 完成 Step 3 对照。

**日期**:2026-06-18

> **订正(2026-06-20,见 ADR-017)**:本 ADR 把根因判为"`recursion_limit=12` 太低、调高到 ~25 即可"——**这是错的**。调到 25 后 recall 仍 0.429、失败率不变。真因是**评测 harness 把工具指向了 cwd**(详见 ADR-017),与 `recursion_limit` 无关。

## ADR-017 · ADR-016 根因订正:评测 harness 把工具指向 cwd 致合成用例无界乱逛(非 recursion_limit)

**起点**:按 ADR-016 的处方,把 `ToolAgentEngine.recursion_limit` 由 12 调到 25 后重跑 `pipeline-repomap --runs 1 --judge`。**没用**:仍 ~25 次审查员撞 `Recursion limit of 25 reached`、失败率仍 ~40%、recall 0.429(反比 0.476 更低)。证伪"上限太低"假设——把上限当旋钮拧是没找根因的瞎猜。

**系统化定位(把失败按 case 归位)**:失败**全集中在 `clean_*` / `complex_*`(合成内联用例)**;7 条 repo-backed 用例(`repomap_*`/`file_*`)**零失败**。

**根因(单审查员流式复现坐实)**:合成内联用例 diff 写在 YAML 里、**磁盘无对应 repo 快照**;但工具档下 runner 仍按 `repo_root = case.repo_path or os.path.abspath(args.repo_base or ".")` 回退建工具会话——`case.repo_path` 为空、又没传 `--repo-base` 时,**回退成 `"."` = cwd = `services/agent`**。而该目录恰好装着整个评测数据集(`evals/dataset/repo/**` 的 Java 夹具)。于是审查一个 `clean_*` 用例时:`get_repo_map` 扫到的全是**别的用例**的 Java 文件,目标文件(如 `ConfigLoader.java`)磁盘不存在 → "文件不存在";审查员被 prompt 要求"补够上下文再判",却只能拿到**真实但完全无关**的他人文件,永远不自信 → 在 `file_npe_contract`/`file_path_traversal`/`repomap_npe_delegate`… 之间一个个乱读直到撞顶失败。失败的审查员被 `except` 静默跳过、零产出,**同时压低 recall(真问题没人报)和 FP(乱报也没发出来)**——后者解释了为何崩塌档的 clean 误报率虚低。

**修复(纯 evals,零碰 `src/codeguard_agent/**`)**:新增纯函数 `evals/profiles.py::case_repo_root(case_repo_path, repo_base)`——**只有用例自带真实快照(`repo_path`),或用户显式 `--repo-base` 断言有真实工程时**才返回 repo 根;否则返回 `None`,**绝不隐式回退 cwd**。`runner.py` 据此:`None` 的合成用例本条按**直连(无工具)**跑(它们本就是 diff-only 用例,无跨文件真值可查),repo-backed 用例照旧走工具。

**验证(`--runs 1 --judge`,修复前后同环境)**:

| | recursion 失败 | recall | precision | F1 |
|---|---|---|---|---|
| 修复前(@limit 25) | 25 | 0.429 | 0.462 | 0.444 |
| **修复后** | **1** | **0.893** | 0.500 | 0.641 |

合成 complex 用例现在产出真实 TP(complex_import 4 TP、complex_config 3 TP)而非全 FN 循环。clean 误报率回到 0.750——这是 diff-only 直连的**真实**过度上报短板**显形**(此前被循环失败掩盖),非本次回归,归 ADR-004 老账。

**残留(production 健壮性缺口,独立、可选)**:修复后仍有 **1 例** `repomap_npe_abstract_001`/logic 撞 limit=12——这是**有真快照的 repo-backed 难例**(抽象类→多实现),审查员**合法地**需 >6 次工具调用而超步。说明真实仓库(含 CLI 生产路径)下,审查员**无工具调用预算**,绕的难例会撞 `recursion_limit` 被静默跳过。该 case 有其他审查员兜住、recall 未损。正解不是再拧大数字,而是给审查员加**优雅的工具调用预算**(到顶就以现有信息收尾出结论,而非死循环被丢)。另开 change,不并入本次。

**教训**:① 没找根因就拧旋钮(`recursion_limit`)=瞎猜,被一次实跑证伪;② 多组件系统先按"失败归位"切分(哪类 case 失败)再深挖,比盯单条日志快得多;③ 工具的"仓库根"必须是显式真实路径,**隐式回退 cwd** 在"脚本目录恰好含数据"时会喂出以假乱真的无关上下文。

**Step 3 / Group 6 实测(2026-06-20,`--runs 3 --judge`,工具档基线已恢复;DeepSeek 审查 + qwen3.7-max 裁判,实际 0 个 403)**:把审查员经工具获取的 diff 外上下文喂给误报复核员后,before(`pipeline-repomap`,复核关)vs after(`pipeline-repomap-fpverify`,复核开+喂上下文):

| | Precision | Recall | F1 | 误报率(clean) |
|---|---|---|---|---|
| before(复核关) | 0.531 (±0.022) | 0.929 (±0.029) | 0.675 | 0.833 |
| **after(复核开+喂上下文)** | **0.640 (±0.018)** | 0.869 (±0.045) | **0.737** | **0.500** |

**净效果**:Precision +0.109、F1 +0.062、clean 误报率 −0.333;Recall −0.060。

**6.3 判定(按切片逐 case 核,3 跑累计 TP/FP/FN)——通过**:

- **repomap+file 切片(Step 3 靶心,需工具真值)**:TP `20→20` **完全守住**、FP `16→9` 近腰斩。4 个最难的跨文件 NPE 用例 `repomap_npe_{crossfile,abstract,delegate,iface_impl}` 在 before/after **全是 3/0/0**——**复核员喂了上下文后没误删任何一个跨文件真问题**,正是 Step 3 要证的"查而非猜删"。`file_path_traversal` FP 5→1、`file_missing_authz` 11→8,TP 不丢。
- **clean 切片**:FP `20→12`,凭空乱报被压,无 TP 可丢。
- **complex 切片**:TP `58→53`、FN `5→10`——整体 Recall −0.06 几乎全在这。原因合理且与 ADR-015 方法论一致:complex 是合成 diff-only 用例,现走直连**无工具上下文**,复核员对密集多问题 diff 在"无上下文"下偏删(`complex_discount` −4、`complex_import` −2)。**即"有上下文则保真、无上下文则偏删",反过来印证了上下文注入的价值。**

**结论**:Step 3(给复核员喂审查员获取的 diff 外上下文)**有效且达成设计目标**——在需要跨文件真值的切片上,复核员保住全部真问题、同时砍掉一半 FP;Precision/F1/clean 误报率三项明显改善,Recall 代价仅 0.06 且集中在"复核员本就拿不到上下文"的 diff-only complex 上(非 Step 3 适用域)。change `fp-verify-reviewer-context` 据此归档。

**残留(同前)**:工具档 3 跑共 ~2–3 次 repo-backed 难例审查员撞 `recursion_limit=12` 被静默跳过(~1/跑),即上文"production 审查员无工具预算"缺口,另开 change。

**日期**:2026-06-20

## ADR-018 · 审查员撞递归上限时优雅降级(无工具直连复审),而非被静默丢弃

**背景**:ADR-017 修掉评测 harness 根因后,工具档仍残留 ~1/跑 的 repo-backed 难例审查员撞 `recursion_limit=12`——这是**有真快照的合法难例**(如 `repomap_npe_abstract_001`,抽象类→多实现需多轮"导航→细读"),不是无界乱逛。问题在于:`ToolAgentEngine.review` 抛 `GraphRecursionError` 后,被 `ReviewerStage` 的 `except` **静默跳过**,该领域(security/logic/quality 之一)的发现**全丢**,直接压低 recall。这是 production 路径(CLI 审真实仓库)也会踩的健壮性缺口。

**决策**:撞上限时**优雅降级**而非丢弃——`ToolAgentEngine.review` 捕获 `GraphRecursionError`,降级为**无工具直连复审一次**(`DirectEngine`,据 diff 产出结论)。直连无工具不会再循环,该域至少有产出。把 agent 构建+invoke 抽成 `_run_agent` 方法作可测接缝。

**为何不拧大 `recursion_limit`**(承 ADR-017):拧大只是把"多久撞顶"往后推,治不了"撞顶=静默丢弃"这个根本动作;且无界乱逛(若再现)给多少都不够。降级才是对"撞顶"这一事件的正确兜底。`recursion_limit` 保持默认 12(仍是构造参数,需要时可调),撞顶由降级接住。

**取舍(诚实记)**:降级走直连,**丢失该审查员已收集的 diff 外上下文**(gathered_context 该域为空)——撞顶的审查员其上下文本就不完整/已绕晕,退到 diff-only 结论是合理兜底,且严格优于"零产出"。更进一步的"流式留存 last state、撞顶时强制无工具收尾以保住已得上下文"留作后续(复杂度更高,暂不做)。

**验证**:单测覆写 `_run_agent` 抛 `GraphRecursionError`,断言 `review` 返回直连降级产出(非抛断、gathered_context 空)。155 passed、ruff 净、mypy 无新增。真实复现该降级是概率性的(原残留 ~1/跑),单测已确证降级逻辑,未为触发它另起 gateway 烧额度。

**日期**:2026-06-20

## ADR-019 · 工具增益首次量化实跑:工具开 vs 关有明确增益,repo_map 导航叠加在 file 之上零增量(当前数据集)

**背景**:ADR-012 落地 `get_repo_map` 后,"工具开 vs 关"的量化增益一直是欠账——先因合成数据集喂不动文件工具(ADR-009),后因评测 harness 把工具指向 cwd 致工具档崩塌(ADR-016→017)。ADR-017 修好 harness 后,这次终于能跑干净对照:同一 git(`de6e037`)、同会话、`--runs 3 --judge`、真实 DeepSeek 审查 + qwen 裁判,三档 head-to-head(`pipeline-notools` / `pipeline-file` / `pipeline-repomap`,唯一变量是工具集)。

**实测结果(2026-06-21,de6e037,--runs 3 --judge)**:

| profile | 工具 | P | R | F1 | clean 误报率 |
|---|---|---|---|---|---|
| pipeline-notools | 关 | 0.511 | 0.833 | 0.633 | 0.792 |
| pipeline-file | get_file_content | 0.547 | 0.893 | 0.679 | 0.708 |
| pipeline-repomap | +get_repo_map | 0.531 | 0.905 | 0.670 | 0.792 |

按能力切片 recall(报告"最近一次",单跑;三档均 de6e037 同会话):

| 切片 | notools | file | repomap |
|---|---|---|---|
| file | 0.762 | **1.000** | 1.000 |
| repo-map | 0.667 | **1.000** | 1.000 |
| diff-only | 0.857 | 0.857 | 0.873 |

**两条结论(诚实记)**:

1. **工具开 vs 关:有明确、now-可测的 recall 增益**。需读 diff 外文件的切片(file / repo-map)从 notools 的 0.762 / 0.667 拉到工具档的 1.000——notools 末跑漏了 3 个跨文件难例(`repomap_npe_abstract_001`、`repomap_npe_delegate_001` 漏报、`file_path_traversal_001` 漏报),工具档(file 与 repomap)8 个"需工具"难例全 TP。整体 Recall 0.833→0.893、F1 0.633→0.679,Precision/误报率不被工具拖坏(反而 file 档误报率 0.792→0.708 略降)。这证伪了 ADR-009/011 时期"测不出增益"的悬案——根因是当时数据集/harness,不是工具无用。

2. **repo_map 导航叠加在 file 之上:当前数据集零增量**。`pipeline-file` 与 `pipeline-repomap` 在 file / repo-map 切片都是 1.000;整体 R 0.893→0.905、diff-only 0.857→0.873 均落在 ±0.06 方差内,不构成可信增益。**原因**:当前 4 个 `repomap_npe_*` 难例,审查员从 diff 文本/import 就能猜到"该读哪个文件",`get_file_content` 单独即可定位细读,用不上 `get_repo_map` 的"先导航定位"。这正应了 ADR-012 设计时的前提——repo_map 要显出价值,难例必须**从 diff 里猜不到该读哪个文件**(如符号定义散在猜不到的文件、需靠 PageRank 邻域图导航)。现有难例隔离度不够。

**取舍/边界**:能力切片是单跑值(报告只记"最近一次"),方差未消;但方向被 per-case 明细佐证(notools 确实漏、工具档确实抓全),且与整体 3 跑均值一致。未据此改任何 `src/**` 代码——这是一次纯测量。

**衍生待办**:① ~~要量化 repo_map 的**独有**增益,需补"diff 内猜不到目标文件"的强隔离跨文件难例~~ → **已做并验证通过(ADR-020)**:`repomap_npe_isolated_001`(接口多实现 + 契约撒谎 + 诱饵填充)下 file 0/3、repomap 2/3,首次测出 repo_map 独有增益。② repo_map 的价值也可能在**大仓库**(候选文件多、猜不准)才显现,小快照难例区分度天然低。③ 工具利用率/耗时仍未纳入报告(HANDOFF 待办 3)。

**日期**:2026-06-21

## ADR-020 · 强隔离跨文件难例:首次测出 repo_map 相对 file 工具的独有增益(file 0/3、repomap 2/3)

**背景**:ADR-019 测出"工具开 vs 关有增益,但 repo_map 叠加在 file 之上零增量"——根因是现有 4 个 `repomap_npe_*` 难例对 `get_file_content` 太友好:快照只 3-4 文件(file 工具可"全读")、改动文件里直接写着字段类型(类型名≈文件名,顺着读即可),用不上 PageRank 导航。ADR-019 衍生待办①遂要求构造"diff 里猜不到目标文件"的强隔离难例,逼审查员走导航,才能量化 repo_map 的**独有**价值。

**决策(用例设计,A1 路线)**:新增 `repomap_npe_isolated_001`(repo-backed,16 个 Java 文件),三重隔离 + 一重"契约撒谎":
1. **接口多实现 + DI**:改动文件 `QuoteController` 持有的是**接口** `PriceCatalog`(非具体类),调 `catalog.lookup(sku).trim()`(通用名、hunk 无 import);接口有 3 实现,只有 `legacy.TariffLookupTable`(名不叫 `*Catalog`、藏 legacy 包)用 `Map.get` 找不到返回 null。
2. **契约撒谎(最关键加固)**:`PriceCatalog.lookup` 的 javadoc 明确承诺"永不返回 null"。只读 diff/接口契约的审查员会**信任契约、判 .trim() 安全而不报**(正确地相信契约);唯有导航到具体实现 `TariffLookupTable` 才看见它**违反契约**。把缺陷从"不判空的 stock NPE 气味"升级为"某实现违反接口非空契约"这一必须细读实现才暴露的真实跨文件缺陷。
3. **诱饵填充**:~10 个无关服务,其中 `ShippingCalculator` 也有同名 `lookup(String)`(按名匹配有歧义);文件多到 ReAct 12 步预算内无法"全读"。

**实测结果(2026-06-21,git 9c7e2da,--runs 3 --judge,真实 DeepSeek + qwen 裁判,该用例逐跑)**:

| profile | 工具 | 该用例 3 跑 | 命中率 |
|---|---|---|---|
| pipeline-file | get_file_content | FN / FN / FN | **0/3** |
| pipeline-repomap | +get_repo_map | TP / TP / FN | **2/3** |

**两类证据(不止指标)**:① 逐跑命中 0/3 vs 2/3——file 单独被契约+诱饵彻底挡住,repo_map 把它捞回来。② **网关日志机制级佐证**:repomap 档审查员先 `get_repo_map("") -> ok`,随即 `get_file_content(".../legacy/TariffLookupTable.java") -> ok` ×2——正是设计的"地图→定位→读对实现"导航路径,读到 bug 实现才发现 `Map.get` 返 null。这是 repo_map 价值的**首次正向实证**(此前 ADR-009/011/019 一路"工具被调但增益测不出")。repomap 整体 F1 0.703 亦**首次反超** file 0.646。

**取舍/边界(诚实记)**:① repomap 那 1/3 漏是真噪音(某跑 ReAct 没去导航或撞预算降级),单用例 recall 仍二值高方差——这是按用户决策"先做 1 个验证、再扩量"的代价;既已验证设计能区分,下一步可加 2-3 个同构强隔离用例把信号做稳(HANDOFF 待办)。② `AppConfig` 装配桥未进 repo map(personalized PageRank 沿种子**出边**流,AppConfig 是指向种子的入边),故 repo_map 并不告诉审查员"哪个实现 live",而是列出全部 3 实现让其逐个细读——对"抓 NPE 风险"已足够,但若要 repo_map 直接定位 live 实现,需另想机制(留后续)。③ 纯新增 fixture + 接口契约文案,未碰 `src/**`、Java gateway 未改。

**日期**:2026-06-21

## ADR-021 · 裁判 harness 修复:disable-thinking 请求体按厂商分派 + 千问强制 thinking 模型不兼容 forced tool_choice

**背景**:实跑 caller 难例(ADR-022)时,qwen 裁判**整轮 44+ 次 400 失败**全部回退规则尺,而同 .env 同模型早些时候(ADR-019/020)还是 0 失败。规则尺位置严格,把"语义正确但报在改动行而非受害行"的结论误杀为 FN——一度让我把"caller 盲区"误判成真。根因有两层:

1. **disable-thinking 请求体格式厂商相关,但代码只发 DeepSeek 格式**。`llm/client.py` 在 `disable_thinking` 时恒发 `{"thinking": {"type": "disabled"}}`(DeepSeek 专用);裁判是千问(dashscope),它认的是 `{"enable_thinking": false}`。`.env` 显式设了 `CODEGUARD_JUDGE_DISABLE_THINKING=true`,于是把 DeepSeek 格式塞给千问 → 千问无视 → thinking 关不掉。config.py 注释其实早警告过"那个 extra_body 是 DeepSeek 专用,塞给千问会出错"。
2. **千问带日期的 `qwen3.7-max-2026-06-08` / `-2026-05-17` 现在强制 thinking**(实测:发 `enable_thinking:false` 直接 400 "restricted to True"),而 **thinking 模式不支持 `tool_choice=required`/object**——正是 `with_structured_output(method="function_calling")` 所需。早些时候那俩日期版还不强制,故 0 失败;千问中途改了行为。

**决策**:

1. **`build_llm` 按 `api_base_url` 分派 disable-thinking 格式**:`dashscope` → `{"enable_thinking": false}`,否则(DeepSeek 及默认)→ `{"thinking": {"type": "disabled"}}`。抽成 `_disable_thinking_body()` 纯函数 + 单测(3 例)。这样对**允许关 thinking 的千问模型**就能真正关掉、function_calling 结构化输出可用。
2. **裁判模型改用不强制 thinking 的 `qwen3.7-max`(alias,无日期)**(用户操作):实测该 alias 接受 `enable_thinking:false` + function_calling,裁判端到端恢复、整轮 **0 失败**。带日期的强制 thinking 版不可用于走 forced tool_choice 的结构化裁判。

**备选(留后续)**:若将来只能用强制 thinking 的裁判模型,改用 `method="json_mode"`(response_format=json_object,不发 tool_choice,thinking 模式下实测可跑通)——但需给裁判独立的 structured_method(现 `matcher.py` 读全局 `CODEGUARD_STRUCTURED_METHOD`,与 DeepSeek 审查员共用,不能全局切 json)。本轮用换 alias 解决,未动 matcher。

**教训**:**裁判这层 harness 一旦悄悄退化(全回退规则尺),会系统性误导一切判图评测**。跑判图前应探活裁判(本轮已有此习惯,但"HTTP 200 探活"不够——要探到结构化调用层,因为 200 的普通对话不等于 forced tool_choice 可用)。

**验证**:`_disable_thinking_body` 3 单测绿;裁判端到端结构化调用 OK;ADR-022 的 before/after 全程 0 裁判失败。

**日期**:2026-06-21

## ADR-022 · repo_map 纳入直接调用方:补结构盲区(实现成立),但本 eval 未证出审查员级增益(诚实记)

**背景**:ADR-020 发现 `get_repo_map` 的结构盲区——只输出"被 diff 邻域指向的定义(callees)",无人引用的叶子调用方(装配/入口/上游服务)永不进地图(`AppConfig` 消失)。`repo-map-context` spec 第一条原已写"列出引用这些符号的其它位置"但实现未兑现。change `repomap-include-callers`:补"直接调用方纳入"。

**决策(实现)**:`RepoMapRanker.findDirectCallers`(纯加法,不改 `rank()`:取种子文件定义的符号 → 引用它们的非种子文件 → 返回这些文件 DEF 签名)+ `RepoMapRenderer` 独立保留预算的 callers 段(上限 K + "(+N more)")+ `RepoMapBuilder` 串联。文件级精度(档 A),精确调用语句由 `get_file_content` 兜底。Java +7 单测(38)、Python 工具描述同步。

**eval-first 结论(诚实记,这是本 ADR 最值钱的一条)**:为证价值,造了 caller 盲区难例 `repomap_npe_caller_001`(diff 把 `MemberDirectory.displayName` 从非空默认改成 `cache.get` 可空;上游叶子调用方 `GreetingService.greet` 不判空直接 `.toUpperCase()` → NPE;bug 在 caller)。

| | caller 案 3 跑 | 审查员是否读 GreetingService |
|---|---|---|
| before(无 callers 段)+ **工作裁判** | **TP/TP/TP = 3/3** | **0 次**(纯靠 diff 推理) |
| after(有 callers 段)+ 工作裁判 | TP/TP/TP = 3/3 | 读了(导航到) |

**两档都 3/3,callers 段未带来可测增益**。机制成立(probe 证地图确实从"缺 GreetingService、只列伪相关 TaxRuleSet.put"变为列出 GreetingService+MemberReport;after 跑审查员真去读了),但**审查员不靠它也能抓到**——"改动让方法返回 null → 调用方 NPE"是可从 diff 推理的警告,语义裁判直接给分,无需定位具体 caller。**先前看到的"before 0/3"是 ADR-021 坏裁判的假象**,非 callers 缺位所致。

**为何仍 ship(决策)**:① 满足 spec 既有要求 + 补 ADR-020 实证的真实结构盲区;② probe 验证机制、Java/Python 单测全绿、**全量 23 案无回归**(before-clean R 0.911 vs after R 0.878 在 ±0.04 噪音内,caller 案两档相同);③ **一个指标抓不到的好处**:有 callers 段时审查员能给"GreetingService:20 会 NPE"的**具体可定位**结论,而非泛泛"调用方可能 NPE"(裁判都算 TP,但前者对开发者有用得多)。诚实标注"审查员级增益未由本 eval 证出"。

**残留/边界**:要证出 callers 段**必要**,需"危险无法从 diff 推断、只有读具体 caller 才暴露"的难例——比 ADR-020 的"契约撒谎"更窄更难,留后续(可选)。`AppConfig` 装配桥这类纯叶子 caller 现已能进 callers 段(原 ADR-020 残留待办②的一部分得到结构性解决)。

**日期**:2026-06-21

## ADR-023 · 评测加"工具使用画像":让"工具到底有没有被用上"可观测(直击 ADR-022 的盲点)

**背景**:ADR-022 的尴尬根因是**测不出工具有没有被用上**——caller 案 before/after 都 3/3,但当时无法从报告判断审查员是"真调 get_repo_map 导航、读了 callers 段"还是"纯靠 diff 推理蒙对"。判断只能靠人去翻日志。后续每加一个重工具(get_method_definition/get_call_graph)都会重蹈覆辙:加了、测不出、只能靠 spec-completeness 兜底 ship。**度量是加重工具前必须先铺的地基**(eval-first 闭环的缺口)。

**决策**:复用管线已有的工具上下文捕获(`engines._extract_gathered_context` 抓 `tool/args/content`,经 ReviewerStage 跨审查员去重),不新造采集逻辑:
- **侧信道**:`PipelineOrchestrator.run` 加可选 `trace_sink: list`——传入则把 `context.gathered_context` 追加进去。**刻意不进 `ReviewResult`**(产品输出不掺工具痕迹,守 ADR-001)。
- **纯函数** `evals/tool_usage.py::summarize_tool_usage(trace) → ToolUsage`:汇出 `tool_calls / tools_used / repomap_called / repomap_caller_section_read / files_read`。`repomap_caller_section_read` 靠地图返回里是否含 `RepoMapRenderer.CALLER_HEADER` 的"直接调用方"标记判定——这正是 ADR-022 想答的那一问。
- **挂载**:`run_once` 在算完 outcome 后,若 trace 非空则 `outcome.tool_usage = summarize_tool_usage(trace)`(空 trace→None,无工具档报告/归档不出现满是 `—` 的行)。`MatchOutcome` 加 `tool_usage` 字段、报告加「工具使用」表、`archive._outcome_dict` 持久化。
- **边界**:`tool_calls` 是**去重后取得有效上下文**的调用条数(非原始次数:gathered_context 按(工具,参数)去重、且仅含有返回内容的调用)。够回答"用没用上",不追求精确调用计数。

**收益**:报告里一眼可读"某用例判 TP 但 repo_map/callers 段全为 — = 工具没用上、纯靠 diff 推理蒙对"。下个重工具落地时,先用它确认"新能力没被 diff 推理 + 语义裁判绕过",再谈增益(承 ADR-022 教训)。

**测试**:`tests/test_tool_usage.py` +6(callers 段判定、files_read 解析去重排序、脏入参回退、空 trace、结构化伪工具剔除)。全量 Python 159 过(5 个 test_tool_client 失败为本机 conda SSL 证书路径坏,`httpx.Client()` 单独构造即复现,与本改动无关)。

**实跑验证 ADR-022(已做)**:起网关 + 探活裁判到结构化层(qwen3.7-max,function_calling 正常)后,实跑 `repomap_npe_caller_001`(after 态)。结果:**`repomap_called=✓`、`repomap_caller_section_read=✓`**,读取文件含 `GreetingService.java`(受害叶子调用方)+ `MemberReport.java`(安全对照 caller)+ `MemberDirectory.java`(改动文件)。→ **坐实 ADR-022:after 态审查员确实经 callers 段导航并读了 GreetingService**;而 before 态同样 3/3,说明"改成可空→调用方 NPE"这步 diff 推理也能 catch,故 callers 段被用上了、但本案不靠它也能蒙对。度量第一次把"工具有没有用上"从翻日志变成报告里一眼可读。

**验证中发现并修掉的 bug**:`create_agent(response_format=ReviewResult)` 把"产出结构化结果"实现为一次同名工具调用,经 `_extract_gathered_context` 以 ToolMessage 混进 gathered_context。首跑画像里 `tools_used` 含 `ReviewResult`、`tool_calls` 由真实 4 虚高到 7(`repomap_caller_section_read` 不受影响,故结论不变)。修复:`summarize_tool_usage` 先按 `_STRUCTURED_SENTINELS={ReviewResult.__name__}` 剔除伪工具再统计;加 1 单测;重跑确认 `tools_used` 干净、`tool_calls=5`(1 地图 + 4 文件)。

**SSL 旁注**:本机 conda 环境 `SSL_CERT_FILE` 指向不存在的 `…/envs/codeguard/ssl/cacert.pem`,`httpx.Client()` 一构造即 FileNotFoundError——会挡掉一切真实 eval。跑评测前需 `export SSL_CERT_FILE=$(python -c "import certifi;print(certifi.where())")`。已记入 HANDOFF 速查。

**日期**:2026-06-21

## ADR-024 · 编排迁移到 LangGraph supervisor 状态图(阶段 4):可读性/可扩展为先,增益非目标

**背景**:原编排是 `for stage in stages: ctx = stage.execute(ctx)` 的线性循环,真正的多 agent 并行被埋在 `ReviewerStage` 内部的 `ThreadPoolExecutor`,编排层看不见 fan-out;`SummaryStage` 自称"软路由"却从不调度(三审查员永远全跑)。这是规划阶段刻意留下的 Phase 4 占位。本次升级为 LangGraph `StateGraph` + supervisor 调度。**明确取舍:不追求评测增益(甚至预期评测更吵),目标是流程拓扑可读、多 agent 调度做成真决策、为后续扩展铺地道接缝、并掌握 LangGraph。** change:`langgraph-supervisor-orchestration`。

**决策**(详见 change 的 design.md D1–D12):
- **手搓 supervisor**(D1),不用 `langgraph-supervisor` 包——后者面向对话式 handoff,与"扇出领域审查员→收结构化发现→再决策"结构不合;手搓零新增重依赖、控制力/可读性最高。
- **State + reducer**(D2):`issues` 用 `operator.add` 自动 fan-in;`gathered_context` 用**自定义去重 reducer**(按 `(tool,args)`,不能用 add 否则丢去重);**`final_issues` 单独承接聚合/过滤后的结果**,避免与加法 reducer 的"替换 vs 累加"冲突。
- **图拓扑 + Send**(D3):`START→[summary]→supervisor──(条件/Send)→[security|logic|quality]→supervisor(循环)→aggregation→fp_filter→END`。supervisor 输出 `SupervisorDecision{action,reviewers,focus_notes,reason}`,用 `Send` 动态扇出子集,审查员回边到 supervisor 形成补派/重派循环。
- **双重护栏**(D4,直面 ADR-016/018):`iteration` 计数达 `MAX_REVIEW_ROUNDS`(=3,D10)强制 finish + 图 `recursion_limit`(=50)兜底。
- **分路径默认**(D9):`enable_supervisor` CLI/产品默认**开**(展示调度);评测受控档(notools/file/repomap)经 profile 强制**关**=确定性全派,保控变量纯净;另设 `pipeline-supervisor` 观测档置开。mock(llm None)一律走确定性,supervisor 不发真实调用。
- **门面不变**(D7):`PipelineOrchestrator.run(...)` 签名与 `ReviewResult` 输出原样保留,`cli.py`/`evals/runner.py` 仅各加传一个开关参数;`gathered_context` 仍只经 `trace_sink` 流出、不进 `ReviewResult`(守 ADR-001)。
- **节点级错误隔离**(D8):审查员节点自捕异常返回空发现 + 告警(LangGraph 默认异常上抛,必须显式兜);fp 复核沿用"失败/None 一律保留 issue"。
- **审查员节点第一刀用普通函数**(D12):内部黑盒复用现有 `ToolAgentEngine`/`DirectEngine`("图中有图":审查员内部本就是 `create_agent` 的子图);子图化作后续可选精炼。
- **旧线性管线下线**(D11):确定性图证绿后删除 `build_default_pipeline` 与 `for` 循环驱动;stage **逻辑**被节点复用故不删。

**实现要点 / 踩到的真坑**:LangGraph 的 reducer 对"下游节点要替换被 reduce 的键"无能为力——聚合/过滤若返回 `{"issues": ...}` 会被 `operator.add` 当成追加(翻倍)。解法是引入无 reducer 的 `final_issues` 承接。mock 三审查员都被派发会把单条 mock 三倍化——让仅 `security` 在 mock 下返回一次 mock 结果、其余空,既保端到端连通又不三倍化。

**测试**:`tests/test_graph_orchestration.py` +17(reducer 去重、supervisor 确定性/智能/护栏/兜底、节点错误隔离、mock 单条、门面侧信道不进 ReviewResult、真实编译图 fan-in、supervisor 派发→finish 循环)。全量 Python **181 过**(164→181),ruff 干净。门面回归:原 164 测全绿,确认 `ReviewResult` 与改造前同构。

**未做(诚实记)**:task 6.3「确定性模式跑一次真实评测确认指标与改造前一致」**未跑**——需起 gateway + 真实 DeepSeek/qwen,本轮按"工程正确性已坐实 + 门面回归全绿"收口,留待下次真实环境复跑(同 ADR-004/008 不编数字原则)。

**日期**:2026-06-22

---

## ADR-025 · 审查员升级为 LangGraph 编译子图(D12 第二刀):节点内 invoke + 显式父↔子映射

**背景**:ADR-024 的 D12 第一刀把审查员做成"普通函数节点,内部黑盒调引擎",并把"升级为子图换内部步骤可见性"列为可选第二刀。本次落地第二刀,目标仍是 ADR-024 的总基调——**可读性 / 学 LangGraph 嵌套 / 可扩展为先,增益非目标**。

**决策**:
- **审查员=编译子图**:每个领域审查员构造成 `StateGraph(ReviewerState)`,内部三节点 `prepare → review → collect`(职责单一:聚焦 diff/prompt 组装 → 跑引擎得单一 `ReviewOutcome` → 归一化为产出键)。`build_reviewer_subgraph()` 产出 `compile()` 后的子图。审查员内部流水线由此在图层面显式可见(`sub.get_graph().nodes`),后续给某审查员加步骤(独立工具采集 / 自我复核节点)即在子图加节点,父图无感。
- **挂载方式=节点内 invoke(而非直接挂子图)**:`make_reviewer_node` 是薄包装节点,在内部 `subgraph.invoke(显式投影的输入)`,只回传产出键。**这是被一个真实坑逼出来的正确范式**(见下),也正面处理了 ADR-024 预警的"父子 state 映射"。
- **ReviewerState 独立 schema**:私有工作键(`eff_diff/user_prompt/outcome`)不与父图 `ReviewState` 同名 → 不外泄、并行无冲突;产出键(issues/gathered_context/...)同名 → 经父图 reducer fan-in。
- **不动 create_agent 边界**:`ToolAgentEngine` 内的 ReAct 图(`create_agent`)仍封在 `review` 节点里。把它也内联为真正的子子图涉及 `MessagesState ↔ ReviewerState` 映射,risk 高、收益边际,**显式留作后续可选深化**(诚实记:本刀只做到"审查员级"子图,ReAct 每一步仍不可见)。

**踩到的真坑(关键学习点)**:第一版把编译子图**直接挂作父图节点**(`g.add_node(name, build_reviewer_subgraph(r))`),三审查员并行 fan-out 时报 `InvalidUpdateError: At key 'diff_text': Can receive only one value per step`。根因:子图直接挂载时,其 schema 里**只读**的共享键(diff_text)会在子图结束时被**回写**父图;三路并行 → 对无 reducer 的 LastValue 键并发写 → 冲突。**根治**:改用"节点内 invoke 子图"范式,显式投影输入 + 只回传产出键,杜绝只读键回写。——这正是 LangGraph 两种子图范式(共享 schema 直接挂 vs 异构 schema 节点内调)里,**异构 + 并行 fan-out 必须选后者**的活教材。

**不变量保持**:节点级错误隔离(review 内 try/except → 空 outcome + 告警)、mock 不三倍化(仅 security 合成)、gathered_context 侧信道、门面签名——全部原样保留。

**测试**:`test_graph_orchestration.py` 改 2 + 增 1(子图暴露 prepare/review/collect 内部节点)。全量 Python **182 过**(181→182),ruff 干净。`pipeline-repomap-fpverify` 真机评测未重跑——本刀只改审查员节点的内部封装方式,父图拓扑 / 引擎调用 / 产出契约不变,且全套含真实编译图 fan-in/supervisor 循环的集成测试通过,故按"工程正确性已坐实"收口(同 ADR-024 收口口径)。

**日期**:2026-06-22

---

## ADR-026 · LangGraph checkpoint 持久化与中断恢复:两档后端、默认不启用、Human-in-the-loop 另开

**背景**:当前审查图是 `graph.invoke(initial_state)` 一次性执行,API 中途失败则从头重跑、已审完的领域和已收集的工具上下文全部白做。LangGraph 内置的 checkpoint 能力让图在每一步自动持久化 State,故障后用同一 `thread_id` 从断点恢复——这是"图已经画好、就差一个参数"的自然下一步。Human-in-the-loop（主动 `interrupt()` 暂停等人决策）依赖 checkpoint,但属于独立能力,另开 change。change: `langgraph-checkpoint-interrupt`。

**决策**:

1. **checkpointer 在 `PipelineOrchestrator` 层创建注入图**(D1):`build_review_graph(checkpointer=None)` 接收可选 checkpointer 参数;`PipelineOrchestrator.__init__` 按 `CODEGUARD_CHECKPOINT_BACKEND` 环境变量创建对应实例。支持的 checkpointer 后端:MemorySaver(内存,`langgraph` 自带)、SqliteSaver(文件,需单独安装 `langgraph-checkpoint-sqlite`)。

2. **默认不启用**(D2):`CODEGUARD_CHECKPOINT_BACKEND` 默认为空（不启用）,向后兼容、零性能开销。需显式配置才激活。

3. **thread_id 透传**(D3):`PipelineOrchestrator.run(thread_id=None)` → 当 thread_id 非 None 且 checkpointer 存在时构造 `config = {"configurable": {"thread_id": thread_id}}` → `graph.invoke(initial_state, config)`。相同 thread_id 重跑从最后 checkpoint 恢复;不传时等价当前一次性执行。CLI 加 `--thread-id` 参数。

4. **SqliteSaver 文件路径可配置**(D4):`CODEGUARD_CHECKPOINT_DB` 默认 `"codeguard_checkpoints.db"`。

5. **不做 Human-in-the-loop**(D5):本次不改审查员撞 `recursion_limit` 的降级行为(ADR-018 保持不变)、不在 supervisor finish 前暂停等待人工确认。interrupt 机制留给下一个 change ——等 checkpoint 跑稳后再决定哪些点值得暂停。

**工程正确性**: 190 单测全绿(182→190,+8:MemorySaver 中断恢复一致 / 同 thread_id 不重复审查 / 不同 thread_id 独立 / 工厂函数优雅降级(含 SqliteSaver 未安装时的降级) / 不传 checkpointer 全链路行为不变)。ruff + mypy 干净。

**局限**:SqliteSaver 需要单独安装 `langgraph-checkpoint-sqlite` 包(当前 conda 环境未装),工厂函数在未安装时优雅降级为 None。checkpoint 文件随审查次数增长,暂无自动清理策略。

**日期**:2026-06-27

<!-- 后续在这里继续追加 ADR-027、ADR-028 …… -->
