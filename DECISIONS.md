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

---

## ADR-027 · Human-in-the-loop:两个固定 interrupt 点 + 交互式 CLI + 开关控制

**背景**:checkpoint（ADR-026）已让图执行状态可持久化恢复，但 supervisor 判 finish 和审查员撞 recursion_limit 这两个不可逆决策仍是全自动的。前者可能漏审（LLM 自判"够了"但实际不够），后者自动降级丢上下文（ADR-018）。在 checkpoint 基础上加 `interrupt()` 主动暂停，让人把关这两个关键决策。change: `langgraph-human-in-the-loop`。

**决策**:

1. **触发点写死，开关控制**（D1）：两个 interrupt 点是写死的——supervisor 判 finish 后 + 审查员撞 `GraphRecursionError` 时。由 `enable_human_in_the_loop` 开关统一控制，默认关（向后兼容）。不让 LLM 决定"该不该问人"——确定性行为、零额外开销。

2. **依赖 checkpoint**（D2）：`interrupt()` 需要 checkpointer。`enable_human_in_the_loop=True` 且 `checkpointer` 非 None 时才调 `interrupt()`；否则跳过。

3. **交互式 CLI 默认 + `--non-interactive` 开关**（D3）：默认终端对话循环（`input()` + 命令解析）。supervisor finish 时支持 `list`/`retry`/`focus`/`help`/回车；撞限时支持 `retry`/`skip`/回车。`--non-interactive` 模式下打印状态 + 退出码 2。

4. **resume 协议**（D4）：resume action 统一为 `continue`/`retry`/`skip` 三种。supervisor finish 的 `retry` 可带 `reviewers` 和 `focus_notes`。

5. **修 ADR-018**：撞限时的 `continue` action 以已收集上下文调 `DirectEngine` 收尾，不再丢弃已读到的文件上下文。

**工程正确性**:195 单测全绿（190→195,+5 HITL 测试:对话框命令解析、HITL 关闭时无 interrupt、确定性模式不触发、默认 false 配置）。ruff + mypy 干净。

**局限**:交互式 `input()` 在 stdin 重定向时立即返回 EOF（CI 场景需 `--non-interactive`）。`list` 命令当前打印 payload 概要，完整 issues 渲染留待后续。

**日期**:2026-06-27

---

## ADR-028 · LangGraph 1.x interrupt 行为变更 + state 序列化安全 + 集成教训

**背景**:HITL（ADR-027）实装后在真实环境（DeepSeek + Java 工具服务 + checkpoint）实跑验证，审出 6 个问题但最终报告显示"未发现问题"。排查发现两个独立根因叠加导致 HITL 中断链路完全断裂。

**根因 1 — LangGraph 1.2.4 `interrupt()` + `invoke()` 不抛异常**:

LangGraph 0.x 中 `interrupt()` 抛出 `GraphInterrupt`，调用方 `except` 捕获后进入对话。1.2.4 中行为变更：`invoke()` 在 `interrupt()` 触发后**不抛异常**，而是在返回的 state 中写入 `__interrupt__` 键，图正常返回。CLI 的 `except GraphInterrupt` 永不到达 → 拿到的 state 停在中断点（`issues=6, final_issues=[]`）→ "未发现问题"。

修复：在 `PipelineOrchestrator.run()` 中 `graph.invoke()` 返回后检测 `__interrupt__` 键，手动 `raise GraphInterrupt(...)` 让 CLI 正常捕获。

**根因 2 — state 含不可 msgpack 序列化对象**:

`ToolClient`（含 `httpx.Client`）、`ChatOpenAI` 等对象被放入 `ReviewState`/`ReviewerState` TypedDict。checkpoint 开启时 LangGraph 对 state 做 msgpack 序列化 → `TypeError: Type is not msgpack serializable: ToolClient`。此前 checkpoint PR 时未被发现，因为当时子图未传 checkpointer（本次才补上）。

修复：将 `llm`/`tool_client`/`fp_verify_llm` 从两个 TypedDict 中移除，改为通过 `build_review_graph()` → 节点工厂函数 → 闭包传递。全部节点函数改为工厂模式（`_supervisor_node(llm)`、`_summary_node(llm, tool_client)` 等）。

**附随改进**:

1. **ReAct 递归上限可配置**（`CODEGUARD_REACT_RECURSION_LIMIT`，默认 24，原 12）：实测 12 步太紧，工具往返耗 2 步只能做 ~5 次调用，diff 文件稍多就撞墙。24 步可做 ~8-10 次工具调用 + 思考。
2. **结构化结果兜底增强**：`_extract_result` 从"只扫最后一条消息"→"扫描全部消息"、新增 `dict`→`ReviewResult` 转换（部分模型返回 dict 非 Pydantic 实例）、告警带诊断信息。
3. **HITL 安全守卫**：`enable_hitl=True` 但 `checkpointer=None` 时自动告警降级，防止 resume 死循环。
4. **`enable_hitl` 参数贯通**：`ToolAgentEngine.review()` 加 `enable_hitl` 参数——HITL 开时不吞 `GraphRecursionError`，让它传播到上层 interrupt handler（修 ADR-027 死代码）。

**工程正确性**:195 单测全绿。真实实跑（springboot-review-demo, 4 类植入缺陷）：全部命中——SQL 注入 x2（findById + findByEmailDomain）、路径遍历（readConfig）、NPE（getEmailList 未判空）、资源泄漏（writeAuditLog）、硬编码密钥（EXPORT_SECRET），共 6 条。

**集成教训（本 session 最值钱的一条）**:

1. **新版本库关键 API 先用最小脚本验证实际行为**——凭 LangGraph 0.x 文档印象写 `except GraphInterrupt`，但 1.2.4 行为已变。
2. **异常处理分支必须可测**——`except GraphInterrupt` 从未被集成测试触发过，是死代码。
3. **排查从数据流最底层开始**——问题表象是"审查结果为空"，直觉反应是模型/prompt 不行，绕了一圈才发现是异常传播断了。第一问应该永远是"数据流到了哪一步"。

**日期**:2026-06-27

## ADR-029 · Supervisor 调度准确性诊断——首轮派发应改确定性规则

**背景**:用纯逻辑错误的 demo 项目（`springboot-review-demo`，SQL 注入/路径遍历/硬编码密钥已修）测试 Codeguard 调度员是否能根据 diff 内容准确派发审查员。预期只派 `logic`，实际每次都全派 `security/logic/quality`。

**诊断过程**（2026-06-28，三轮测试）:

1. **第一次运行**:改动未 commit，`git diff HEAD` 含删除的旧代码行（SQL 注入等），LLM 审查员看到 diff 中的 `-` 行并报告了它们。→ 修复：commit 后再做纯逻辑改动。

2. **第二次运行**:Supervisor 兜底策略"全派三个"导致 security 审查员被派出，花 13+ 次工具调用找不到安全问题，撞 recursion limit（24 步），进入 HITL 中断。→ 临时修复：将兜底策略改为摘要推断（`_guess_domains_from_summary`）+ 提高 recursion limit 到 48。

3. **第三次运行**:Summary 阶段已正确识别 `{security: 0, logic: 1, quality: 1}`，但 Supervisor 仍派发 security。根因拆解：
   - `_render_supervisor_user` **没传** `file_focus` 结构化数据给 Supervisor LLM，只传了文本摘要
   - `supervisor-system.txt` 写"不确定时宁可多派"——生产策略（recall > precision）
   - **两个 LLM 串行猜测无信息增量**:Summary LLM 猜一轮 → Supervisor LLM 再猜一轮，第二轮没有新增信息

**临时改动**（已生效，治标）:

| 改动 | 位置 | 效果 |
|------|------|------|
| `_render_supervisor_user` 新增 `变更文件领域分布` 行 | `graph.py` | Supervisor 看到 `{security:0, logic:1, quality:1}` |
| prompt 收紧：文件数为 0 禁止派发 | `supervisor-system.txt` | 禁止"顺手多派一个" |
| 兜底策略改为摘要关键词推断 | `graph.py` 三处 fallback | 不再无脑全派 |
| recursion limit 24 → 48 | `config.py` | 审查员有更多工具调用额度 |
| 文件类型白名单放宽 | `FileAccessSandbox.java` | 新增 xml/properties/yml 等 |

**根因分析——Supervisor 职责混乱**:

Supervisor 当前承担三种不同性质的职责：

| 职责 | 性质 | 应该怎么做 |
|------|------|-----------|
| ① 首轮派谁 | 基于 LLM 预测，与 Summary LLM 串行猜测 | **确定性规则**：`file_focus` 里哪些领域有文件就派哪些 |
| ② 补派/重派 | 基于审查产出做决策，有信息增量 | 保留 LLM 决策 |
| ③ 终止判断 | 必要的护栏 | 保留，可叠加确定性兜底 |

**决策**:首轮派发不应经过 LLM——Summary 阶段已产出结构化的 `file_focus`，直接用确定性规则派发即可。Supervisor 的价值在 ②③。

**下一步讨论**:将 Supervisor 拆为 `router`（确定性首派）+ `supervisor`（补派/终止），消除两个 LLM 串行猜测的无信息增量问题。

**日期**:2026-06-28

---

## ADR-030 · DeepSeek API 对齐 + 韧性改进:修默认值/错误分类/文档/推理深度配置

**背景**:通读 DeepSeek API 官方文档（2026-06-30 版）后对照 Codeguard 当前调用方式，发现若干不一致与可改进点。文档关键更新：① `deepseek-chat`/`deepseek-reasoner` 2026-07-24 下线，新模型名 `deepseek-v4-pro`/`deepseek-v4-flash`；② 新增 `reasoning_effort` 参数（`high`/`max`）；③ 明确 thinking mode 下 `temperature`/`top_p` 等静默无视；④ tool call 场景下 `reasoning_content` 必须传回否则 400（关 thinking 不受影响）。

**决策**:

1. **修 `react_recursion_limit` 默认值不一致（P0 bug）**:`Settings.from_env()` 里环境变量默认 `"24"`，但 dataclass 声明 `48`、ADR-029 记录"已调至 48"。根因是 ADR-029 改 dataclass 时漏改 `from_env()` 的 `os.environ.get(..., "24")` → 实际生效的一直是 24。统一为 48。

2. **细化 `invoke_with_retry` 错误分类（P1 韧性）**:新增 `_is_non_retryable()` 判据——从异常链取 HTTP 状态码，4xx（除 429）不重试（避免余额不足 402 / 密钥错 401 白白烧重试），429 和 5xx/网络错误照常指数退避。不硬 import `openai`/`anthropic` 错误类型（守 mock 兼容 + 延迟导入习惯）。

3. **修 `.env.example` 两处错/缺**:① DeepSeek base URL 示例错误写成 `https://api.deepseek.com/v1`（多了 `/v1`），文档与 `.env` 实际均为不带后缀；② 模型名加下线预警与新旧对应说明。同时补上缺失的 `CODEGUARD_REACT_RECURSION_LIMIT` 和 `CODEGUARD_REASONING_EFFORT` 文档。

4. **新增 `reasoning_effort` 可配（P3 优化）**:加 `Settings.reasoning_effort` 字段 + `CODEGUARD_REASONING_EFFORT` 环境变量，默认空（不设=模型默认 `high`），可选 `"max"`。通过 `extra_body` 透传，非 DeepSeek 端点静默无视。`build_llm` 里将 `disable_thinking` 与 `reasoning_effort` 的 `extra_body` 合并构造，避免后者覆盖前者。

**放弃的备选**:
- **Strict mode（beta）**:要求所有 object `additionalProperties: false` + 全属性 required，与当前 `Issue` schema（`suggestion`/`confidence` 可选）冲突。schema 改动牵连面太大（prompt/CLI/evals），且普通 function_calling 当前已可用，留后续。
- **对 429 做更长退避 / jitter**:当前指数退避（1s/2s/4s）对 DeepSeek 默认 500 并发上限已够用；真正需要时再上 jitter + 更激进退避（阶段 5）。

**效果**:195 单测全绿，ruff 净。`react_recursion_limit` 实际默认 24→48，审查员工具调用预算翻倍，减少合法难例误撞递归上限的概率。

**日期**:2026-06-30

---

## ADR-031 · 架构重构：三审查员合一 + 上下文预计算 + SelfChecker 取缔聚合/误报过滤

> **已废弃（2026-07-03）**：本 ADR 被 ADR-032 取代，保留作为设计演进记录。
>
> 废弃原因：
> 1. 它把多 Agent 审查收敛成"单一 CodeReviewer"，虽然工程上更简单，但不符合本项目作为求职展示项目对"多 Agent 编排"亮点的目标。
> 2. 它仍保留一个混合式 Supervisor，职责包含覆盖率、聚焦、充分性判断与重审决策，容易重新变成不稳定的 LLM 调度面。
> 3. 它把 ContextProvider、Supervisor、CodeReviewer、SelfChecker 的职责边界写得过重，一次 change 同时改上下文、审查、裁决、工具退役与调度策略，实施风险偏大。
> 4. 它没有充分体现"发现-举证-质疑-裁决"的多角色协作协议，而这是 ADR-032 要强调的架构亮点。
>
> ADR-031 中仍可复用的思想：上下文前置、Java 只产事实、repo map 从开放式工具退为内部事实来源、SelfChecker 负责最终证据校验与去重。

**背景**:当前架构以 `security` / `logic` / `quality` 三个并行审查员为核心，通过专属工具（`find_sensitive_apis` / `find_callers` / `get_code_metrics`）和输出边界约束（"不归你管就别报"）来制造区分度。但实践下来发现三个深层问题：

1. **分类轴选错了**:安全/逻辑/质量是按"问题长什么样"（issue taxonomy）来分类，不是按"如何发现"（review methodology）来分类。三个审查员拿到同一份 diff、用同一套 LLM 推理能力、走几乎相同的思考过程——只是输出时各戴一个 filter mask。这不叫三个审查员，这叫一个审查员带三个输出过滤器。

2. **专属工具边际价值低**:`find_sensitive_apis`（危险 API 清单匹配）、`get_code_metrics`（CC/LOC 计算）、`find_callers`（调用方查表）三个都是确定性计算——不需要 LLM 推理。把它们做成工具让 Agent 调，既浪费 token（Agent 需要多轮 ReAct 去调用），又无法真正产生区分度（查表得出的结论不是"独到见解"）。

3. **审查员交集天然大**:同一个人写的一段 SQL 拼接，安全审查员看到的是注入漏洞，逻辑审查员看到的是数据流错误，质量审查员看到的是可维护性问题——三个审查员报同一个问题的不同侧面。prompt 里大段"这不归你管"的排除项本质上是在对抗 LLM 的自然倾向，维护成本高且效果有限。

经过与 aider 架构对比（repo map 自动注入而非工具调用）、Diffguard 架构参考（ASTEnricher 预计算注入 + CodeRAG 检索）、以及用户提出的 6 Agent 设计方案讨论后，做出以下决策。

**决策**:

### 1. 三审查员合并为单一 CodeReviewer

三个 `security`/`logic`/`quality` 审查员合并为一个 `CodeReviewer` Agent。不再按"输出什么类型的问题"分配 Agent，而是让同一 Agent 从多条思考路径审视代码：

- **数据流追踪**:输入从哪来、经过哪些变换、到达哪些汇聚点
- **契约/不变式校验**:方法的输入前置条件是否满足、输出后置条件是否保持
- **模式匹配**:对照常见 bug 模式目录（CWE、已知反模式）逐项匹配

不同路径可能发现同一个问题（如 SQL 拼接被数据流和模式匹配同时命中），重复交给 SelfChecker 去重——不再靠"边界约束"来避免重复。

### 2. ContextProvider：确定性预处理节点（非 Agent，0 token）

新增 `ContextProvider` 节点作为图的第一个节点，内部包含 5 个子模块。所有子模块都是确定性计算，不消耗 LLM token：

| 子模块 | 来源 | 职责 |
|--------|------|------|
| **WarehouseParser** | 现有 `JavaTagExtractor` | JavaParser AST 解析 + 全项目符号提取（类/方法/字段/调用边） |
| **CodeGraph** | 现有 `RepoMapBuilder` + PageRank | HashMap 结构索引（`classNameToPath`、`classMethods`、`interfaceImpls`、`reverseCallers`）+ 基于调用关系的 PageRank 排序 + token 预算截断 |
| **CodeRetriever** | 新（TF-IDF） | CamelCase 分词 + TF-IDF + L2 归一化，召回跨模块语义相关代码 |
| **StaticScanInvoker** | 新（轻量 AST visitor） | 不依赖 PMD/SpotBugs 等外部工具，仅用 JavaParser AST visitor 做确定性事实标注：空指针风险模式（返回值/Map.get() 后无判空）、资源未关闭（Stream/Connection 不在 try-with-resources）、SQL 拼接（execute* 参数含 `+`）、硬编码（password/key/token 字面量赋值） |
| **CoverageCalculator** | 新（确定性） | 计算依赖链完整度 = 已覆盖的依赖 / 应覆盖的依赖，产出 `state.coverage` 供 Supervisor 判断 |

ContextProvider 写三个 state 字段：`state.context`（结构索引 + 语义检索结果）、`state.static_facts`（静态事实标注）、`state.coverage`（覆盖完整度）。

**StaticScanInvoker 不作为独立 Agent 的原因**:它的输入（AST、文件内容）就是 ContextProvider 已持有的数据，拆成两个节点只是把同一份数据传两遍。共享输入 + 共享生命周期 + 都是确定性计算 → 天然属于同一节点的不同子步骤。

**WarehouseParser / CodeRetriever 不作为独立 Agent 的原因**:它们做的事（AST 解析、建索引、TF-IDF 检索、查表）全是确定性计算，不需要 LLM 的"思考-行动"循环。做成 Agent 白白消耗 token，零增量价值。

**子模块并行执行**:WarehouseParser 先行产出 AST → CodeGraph / CodeRetriever / StaticScanInvoker 三路并行（输入均为 AST，彼此独立）→ CoverageCalculator 汇总。节点内用 `ThreadPoolExecutor` 并行。

### 3. Supervisor 角色转型：从派发器到 State 协调者

三个审查员合并后，"选谁去审"的派发职能消失。Supervisor 转型为**信息充分性守门人 + State 唯一协调者**。

规则与 LLM 的分工：

| 判断 | 方式 | 说明 |
|------|------|------|
| 上下文覆盖率是否达标 | **规则** | 读 `state.coverage`，低于阈值自动触发补全，不调 LLM |
| 给审查员写聚焦指令 | **LLM** | 输入 diff + context 摘要 → 输出 `state.focus_notes`："重点关注 X 文件的 Y 方法，关注 Z 风险" |
| 审查结果是否充分 | **规则优先，LLM 兜底** | 规则先判：findings 为空但 diff 有实质变更？关键文件无 finding 覆盖？规则兜不住再调 LLM |
| 是否需要重审 | **LLM** | 判断 findings 质量是否足够，需要时带聚焦指令重派 CodeReviewer |
| 迭代终止 | **规则** | `iteration > max_rounds` 直接路由到 SelfChecker |
| 决策轨迹 | **规则** | 每步 action + reason + 规则/LLM 标记写入 `state.supervisor_log` |

**规则保证不翻车，LLM 保证不僵化。两者叠加而非互相替代。**

### 4. SelfChecker：取缔 aggregation + fp_filter，增加幻觉检测

SelfChecker 合并当前 aggregation 和 fp_filter 两个阶段的所有能力，并新增两项确定性校验：

| 步骤 | 方式 | 说明 |
|------|------|------|
| 引用真实性校验 | **规则（新增）** | 每个 finding 提到的类/方法是否存在于 `state.context` 的 HashMap 索引？不存在 → 幻觉，剔除或降置信度 |
| 静态事实冲突 | **规则（新增）** | finding 说"第 N 行未判空"，查 `state.static_facts` 发现第 N-1 行有判空 → 冲突，剔除 |
| 规则去重 | **规则（已有）** | 文件+行号+type 指纹精确去重 |
| 语义合并 | **LLM（已有）** | 不同措辞但指向同一底层问题 → 归组合并 |
| 规则硬过滤 | **规则（已有）** | YAML 规则剔除已知误报模式 |
| 逐条复核 | **LLM（已有）** | 对存活的 finding 逐条问"这是真问题吗" |

前四步（引用校验 / 事实冲突 / 规则去重 / 规则硬过滤）无依赖关系，`ThreadPoolExecutor` 并行执行。

### 5. 三个专属工具降级为上下文注入

`find_sensitive_apis` / `find_callers` / `get_code_metrics` 不再作为 Agent 工具暴露，而是降级为 ContextProvider 的产出：

- `find_sensitive_apis`（危险 API 清单匹配）→ StaticScanInvoker 的 SQL 拼接 / 硬编码标注
- `find_callers`（调用方追踪）→ CodeGraph 的 `reverseCallers` HashMap 查表
- `get_code_metrics`（CC/LOC/嵌套深度）→ 可选的静态事实标注（CC > 10 标记为高复杂度）

**理由**:这三个都是确定性计算，不需要 Agent 在 ReAct 循环中多轮调用。预计算后自动注入上下文，Agent 直接看到结果，节省 token 且不损失信息。

**Java 侧保留 `repomap/` 包和三个工具实现不删**——它们作为 ContextProvider 子模块的基础设施被复用，只是不再通过 `POST /api/v1/tools/{name}` 暴露给 Agent。

### 6. 最终图拓扑

```
START
  │
  ▼
┌─────────────────────────────────────────┐
│  ContextProvider（确定性节点，0 token）    │
│  WarehouseParser → 三路并行               │
│  (CodeGraph | CodeRetriever | StaticScan)│
│  → CoverageCalculator                    │
│  写：context / static_facts / coverage    │
└─────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────┐
│  Supervisor（混合：规则 + LLM）            │
│  规则：coverage / 终止 / 轨迹              │
│  LLM：聚焦指令 / 充分性 / 重审             │
│  读：context / coverage / findings        │
│  写：focus_notes / route / supervisor_log │
└─────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────┐
│  CodeReviewer（LLM Agent，ReAct）          │
│  输入：diff + context + static_facts      │
│        + focus_notes                     │
│  工具：get_file_content                   │
│  写：findings                             │
└─────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────┐
│  SelfChecker（混合：确定校验 + LLM 综合）   │
│  规则并行：引用校验 / 事实冲突 / 去重 / 过滤 │
│  LLM：语义合并 / 逐条复核                  │
│  读：findings / context / static_facts    │
│  写：final_issues / summary               │
└─────────────────────────────────────────┘
  │
  ▼
 END
```

**4 个图节点，5 个子模块（在 ContextProvider 内），6 个设计能力全覆盖。**

### 7. 并行策略

节点间因依赖链串行。并行下沉到节点内：

- **ContextProvider**:WarehouseParser 先行 → CodeGraph / CodeRetriever / StaticScanInvoker 三路并行（ThreadPoolExecutor）
- **SelfChecker**:引用校验 / 事实冲突 / 规则去重 / 规则硬过滤 四路并行（ThreadPoolExecutor）

预估 LLM 调用从 5-6 次降到 2-3 次，即使少了三审查员并行，总时延反而更短（LLM 调用耗时远大于确定性计算）。

**理由（综合）**:

1. **区分度来自方法论而非输出 mask**:让同一 LLM 从三条思考路径审视代码，比三个 LLM 各戴一个输出过滤器更自然。LLM 的推理能力用在"如何发现"上，而非"该不该报告"上。
2. **确定性计算不该消耗 token**:AST 解析、建索引、TF-IDF、静态标注——这些不需要 LLM。ContextProvider 一次性预计算、两个 Agent 节点（CodeReviewer + SelfChecker）复用同一份产出，效率远高于三个 Agent 各自在 ReAct 中调工具。
3. **幻觉检测是当前管线的盲区**:LLM 可能引用不存在的类/方法、可能无视已有的判空检查。HashMap 查表 + 静态事实对比是确定性校验，零 token、可单测、不可被 LLM 自我复核替代。
4. **Supervisor 不消失，只是转型**:即使只有一个审查员，仍需要一个协调者来判"信息够不够"、"要不要重审"、"该聚焦哪里"。规则做护栏、LLM 做判断的混合模式保证稳定 + 灵活。
5. **用户 6 Agent 设计全量保留，不砍能力**:只是执行方式区分——需要 LLM 推理的做 Agent，纯确定性计算的做 ContextProvider 子模块。

**放弃的备选**:

- **保留三审查员 + 加强工具**:在现有架构上微调 prompt + 增加工具。放弃原因：根因是分类轴选错了（issue type vs. review methodology），微调治标不治本。三个专属工具的确定性本质也决定了它们无法提供真正的区分度。
- **引入 PMD/SpotBugs 等外部静态工具**:作为独立 Agent 或独立节点。放弃原因：默认规则集误报率 30-50%，引入的噪音可能大于信号。先用轻量 AST visitor 做高精度标注，重工具留到 Phase 4+ 作为可选增强。
- **把所有 6 个都做成 Agent**:WarehouseParser / CodeRetriever / StaticScanInvoker 做成独立的 LLM Agent。放弃原因：纯确定性计算不需要"思考-行动"循环，做成 Agent 只会增加 token 消耗和延迟，零增量价值。
- **砍掉 Supervisor**:退化为固定路由节点（coverage < 阈值 → 补全 → reviewer → SelfChecker）。放弃原因：聚焦指令生成（LLM）和审查充分性判断（LLM 兜底）能实质性提升 CodeReviewer 的输出质量，值得保留。
- **ContextProvider 拆成多个独立图节点**:每个子模块一个节点。放弃原因：子模块共享输入（AST + 文件内容）和生命周期，拆开只是传相同数据多遍。LangGraph 的节点边界是同步屏障，拆分还会阻止子模块间的并行执行机会。

**日期**:2026-07-03

---

## ADR-032 · 证据驱动的多 Agent ReviewCouncil：确定性外层编排 + 多角色审查协作

**背景**:ADR-031 试图把三审查员合并为单一 CodeReviewer，并用 ContextProvider + SelfChecker 简化管线。这个方向解决了"三维度审查员重复探索、工具调用不稳定"的问题，但也削弱了本项目最值得展示的亮点：多 Agent 编排。

Codeguard 是求职展示项目，不只是要"能审代码"，也要体现系统设计能力。当前行业语境下，多 Agent 编排的价值不应体现在"把 security / logic / quality 切得更细"，而应体现在一套可解释、可恢复、可量化的协作协议：不同 Agent 以不同认知职责参与代码审查，围绕同一份事实上下文产出候选、补证据、提出质疑，最后由裁决节点统一输出。

因此 ADR-032 取代 ADR-031，目标不是回到旧的 LLM Supervisor 动态派发，而是设计一条更适合展示也更可控的新路径：

```
START
  ↓
Summary（可选）
  ↓
ContextProvider
  ↓
CouncilCoordinator（确定性编排器）
  ↓
ReviewCouncilSubgraph（多 Agent 协作审查）
  ↓
SelfChecker（去重 + 证据校验 + 误报压制 + 级别校准）
  ↓
END
```

**核心定位**:

> ADR-032 是 "multi-agent inside a deterministic review protocol"：多 Agent 是外层图中可见的审查子图，但调度逻辑不是黑盒 LLM Supervisor，而是确定性的 Coordinator + 结构化 State。

---

### 决策 1：保留多 Agent，但从"三维度并行审查员"升级为 ReviewCouncil

ADR-032 不再采用 ADR-031 的"单一 CodeReviewer"目标形态。新的审查核心是 `ReviewCouncilSubgraph`，它在外层 LangGraph 中是显式节点/子图，从展示视角能清楚看到多 Agent 协作；从工程视角又可以作为一个独立阶段接入现有管线。

ReviewCouncil 第一版只定义角色大纲，具体 prompt、工具细节、结构化输出 schema 后续另开 change 深入讨论。

初步角色划分：

| 角色 | 职责 | 非职责 | 输出 |
|------|------|--------|------|
| **ThreatModelAgent** | 从攻击者/滥用者视角发现安全风险，如输入可控性、敏感 API、权限边界、注入与数据泄露 | 不报普通空指针、纯复杂度或风格问题 | `CandidateIssue[]` |
| **BehaviorAgent** | 从运行行为视角发现正确性问题，如空值、异常路径、事务一致性、状态变化、调用链影响 | 不报安全推测，不报纯维护性问题 | `CandidateIssue[]` |
| **MaintainabilityAgent** | 从长期维护视角发现复杂度、重复、可测试性、局部设计退化 | 不报漏洞，不报运行时 bug，除非其主要影响是维护风险 | `CandidateIssue[]` |
| **EvidenceAgent** | 针对候选 issue 补充支持证据、反证或未知项 | 不主动发现新 issue，不做最终裁决 | `EvidenceNote[]` |
| **ChallengeAgent** | 对候选 issue 提出质疑：证据不足、重复、级别过高、已有保护逻辑 | 不主动发现新 issue，不替代 SelfChecker | `Challenge[]` |
| **SelfChecker** | 最终去重、证据一致性校验、误报压制、严重级别校准、转成最终 `Issue` | 不做开放式上下文探索 | `ReviewResult` |

这个划分的重点是**认知职责不同**，而不是简单把问题类型切得更细。前三个 Agent 是发现者，EvidenceAgent 是举证者，ChallengeAgent 是质疑者，SelfChecker 是裁决者。

---

### 决策 2：外层调度员不是 LLM Supervisor，而是确定性的 CouncilCoordinator

旧 Supervisor 的问题在于：它使用 LLM 做派发、补派、finish 判断，导致调度结果不稳定，也容易把"工程编排"和"语义判断"混在一起。

ADR-032 保留"调度员"概念，但职责改为 `CouncilCoordinator`：

| 职责 | 做法 |
|------|------|
| 启动发现者 Agent | 固定 fan-out ThreatModel / Behavior / Maintainability |
| 收集结果 | 通过 LangGraph reducer 汇总 `CandidateIssue[]` |
| 控制 Evidence/Challenge 是否运行 | 读结构化字段和硬规则，不读自然语言关键词 |
| 控制有限轮次 | `max_evidence_rounds`、预算上限、候选数量上限 |
| 维护 trace | 记录每轮运行、跳过原因、预算消耗、候选数量 |
| 路由到 SelfChecker | 达到终止条件后进入裁决节点 |

`CouncilCoordinator` 不负责：

- 判断某个 issue 是否真实
- 用 LLM 动态决定"今天派不派某个发现 Agent"
- 让 Agent 自由自然语言聊天
- 替代 SelfChecker 做最终裁决
- 直接调用 Java 判断"是不是问题"

它本质上是：

```
LangGraph edges + State reducer + deterministic routing rules
```

---

### 决策 3：Agent 之间通过共享黑板和结构化消息通信

多 Agent 不做自由对话，也不做"Agent A 直接私聊 Agent B"。所有通信通过图 State 中的共享黑板完成：

```
ReviewCouncilState
  context_bundle
  candidate_issues[]
  evidence_requests[]
  evidence_notes[]
  challenges[]
  council_trace[]
  evidence_round
```

发现者 Agent 输出 `CandidateIssue`，EvidenceAgent 读取候选与证据请求后输出 `EvidenceNote`，ChallengeAgent 读取候选与证据后输出 `Challenge`，SelfChecker 读取所有结构化数据后产出最终 `Issue`。

关键结构化字段（大纲，后续 change 细化）：

```
CandidateIssue
  id
  agent
  category
  file
  line
  severity_proposal
  claim
  evidence_ids
  evidence_status: sufficient | partial | missing
  needs_evidence: bool
  evidence_requests[]
  confidence

EvidenceRequest
  candidate_id
  kind: caller_chain | callee_impl | sensitive_api | metric | guard_condition | related_snippet
  target
  reason_code

EvidenceNote
  candidate_id
  supports[]
  contradicts[]
  unknowns[]
  evidence_ids[]

Challenge
  candidate_id
  verdict: keep | downgrade | merge | drop | needs_more_evidence
  reason_code[]
  evidence_request?
```

Coordinator 的路由只能读取这些枚举/布尔/计数字段，禁止基于自然语言关键词判断。

---

### 决策 4：EvidenceAgent / ChallengeAgent 使用确定性路由 + 有限证据轮次

固定调用一次 EvidenceAgent / ChallengeAgent 实现简单，但会浪费 token，也无法处理复杂跨文件问题需要二次补证的场景。完全交给 LLM Supervisor 动态决定则又回到旧问题。

ADR-032 采用折中方案：**确定性路由 + 有限证据轮次**。

第一版推荐规则：

```
if candidate_issues is empty:
    skip EvidenceAgent
    skip ChallengeAgent
    go SelfChecker

if any candidate.needs_evidence:
    run EvidenceAgent

run ChallengeAgent once if candidate_issues is not empty

if any challenge.verdict == "needs_more_evidence"
   and evidence_round < max_evidence_rounds:
    run EvidenceAgent again
    run ChallengeAgent again
else:
    go SelfChecker
```

默认配置：

| 配置 | 默认 | 说明 |
|------|------|------|
| `max_evidence_rounds` | `1` | 第一版只允许一轮补证，复杂循环后续再开 |
| `max_candidates_for_challenge` | 待定 | 候选过多时先做规则截断或分批 |
| `min_confidence_for_fast_path` | 待定 | 高置信且证据充分的候选可减少补证 |
| `max_evidence_requests_per_candidate` | 待定 | 防止单个候选拖垮预算 |

这样能覆盖三种情况：

- 无候选：跳过 Evidence/Challenge，节省成本
- 普通候选：跑一轮举证 + 质疑
- 复杂候选：允许一轮受限补证，不进入无限 ReAct

---

### 决策 5：ContextProvider 保留，但职责收敛为事实供给层

ADR-031 中 ContextProvider 的方向保留，但不在 ADR-032 第一版继续膨胀成过大的"全能预计算节点"。它的边界是：为 ReviewCouncil 提供共享事实基础，而不是做问题判断。

第一版 ContextProvider 外层职责：

- 从 diff 派生 changed files / changed symbols
- 复用 repo-map / tag extraction 产出符号摘要与相关候选文件
- 复用或封装现有 Java 工具产出事实：敏感 API、调用方、复杂度、必要代码片段
- 对事实排序、截断、打 provenance
- 生成 `ContextBundle`

不做：

- 不判断漏洞/bug/质量问题是否成立
- 不直接替代发现 Agent
- 不做无限 RAG 或长期记忆
- 不把 Java 工具变成 LLM 决策点

`get_repo_map` 的处理延续 ADR-031 的可复用结论：repo map 能力保留，作为 ContextProvider 内部事实来源；`get_repo_map` 不再作为开放式 Agent tool 暴露给发现者 Agent。

---

### 决策 6：SelfChecker 合并 aggregation / fp_filter 的目标能力，但实现可分阶段复用旧代码

ADR-032 的目标图中不再额外挂 `aggregation -> fp_filter` 两个阶段。最终图上只保留一个 `SelfChecker` 裁决节点：

```
ReviewCouncilSubgraph -> SelfChecker -> END
```

SelfChecker 目标职责：

| 职责 | 来源 |
|------|------|
| 规则去重 | 复用当前 aggregation 第一段 |
| 语义合并 | 复用当前 aggregation 第二段 |
| 确定性误报规则 | 复用当前 fp_filter 第一段 |
| 可选 LLM 复核 | 复用当前 fp_filter 第二段 |
| 引用真实性校验 | ADR-032 新增 |
| 事实冲突校验 | ADR-032 新增 |
| Challenge 处理 | ADR-032 新增 |
| 严重级别校准 | ADR-032 新增 |

实现时可以先包装旧 `AggregationStage` 与 `FalsePositiveFilterStage`，保持行为可比；后续再内部重构成统一 SelfChecker。外部拓扑不再暴露两个独立节点。

---

### 决策 7：持久化与交互（ADR-026/027）第一版先舍弃

ADR-032 第一版暂不把 checkpoint / HITL 作为必做范围。

原因：

1. 外层图拓扑发生较大变化，旧 ADR-027 的两个固定 interrupt 点（supervisor finish、reviewer hit limit）已经不适配。
2. ReviewCouncil 的内部结构化 State 还未稳定，过早把 candidate/evidence/challenge 全部纳入可恢复契约，会放大迁移成本。
3. 当前优先级是先把多 Agent 外层编排与通信协议跑通，让架构亮点成立。
4. HITL 可以后续重新设计为 `review_council_complete` 或 `before_self_check` interrupt，而不是复用旧的 `supervisor_finish`。

取舍：

- ADR-032 第一版允许不启用 checkpointer。
- 当前 `PipelineOrchestrator.run(thread_id=...)` 公共参数可以保留，但新图第一版不承诺完整恢复 ReviewCouncil 内部中间态。
- `CODEGUARD_ENABLE_HITL` 在 ADR-032 默认路径下可暂时无效；旧 supervisor/HITL 实现迁移到 `services/agent/legacy/supervisor_graph/`，仅作历史参考，不作为运行回退。
- 后续另开 ADR-033/034 专门设计"ReviewCouncil checkpoint + HITL"。

---

### 外层编排如何从当前实现迁移

当前实现（简化）：

```
summary
  ↓
supervisor
  ↓
security / logic / quality reviewer subgraphs
  ↓
supervisor loop
  ↓
aggregation
  ↓
fp_filter
```

ADR-032 目标外层：

```
summary（可选）
  ↓
context_provider
  ↓
council_coordinator
  ↓
review_council_subgraph
  ↓
self_checker
```

迁移原则：

1. **保留 LangGraph StateGraph 作为外层载体**：继续使用当前 `pipeline/graph.py` 的图构建方式和 reducer 思路。
2. **废弃智能 Supervisor 默认路径**：旧 supervisor-scheduling 迁移到 `services/agent/legacy/supervisor_graph/`，仅作历史参考；新路径不再做 LLM 派发/finish，主编排代码不保留旧图运行分支。
3. **新增 ContextProvider 节点**：先生成 `context_bundle`，写入图 State，供后续所有 Agent 只读使用。
4. **新增 ReviewCouncilSubgraph**：作为外层图的一个子图或一组显式节点，内部包含发现者并行、证据、质疑的确定性流转。
5. **新增 CouncilCoordinator 逻辑**：可以是独立节点，也可以是 conditional edges + reducer + helper function；第一版更推荐用显式 helper，避免再引入一个 LLM Agent。
6. **SelfChecker 替代 aggregation/fp_filter 外层节点**：第一版内部复用旧 stage，外层只暴露 `self_checker`。
7. **输出契约不变**：最终仍返回 `ReviewResult`，不向 `Issue` 增加 candidate/evidence/challenge 字段；这些中间态只进 trace/eval。

推荐第一版节点：

```
START
  ↓
summary_node?             # 可继续复用
  ↓
context_provider_node     # 新增
  ↓
discover_fanout           # Threat / Behavior / Maintainability 并行
  ↓
council_route_node        # 规则判断是否需要 Evidence/Challenge
  ↓
evidence_node?            # 条件执行
  ↓
challenge_node?           # 条件执行
  ↓
self_checker_node         # 复用 aggregation + fp_filter + 新校验
  ↓
END
```

其中 `discover_fanout`、`evidence_node`、`challenge_node` 可以先直接做成外层节点，等逻辑稳定后再收敛为 `ReviewCouncilSubgraph`。

---

### 工具设计原则（大纲，后续另开任务细化）

工具不按"所有 Agent 都能用全部工具"分配，而按职责分配：

| 工具层 | 说明 |
|--------|------|
| 公共只读上下文工具 | `lookup_context_fact`、`get_context_snippet`、`cite_evidence`，只能查询 ContextBundle |
| 角色专属事实工具 | ThreatModelAgent 偏 `find_sensitive_apis`，BehaviorAgent 偏 `find_callers`，MaintainabilityAgent 偏 `get_code_metrics` |
| EvidenceAgent 补证工具 | 受限 `get_file_content`、related symbol lookup、caller/callee lookup，但只能响应结构化 `EvidenceRequest` |
| 禁止工具 | 开放式 `get_repo_map`、任意文件读取、无边界 repo 探索 |

具体工具 schema、allowlist、budget、失败策略后续另开 change 讨论。

---

### 后续优化策略（ADR-032 后续演进记录）

ADR-032 已完成的是"外层拓扑 + 结构化通信 + 默认路径切换"。下一步优化不应急着堆更多节点，而应围绕
ReviewCouncil 的专业分工、工具边界、调度策略和评测可观测性逐步精修。其中 ReviewCouncil 发现者职责、
状态通信与外层轮次策略已于 2026-07-04 先行敲定，并已通过 `refine-review-council-agents` 落地第一版实现；
其余方向继续作为候选策略记录，后续逐项讨论后再决定是否开新的 ADR / OpenSpec change。

#### 已敲定：ReviewCouncil 内部发现者职责与状态边界

核心原则：**内层可以 ReAct，外层不要群聊**。

- 发现者 Agent 内部可以使用 ReAct 循环，基于本职工具完成初步探索与验证，一次性返回多个 `CandidateIssue`。
- 外层 ReviewCouncil 不把补到的证据再返给原发现者重新审查，不做发现者之间的自由多轮对话。
- 外层通过结构化黑板完成 `Discover -> Evidence -> Challenge -> Decide`：发现者提出候选，EvidenceAgent 补事实，
  ChallengeAgent 质疑或降级，SelfChecker 最终聚合、去重、过滤并输出 `ReviewResult`。

发现者命名从"问题类型分类"升级为"审查方法论分工"，但对外输出仍兼容现有 category：

| 发现者 Agent | 对应 category | 核心问题 | 第一版工具边界 |
|--------------|---------------|----------|----------------|
| `ThreatModelAgent` | `security` | 是否引入真实可利用的攻击路径、信任边界破坏或敏感 sink 风险 | `get_file_content`、`find_sensitive_apis` |
| `BehaviorAgent` | `logic` | 是否破坏运行行为、触发路径、状态流转、异常处理或业务不变量 | `get_file_content`、`find_callers` |
| `MaintainabilityAgent` | `quality` | 是否制造值得 Code Review 阶段指出的维护风险 | `get_file_content`、`get_code_metrics` |

三个发现者都不是最终裁判，只输出候选主张：

```text
CandidateIssue:
  id
  source_agent: threat_model | behavior | maintainability
  category: security | logic | quality
  file
  line
  claim
  severity_proposal
  confidence
  evidence_status: sufficient | partial | missing
  needs_evidence: bool
  evidence_requests: EvidenceRequest[]
  evidence_notes: EvidenceNote[]
  challenge: Challenge | None
```

`EvidenceRequest.kind` 不追求覆盖所有情况，只作为 EvidenceAgent 的路由 hint。开放语义由 `question`、`reason`、
`target` 承载，无法稳定归类的请求统一使用 `open_question`：

```text
EvidenceRequest:
  kind: related_snippet | caller_path | sensitive_sink | metric_context | open_question
  target
  question
  reason
  preferred_tools
```

第一版建议的数量与成本上限：

```text
max_candidates_per_agent = 5
max_evidence_requests_per_candidate = 2
max_total_evidence_requests = 20
max_evidence_rounds = 1
```

停止条件保持确定性：

- 没有候选需要更多证据。
- Challenge 已给出 `keep` / `downgrade` / `drop` 等终态 verdict。
- 达到 `max_evidence_rounds`。
- 工具不支持或查不到证据时，记录 `unsupported` / `not_found` 的 `EvidenceNote`，不继续追问。

本次敲定范围不包括：发现者 prompt 的最终方法论、EvidenceAgent 的智能搜索策略、ChallengeAgent 的完整反方 prompt、
SelfChecker 的真正法官化实现、发现者之间的多轮讨论、持久化与 HITL。

已完成的优化项：

1. **细化 ReviewCouncil 内部 Agent 职责**
   已把现有 security / logic / quality reviewer 迁移为 ThreatModelAgent、BehaviorAgent、MaintainabilityAgent，
   并把职责、非职责、输出约束写入 prompt 与结构化 schema，避免只是把原来的三个维度切得更碎。

2. **定义每个 Agent 的工具边界**
   第一版 allowlist 已按角色敲定：ThreatModelAgent 偏 `find_sensitive_apis`，BehaviorAgent 偏 `find_callers`，
   MaintainabilityAgent 偏 `get_code_metrics`，三者共享 `get_file_content`，并已通过 `tool_allowlist` 生效。

3. **发现者 prompt 知识密度大幅增强**（2026-07-05，详见 ADR-033）
   三个发现者 prompt 从各 ~25 行扩充到各 ~220 行，每个 prompt 携带该领域的完整知识图谱
   （漏洞分类体系、Java 常见形态、误报判例、判定要点），三个 prompt 合计 ~690 行——
   这就是拆分为三个 Agent 的架构理由：任何一个 prompt 已经大到无法与其他两个合并而不稀释上下文。

4. **去重策略从"行号主键"升级为"根因主键"**（2026-07-05，详见 ADR-033）
   新增 `_extract_identifier_tokens` + `_share_key_identifier`：从 claim 文本提取 Java 标识符
   （方法名/变量名）作为根因锚点。同文件+共享关键标识符 → 同一根因合并。行号降级为 ±3 兜底弱信号。

5. **EvidenceAgent 证据路由（P1，详见 Change 1）**
   `_evidence_agent_node` 按 `preferred_tools` 路由到 4 个 Java 工具 + 去重 + ContextBundle 兜底；
   删 `EvidenceKind`（10 值死代码）。

6. **CouncilJudge 闭环裁决（P1，详见 Change 2）**
   合并 challenge_agent + self_checker 为 `_council_judge_node`（7 规则 + 两段式去重 + LLM 终审）；
   支持 `needs_more_evidence` 循环补证；裁决模型异源千问（temperature=0）。

剩余优化项：

7. **补全去重覆盖：LLM 语义综合未命中的跨行号同根因**
   EvidenceAgent 只响应结构化 EvidenceRequest 补事实，不能主动新增 issue。下一步应把 `related_snippet`、
   `caller_path`、`sensitive_sink`、`metric_context`、`open_question` 稳定映射到现有工具、证据状态与 trace，
   同时补齐 budget、失败策略、禁止行为与工具调用 trace。ChallengeAgent 只提出质疑，不能替代 SelfChecker 做最终裁决。

4. **增强 CouncilCoordinator 的确定性调度策略**
   当前第一版以 `needs_evidence`、challenge verdict、轮次上限等结构化字段路由，方向正确。后续可继续细化：
   高危候选是否必须经过 Challenge；低置信候选是补证、降级还是丢弃；候选过多时如何排序和截断；
   EvidenceAgent 是否允许多轮补证；哪些情况可以 fast path 直接进入 SelfChecker。调度条件仍应读取枚举、
   布尔和计数字段，禁止回到自然语言关键词判断。

5. **把 SelfChecker 从包装旧阶段升级为真正裁决节点**
   第一版 SelfChecker 内部包装 AggregationStage 与 FalsePositiveFilterStage，保持行为可比。后续应逐步承担
   ReviewCouncil 语境下的统一裁决：合并重复 CandidateIssue、根据 EvidenceNote 调整置信度、根据 Challenge
   降级或剔除、校准 severity、保证最终 Issue 有可靠定位与建议。外部输出契约仍保持 `ReviewResult` / `Issue`
   不变，中间态只进入 trace / eval。

6. **补齐 ReviewCouncil 的过程可观测性与评测指标**
   为了让"多 Agent 编排"不只是口号，后续 eval/report 可以增加过程指标：每次审查触发了哪些 Agent；
   EvidenceAgent / ChallengeAgent 调用次数；Challenge 推翻、降级、合并了多少候选；有证据支持的问题占比；
   SelfChecker 丢弃候选的原因分布；多 Agent 对同一候选的冲突率。这些指标能直接服务求职展示，也能指导后续调参。

7. **增强 ContextProvider，但守住事实层边界**
   ContextProvider 后续可以接入更好的 changed symbol 摘要、repo-map、caller/callee、敏感 API、复杂度和上下文片段
   排序，并为不同 Agent 提供不同的 context slice。但它仍只产出事实、来源和截断信息，不判断"是不是问题"，
   不替代发现者 Agent，也不把 Java Gateway 变成 LLM 决策点。

8. **持久化与交互继续后置**
   ADR-027 的简约持久化 + HITL 暂不接回 ADR-032 第一版。更合适的时机是在 Candidate / Evidence / Challenge
   状态契约稳定后，重新设计 `review_council_complete` 或 `before_self_check` 这类 interrupt 点，再讨论 checkpoint
   与人工确认。现在过早迁移会稀释 ReviewCouncil 编排主线。

优先级建议：

| 优先级 | 方向 | 原因 |
|--------|------|------|
| Done | ReviewCouncil 内部 Agent 职责与发现者工具边界 | 已通过 `refine-review-council-agents` 落地第一版 |
| Done | EvidenceAgent 证据路由（Change 1） | `preferred_tools` 路由 4 个 Java 工具，删 EvidenceKind |
| Done | CouncilJudge 闭环裁决（Change 2） | 7 规则 + 两段式去重 + LLM 终审 + needs_more_evidence 循环 |
| Done | 发现者 prompt 知识密度（ADR-033） | 三个 prompt 各 ~220 行，合计 ~690 行领域知识 |
| Done | 去重从行号主键升级为根因主键（ADR-033） | 方法名匹配优先，行号 ±3 兜底 |
| P2 | SelfChecker 裁决语义 | 直接影响误报、重复和最终输出质量 |
| P2 | 过程 trace / eval 指标 | 让架构效果可展示、可量化 |
| P3 | ContextProvider 深化 | 有价值，但要等 Agent 需要什么上下文更清楚后再加 |
| P3 | checkpoint / HITL | 依赖中间态契约稳定，暂不抢跑 |

---

### 放弃的备选

1. **继续 ADR-031 单一 CodeReviewer**：工程更简单，但多 Agent 亮点不足，不适合作为求职项目的核心展示。
2. **恢复旧 LLM Supervisor 调度**：多 Agent 可见度强，但调度不稳定，且已在 ADR-029 暴露"两个 LLM 串行猜测"问题。
3. **所有 Agent 自由 ReAct + 任意工具**：看起来更 Agentic，但 token 成本、可复现性、误报控制都会变差。
4. **Evidence/Challenge 固定每次都跑**：简单但浪费成本；遇到复杂候选也缺少二次补证能力。
5. **现在就把 HITL/checkpoint 一并迁移**：范围过大，且 interrupt 点需要基于新 State 重新设计，先舍弃。

### 补充决策：状态单一权威来源与举证账本收敛（2026-07-09）

完整 Trace 暴露出 ADR-032 状态中存在摘要、文件、证据的重复副本，以及
旧 Supervisor / 文件软分派遗留字段。决定采用以下约束：

1. `ReviewState.diff_summary` 是唯一摘要；`ContextBundle` 只保存
   `changed_files` 与工具/AST 新增事实，不再复制摘要、来源列表或截断总标记。
2. `CandidateIssue`、`EvidenceRequest`、`EvidenceNote` 分别表示候选、
   补证命令与证据记录，通过 candidate/request ID 关联，不互相内嵌副本。
3. CouncilJudge 的规则层和 LLM Prompt 读取同一份顶层 `EvidenceNote` 账本；
   证据的支持、反驳、不足必须来自这个账本。
4. Annotated reducer 只接收节点增量；`EvidenceRequest` 按稳定 ID 去重，
   EvidenceAgent 不重复处理已有 `EvidenceNote.request_id` 的请求。
5. Summary Prompt schema 与运行 schema 保持一致，只生成实际消费的 `summary`。
6. 当前发现者始终接收完整 diff；删除 `file_groups`、`focus_notes`、
   `Reviewer.category` 等废弃软路由接口。
7. 初始 `ReviewState` 只写入真实外部输入与配置项，不预填空运行输出；
   删除无消费方的 `judge_pass`。

该调整不改变 `ReviewResult` / `Issue`、Java Gateway 协议或 ReviewCouncil 拓扑，
但修复了终审 LLM 无法稳定看到 EvidenceAgent 证据的问题，并让 Trace 中的状态流转更接近真实接口。

**验证记录（2026-07-09）**：

- `conda run -n codeguard --no-capture-output python -m pytest tests/ -q`
  → `246 passed`
- `conda run -n codeguard --no-capture-output ruff check src/ tests/`
  → `All checks passed!`
- `conda run -n codeguard --no-capture-output mypy src/`
  → `Success: no issues found in 30 source files`
- mypy 明确排除 `src/codeguard_agent/legacy/`，符合 legacy 为废弃代码、不整理的边界。
- 新生成 Trace：`services/agent/trace/trace-20260709-235137-d2efde97.html`，
  共 40 个事件；自动检查确认不含 `"file_groups"`、`"focus_notes"`、
  `"judge_pass"`、`"evidence_status"`、`"needs_evidence"`。

**日期**:2026-07-03

---

## ADR-033 · 发现者 prompt 知识密度重构 + 去重从行号主键升级为根因主键

**背景**:ADR-032 落地后两轮实跑暴露两个关联问题。

**问题 1 — 三个 Agent 没有各司其职**:用 springboot-review-demo（4 个植入缺陷）测试发现，三个 Agent 的摘要显示各自都发现了全部 4 个问题（off-by-one、NPE、路径穿越、资源泄漏）。不是"三个专家各看一面"，而是"三个 LLM 看同一份 diff 得出几乎相同的结论"——分类轴选对了（安全/行为/维护），但 prompt 太薄（各 ~25 行），LLM 缺乏领域知识深度，只能凭训练数据的"常识"审查，自然产出高度重叠。

**问题 2 — NPE 重复报且去重漏了**:同一个 `getUserDisplayName` 方法里的同一个 NPE，BehaviorAgent 报在第 76 行（`.getEmail()` 调用点），MaintainabilityAgent 报在第 71 行（`findById` 返回值）。行号差 5 行，现有 ±3 容差没兜住；LLM 语义综合（千问 `_MergePlan`）也没识别为同源。根因是去重的主键 `(file, line, type)` 把 LLM 天然不精确的行号当作精确主键——行号是 LLM 从 diff hunk header 推算的，不同 Agent 描述同一根因时自然会锚定到不同行（一个锚在"null 从哪里来"，一个锚在"null 在哪里炸"），这是结构性必然产物，不是 bug。

### 决策 1：prompt 从"职责声明"升级为"领域知识图谱"

三个 prompt 从 ~25 行扩充到各 ~220 行，每个携带该领域的完整知识：

| prompt | 行数 | 新增的领域知识 |
|--------|------|---------------|
| `threat-model.txt` | 27→~220 | 8 大类安全漏洞知识图谱（注入/认证授权/敏感数据/加密/跨站/反序列化/文件操作/配置安全），每类含 Java 常见形态、危险模式 vs 安全模式、判定要点；分析方法论（找攻击入口→追数据流与防护→验可利用性）；严重级别 rubric（锚定开发者动作）；已知误报判例 10 条；强制排除项 10 条 |
| `behavior.txt` | 27→~240 | 8 大类运行时缺陷知识图谱（空值安全/边界与范围/异常处理/资源管理/并发与线程安全/状态与契约/调用链影响/常见逻辑错误），每类含 Java 常见形态、判定要点；分析方法论（找入口与触发条件→追执行路径→验真实可触发）；误报判例 9 条；强制排除项 |
| `maintainability.txt` | 28→~230 | 8 大类可维护性知识图谱（错误处理质量/硬编码与魔法值/复杂度与规模/重复与抽象/耦合与内聚/可测试性/设计退化信号/命名与文档质量），每类含判定要点；分析方法论（看业务目的→评维护成本→验值得改）；误报判例 9 条；强制排除项 |

**三个 prompt 合计 ~690 行。这就是拆分为三个 Agent 的架构理由——不是为了制造"不同视角"（ADM-031 的教训：同一个 LLM 看同一份 diff，视角差异天然有限），而是为了分摊上下文压力：任何一个 prompt 已经大到无法与其他两个合并而不稀释知识密度。每个 Agent 带着 200+ 行的领域知识去审查，相当于带了该领域的 checklist + 判例手册。**

设计原则（与用户对齐）：
- 三个 Agent 并行的目的不是"各管一摊不重叠"，而是**让每个 Agent 带着自己领域的深厚知识去审视同一份代码**
- 重叠不叫重复，叫"不同角度验证同一个问题"——去重是后端的事，漏报才是不可接受的
- 拆分是工程需要（上下文窗口装不下 690 行 + diff + 工具输出），不是语义需要

### 决策 2：去重主键从 `(file, line, type)` 升级为"根因标识符"

**问题诊断**:行号是 LLM 从 diff hunk header 推算的，天然不精确。把它当作去重主键，等于用一把不准的尺子量东西。之前试图用 ±10 容差补救，但用户指出"万一 10 行内真有不同问题怎么办"——增大容差是治标膏药，且引入误合并风险。

**新策略**:行号从"主键"降级为"兜底弱信号"，新增更可靠的"根因标识符"作为主键。

新增两个纯函数（`graph.py`，确定性、可单测）：

- **`_extract_identifier_tokens(claim)`**:从 claim 文本提取 Java 标识符——camelCase（`findById`、`getUserDisplayName`）、反引号包裹的代码字体标识符、过滤掉停用词和过短词（<4 字符）
- **`_share_key_identifier(tokens_a, tokens_b)`**:两组 token 是否共享至少一个关键标识符（长度 ≥ 5 且非泛词）

在两个位置应用：

1. **`_candidate_dedup_reducer`（fan-in 层）**:同文件 +（共享关键标识符 → 优先合并 或 ±3 行 → 兜底合并）
2. **`_council_judge` 安全网**:同上逻辑

示例（NPE 重复案例）：
```
#2 claim: "...getUserDisplayName...findById...getEmail..."
#4 claim: "...getUserDisplayName...findById...getEmail..."
→ _extract_identifier_tokens 分别提取 {findById, getUserDisplayName, getEmail, ...}
→ _share_key_identifier 发现共享 findById（长度 7 ≥ 5）→ 合并 ✓
```

反例（同文件不同问题）：
```
NPE claim: "...findById...getUserDisplayName..."
资源泄漏 claim: "...FileInputStream...mergeFiles...try-with-resources..."
→ 无共享关键标识符 → 不合并 ✓
```

**当前 5 层去重体系**（本 ADR 落定后的最终态）：

| 层 | 位置 | 机制 | 判据 |
|---|---|---|---|
| 1a | fan-in reducer | 规则指纹 | `(file, line, type)` 精确匹配 |
| 1b | fan-in reducer | **根因标识符（新增）** | 同文件 + 共享关键标识符 |
| 1c | fan-in reducer | 邻行兜底 | 同文件 + ±3 行 |
| 2 | council_judge 阶段 1 | 6 条确定性规则 | 淘汰/降级（invalid_file/contradicted/no_evidence/quality_no_metrics/guard_detected/critical_partial） |
| 3a | council_judge 阶段 2 | 规则去重 | `deduplicate()` 指纹 |
| 3b | council_judge 阶段 2 | LLM 语义综合 | 千问 `_MergePlan` 分组（temperature=0） |
| 4 | council_judge 安全网 | **根因标识符 + 邻行兜底（新增）** | 同文件 +（共享标识符 或 ±3 行） |
| 5 | council_judge 阶段 3 | LLM 终审 | 千问 `JudgeDecisions`（keep/drop/downgrade/merge） |

### 放弃的备选

- **±10 容差**:用户指出会误合并 10 行内不同问题，已回退到 ±3。根因修复是方法名匹配而非盲目扩容器差。
- **行号标准化（让 LLM 输出更准的行号）**:不可靠，LLM 的行号推算本质上是非确定性的，不同 Agent 锚定到不同行是结构性必然。
- **去掉行号、纯靠 LLM 去重**:LLM 语义综合（层 3b）已存在但不够稳定（NPE 案例就没兜住），需要确定性规则兜底。
- **合并三个 Agent 为单一审查员**:用户明确反对，三个 Agent 的目的是分摊上下文压力（690 行 prompt + diff + 工具输出），不是制造视角差异。

**日期**:2026-07-05

---

## ADR-034 · 智能举证与证据驱动裁决:evidence_agent LLM 分析 + council_judge 规则精简

**背景**:ADR-032 实现了 evidence_agent → council_judge 的举证-裁决流水线，但 evidence_agent 是纯确定性 HTTP 转发节点（调工具 → raw output[:200] → EvidenceNote.supports），EvidenceNote.contradicts 从未被写入（死字段），council_judge 的规则层和 LLM 终审拿到的是未经分析的原始工具输出片段。整个证据管道在架构上存在但在语义上是空的。

**决策**:

### 1. evidence_agent 升级为智能举证

evidence_agent 从"确定性 HTTP 转发"改为"调工具 + LLM 证据分析"两阶段：

- **阶段 1**：调 Java 工具获取原始事实（不变，去重调用）
- **阶段 2**：对每条工具输出调轻量 LLM（`EvidenceJudgment` Pydantic 结构化输出）分析证据含义，产出 SUPPORTS / CONTRADICTS / INSUFFICIENT 判定 + 推理依据

LLM 分析 prompt（`prompts/evidence-analysis.txt`）约 30 行，包含判定标准和纪律。LLM 不可用时自动回退 raw output[:200] 模式（保持向后兼容）。

evidence_agent 复用 `fp_verify_llm`（异源千问 temperature=0），与 council_judge 的语义综合和终审共用。每个 EvidenceRequest 增加一次轻量 LLM 调用，实际多数审查只有 0-3 个低置信候选需要补证。

### 2. EvidenceNote 模型更新

- 新增 `reasoning: str` 字段承载 LLM 证据分析的整体推理
- `status` 根据 supports/contradicts 分布自动计算：有 supports 无 contradicts → "supported"；有 contradicts 无 supports → "contradicted"；两者都有 → "mixed"；都没有 → "insufficient"
- `EvidenceNoteStatus` Literal 新增 "contradicted" 和 "insufficient"，保留旧值 "not_found" 和 "unsupported" 向后兼容

### 3. council_judge 规则从 6 条精简为 4 条

| 删除的规则 | 原因 |
|-----------|------|
| `_rule_quality_no_metrics` | LLM 证据分析比"调没调 get_code_metrics"更准 |
| `_rule_guard_detected` | evidence_agent 的 LLM 显式判断"是否有保护逻辑"，不再需要关键词匹配 |
| `_rule_critical_partial` | 并入 `_rule_no_evidence`（CRITICAL + 全部 insufficient → downgrade） |

| 新增/重写的规则 | 判据 |
|----------------|------|
| `_rule_contradicted`（重写） | 检查 `EvidenceNote.status`（"contradicted" 或 "mixed"）替代死字段 `n.contradicts` |
| `_rule_no_evidence`（重写） | 检查全部 "insufficient" 替代 "not_found"；CRITICAL + 全 insufficient → downgrade |
| `_rule_strong_support`（新增） | confidence ≥ 0.9 + 全 "supported" + 零 contradicts → fast-track keep（跳过 LLM 终审） |

### 4. council_judge LLM 终审 prompt 证据驱动化

终审 prompt 从 raw output 片段改为结构化证据摘要：

```
✅ 支持: [find_callers] 确认 getEmail() 调用方未判空
❌ 反驳: (无)
⚠️  不足: (无)
```

prompt 中加入证据驱动判定原则：有支持无反证 → keep；有反证 → drop 或 downgrade；全部 insufficient → 保守决策；共享证据链 → merge。

### 效果

- **contradicts 字段激活**：evidence_agent LLM 分析显式输出 CONTRADICTS 判定，`_rule_contradicted` 现在可真正触发
- **裁决质量提升**：council_judge 基于已分析的证据质量做决策，而非从 raw output 里猜含义
- **面试可讲**："举证 Agent 分析证据含义 → 裁决 Agent 基于证据质量判 keep/drop/downgrade/merge，形成可追溯的决策链"
- **成本可控**：实际多数审查只有 0-3 个低置信候选需要补证，每次证据分析 prompt 很短（~500-1000 tokens）

**日期**:2026-07-05

---

## ADR-035 · GitHub PR 自动审查 CI 集成

**背景**:阶段 4 审查核心稳定后,需要把 Codeguard 从命令行工具升级为 CI 门禁——开发者提 PR 自动触发审查、结果回写到 GitHub Check Runs 和行级评论。

**决策**:

### 1. CI 入口放 Java Gateway,不另起服务

webhook 端点 (`POST /webhooks/github`) 注册在现有 Javalin 上,和 `/tools/*` 共用同一个进程。理由:
- Java Gateway 已是常驻 HTTP 服务,加一个端点零成本
- 异步 job、并发控制、限流是 Java 强项
- 不破坏"Java = 基础设施,Python = 智能"的职责边界
- 部署不变:仍是 docker-compose 单容器(双运行时)

### 2. 审查执行走 ProcessBuilder + 异步 IO

Gateway 通过 `ProcessBuilder` 启动 Python CLI 子进程而非给 Agent 加 HTTP 包装:
- Python Agent 零修改(除加 `--format json` CLI 参数)
- 进程隔离天然可靠
- 子进程 stdout 用新线程异步读取,避免 `readAllBytes()` 在 `waitFor()` 之前永久阻塞

### 3. H2 持久化 job 队列,不上 Redis/MQ

单实例部署下,H2 文件模式 + 启动扫表恢复 = 持久化队列。BlockingQueue 只是调度器,H2 是数据源。多实例水平扩展时换 MySQL + Redis。

### 4. GitHub App 认证(JWT),不用 PAT

Check Runs API 要求 GitHub App 安装令牌。JWT 每 10 分钟轮换,私钥从文件读取,不硬编码。

### 5. 限流:Guava RateLimiter 令牌桶

`CODEGUARD_WEBHOOK_RATE_LIMIT` 单位是**每秒许可数**,默认 0.5(平滑突发模式)。早期 Bug:错误地用了"每小时次数除以 3600 → 0.0083/秒 → tryAcquire(100ms) 永远拿不到许可"。

### 6. 反馈失败不影响审查结果

`ResultFeedback` 抛异常时,`ReviewExecutorImpl` 用独立 try-catch 包裹,不让 Check Run API 400 拖累审查结果重试。审查本身成功 → job DONE 落 H2,反馈失败只打 ERROR 日志。

### 联调中发现的 Bug

| Bug | 根因 | 修复 |
|-----|------|------|
| 审查永远空结果 | WebhookPayload 用 head.ref 当 baseRef,clone master 后 diff master | headRef/baseRef 拆分,clone 后 fetch base + checkout PR commit |
| 全部 webhook 429 | Guava RateLimiter 参数语义错误:0.0083/秒 + 100ms 超时 | 改为每秒许可数语义,默认 0.5 |
| 审查"卡死" | ProcessBuilder stdout 用 readAllBytes() 同步读,进程不退出就永远阻塞 | 新线程异步读 + waitFor(10min) 超时 |
| Check Run 400 | PATCH 请求带了 name 字段(GitHub API 只在 POST 允许) | 去掉 name,补 completed_at |
| origin/master 不存在 | shallow clone 只拉默认分支,PR 的 base 分支未 fetch | 显式 fetch base ref |
| SSH 隧道频繁断开 | TCP 空闲超时,NAT 丢连接 | ServerAliveInterval=60 心跳保活 |

### 知识注入问题(待决)

当前 agent prompt 含 ~690 行领域知识,但仍存在漏报(如 ConcurrentHashMap 原子性、异常吞没)。用户指出 RAG/向量检索不适用于"带着知识去审"这个范式——Agent 认不出问题就不会去搜。

讨论的解决方向:在 discover agent 之前加一个**确定性知识注入器**(Java Gateway,正则/模式匹配),根据 diff 中出现的 API/模式,注入对应的审查知识卡片到 agent prompt。不调 LLM,不依赖向量检索。这个问题留待后续设计。

**日期**:2026-07-06

---

## ADR-036: ContextProvider AST 富化（Layer 1）

**日期**: 2026-07-07
**状态**: 已实现

### 背景

三个发现者 Agent 各自通过 `get_file_content` 理解 diff 文件的代码结构，存在重复劳动和 token 浪费。Diffguard 的 `ASTEnricher` 模式证明：在审查前将 diff 文件的 AST 注入共享上下文，可以减少冗余工具调用。

### 决策

- **Layer 1（本次）**: `context_provider` 新增 `get_diff_ast` 调用，对每个 diff 内的 Java 文件提取完整 AST（类结构/方法签名+可见性+注解/控制流/调用边），以 `ContextFact(kind="ast_structure")` 存入 `ContextBundle`。预算按文件独立：`max(20% × diff_tokens × 4, 600 chars)`，超预算两级裁剪（Tier 1: 仅 diff 行范围内方法；Tier 2: 极简模式仅类名+签名）。
- **独立 AST 体系**: 不复用 repomap 的 `Tag` 模型——新建 `com.codeguard.agent.ast` 包，用 JavaParser 直接解析，产出 `DiffASTResult`（含 `ClassDef`/`MethodDef`/`CFNode`/`CallEdgeDef`）。模型粒度对齐 Diffguard 的 `ASTAnalysisResult`。
- **Layer 2（后续）**: 跨文件探索工具（`get_method_definition`、`find_callers` 扩展 direction+depth）留待独立 change。

### 放弃的方案

- 复用 repomap `JavaTagExtractor`/`Tag`: Tag 模型只有 `(name, kind, line, signature)`，无法承载可见性/注解/控制流。
- 全局 ContextBundle 预算共享: 改为文件独立预算，避免大 diff 的 AST 被非 AST 事实挤占。
- 注入 diff 文本内部: 改为注入 ContextBundle，使所有 Agent 共享统一的结构化视图。

### 影响

- `repomap/` + `GetRepoMapTool` 迁移到 `services/gateway/legacy/`，不再参与编译。
- 新增 Java 4 文件（`DiffASTResult`/`DiffASTAnalyzer`/`ASTContextFormatter`/`GetDiffASTTool`），Python 2 文件改动（`tool_client.py` + `context_provider.py`）。
- 后续参考: ADR-032 ReviewCouncil 编排，ADR-020 repo map 下线决策。

---

## ADR-037: CI PR 行级评论行号映射

**日期**：2026-07-07
**状态**：已实现

### 背景

LLM 审查输出的 `line` 是源文件绝对行号，GitHub PR review comment API 要求行号落在 diff hunk 的上下文行内。不匹配时返回 HTTP 422。之前只做了降级兜底（失败的 issue 汇总为 PR 普通评论），丢失了精确的行级定位。

### 决策

- **持久化 diff 文本**: `ReviewJob` 新增 `diffText` 字段，H2 `CLOB` 列。`ReviewExecutorImpl` 在 clone 后采集 `git diff base...HEAD`。
- **严格映射**: `ResultFeedback.mapToDiffLine()` 解析 unified diff hunk header，在 hunk 内逐行推进 new-side 行号计数。匹配返回 diff 行号，未匹配返回 -1 走降级路径。
- **降级保留**: 行号无法映射的 issue 仍以 PR 普通评论形式出现，信息不丢。

### 放弃的方案

- 邻近兜底（行号偏几行时用最近的 diff 行代替）
- diff 压缩存储
- 不持久化直接传参

### 影响

- Java 4 文件改动（ReviewJob / JobRepository / ReviewExecutorImpl / ResultFeedback）
- 1 个新单测文件（6 条）

---

## ADR-038: 风险路由驱动的 ReviewTask 编排（Phase 1：冻结状态契约与最终拓扑）

**日期**：2026-07-10
**状态**：已实现（Phase 1）
**关联**：ADR-032 ReviewCouncil、ADR-036 ContextProvider AST 富化
**设计文档**：`docs/superpowers/specs/2026-07-10-risk-routed-review-orchestration-design.md`（含 6 阶段实施台账）
**实施计划**：`docs/superpowers/plans/2026-07-10-risk-routed-review-orchestration-phase1.md`

### 背景

ADR-032 的默认编排调度单位接近"整份 diff + 三路发现者"。大 diff 下缺少任务级目标、缺少风险路由信号、缺少统一预算入口，且 trace 无法解释"为什么审这个 hunk、不审那个"。目标是引入 `ReviewTask + RiskProfile + TaskContextBundle` 中间链路，把"整份 diff 审查"升级为"风险路由驱动的任务审查"。Phase 1 只做一件事：**一次性冻结完整状态主干与最终拓扑**，后续阶段只填充节点内部策略、不再新增改变主路由的业务 State 字段。

### 决策

- **一次性引入 5 个规范化 State 字段**（`review_budget` / `review_tasks` / `risk_profiles` / `task_selection` / `task_context_bundles`），承载不变链路 `ReviewTask → RiskProfile → TaskSelection → TaskContextBundle → CandidateIssue(task_id) → EvidenceRequest → EvidenceNote → Verdict → ReviewResult`。模型放 `models/tasks.py`（`ContextFact` 复用自 `models/council.py`，单向依赖）。
- **规范化而非胖对象**：`TaskContextBundle` 不复制 file/patch/RiskTag，`RiskProfile` 不保存 `total_score`（分数是 TaskRank 的派生计算）。事实源单一所有者，下游不回写上游；工作队列只追加。
- **三个薄准备节点**：`diff_task_builder`（每 hunk 一个 `ReviewTask`，无 hunk 退化为文件级 fallback）→ `risk_triage`（Phase 1 空画像）→ `task_rank`（Phase 1 全选）→ 前置于 `context_provider`。均为纯函数（`pipeline/task_prep.py`），不判断风险、不读仓库、不调 LLM。
- **`CandidateIssue.task_id` 字面必填**：收集节点用确定性映射 `map_candidate_to_task(file, line, tasks)` 回填。ReviewCouncil 仍吃整份 diff（Phase 1 非目标之一），task_id 由收集节点回填。
- **候选映射严格绑定 changed 区域**（复审修正）：精度递减为 changed_lines 命中 → hunk 覆盖行范围（含上下文行，从 hunk_header 解析）→ 该文件的文件级 fallback → 否则 `None`。**绝不把无法绑定的候选归属到"第一个"task**——否则风险/上下文/证据会错挂到无关任务。无法映射留 `candidate_rejected_unmapped` trace。
- **删除/纯重命名文件也生成 fallback task**（复审修正）：`split_diff_by_file` 只认 `+++ b/`，会漏掉删除（`+++ /dev/null`）与纯重命名（无 `+++`）。`build_tasks` 补扫这两类块（删除取旧路径、重命名取新路径），使"删掉鉴权/校验/事务代码"的候选不被丢弃。
- **收集节点按 `task_selection` 收口**（复审修正）：候选映射到的任务不在 `selected_task_ids` 内 → 拒绝并留 `candidate_rejected_unselected`。Phase 1 全选下为 no-op，但契约已收口，Phase 2 启用 Top-K/预算即刻生效。`review_budget` 写入 orchestrator 初始 State。
- **移除 `council_route`，固定主路由**：`discover_*(×3) → council_coordinator(fan-in 一次) → evidence_agent(必经一次，无请求则 no-op) → council_judge → [needs_more 且轮次未超 → evidence_agent | END]`。首次 Evidence 不再由条件路由决定；coordinator 退化为纯 fan-in barrier。

### 放弃的方案

- **胖 ReviewTask 对象**（`task.risk_profile/context_bundle/findings/verdict`）：改为按 `task_id` 关联的规范化 state，避免多份可变事实漂移。
- **`task_id` 给默认值 + 边界校验**：改为字面必填，让契约由类型系统保证而非运行时约定。
- **候选映射兜底到首个 hunk**：违反"无法绑定 changed line → drop"契约，会错挂风险/证据；改为严格绑定 + 拒绝。
- **Phase 1 就做 hunk 级定向审查 / 风险规则 / 预算 Top-K / 定向上下文**：刻意推迟到 Phase 2-6，Phase 1 只冻结契约与拓扑。

### 影响

- 新增 Python：`models/tasks.py`、`pipeline/task_prep.py`；改动 `models/council.py`（task_id）、`pipeline/graph.py`（节点+拓扑）、`pipeline/orchestrator.py`（review_budget）。
- 产品契约 `ReviewResult` / `Issue`（`models/schemas.py`）**零改动**——任务/风险/证据仅进 state/trace/eval。
- 测试：`test_tasks_models.py`(7)、`test_task_prep.py`(14)、`test_graph_orchestration.py`（拓扑+映射+fan-in-once+两类拒绝）；全套 275 passed，ruff/mypy clean。
- **观测层遗留**：`observability/collector.py` / `view_model.py` 仍防御式 `.get("council_route")`（恒空、不崩），清理留 Phase 6。

### 给后续阶段的接缝（Phase 2-6 直接插这里，不得新增改主路由的 State）

- **Phase 2（任务准备链，已实现）**：`task_prep.triage_tasks` 已接入 23 个细粒度规则、固定 reviewer 映射、聚合/诊断和 `GENERAL_REVIEW` 兜底；`rank_tasks` 已按派生风险分数 + `ReviewBudget` 实现 Top-K/单文件上限/生产代码优先/跳过原因；`risk_routing.py` 和 `graph.py` 已将 selected task scope 派发给固定三路 reviewer。未改 State 字段，未引入 AST。
- **Phase 3（风险感知上下文）**：`_context_provider_node` 目前对每个选中任务产出空 `TaskContextBundle`；改为按 `risk_profiles[task_id]` 的 RiskTag 走定向 context strategy 填 `facts`，经 Java Gateway 收集并记来源/截断。
- **Phase 4（定向发现）**：在 Phase 2 已有 task-scoped reviewer 输入基础上，补充 task + risk profile + task context 的结构化提示和工具查询边界；fan-out 与工具 allowlist 继续从已有 state 纯计算，**不引入 assignment 类 State**。
- **Phase 5（任务化证据/裁决）**：`evidence_agent` 通过 `candidate_id → task_id` 读风险/上下文按 RiskTag 分流补证；`council_judge` 把 task/risk/context/evidence 统一用于 keep/drop/downgrade/merge。首次 Evidence 必经已固定，额外补证仍只追加 `evidence_requests`。
- **Phase 6（Trace/Eval 闭环）**：trace 已记 `tasks_built/profiled/selected/candidate_rejected_unmapped/candidate_rejected_unselected/fan_in`；Dashboard 展示任务/风险/选择跳过/证据链，并清理 `council_route` 恒空读取。
- **后续需注意**：当前 task_id 只是回溯标签（ReviewCouncil 仍整份 diff 审查）；Phase 4 令其真正驱动 fan-out 时，需复核 `_candidate_dedup_reducer` 的邻行(±3)合并会否跨 task 合并候选。
## ADR-039: Phase 2 风险标签、预算与 reviewer task scope 已落地

**日期**: 2026-07-10
**状态**: 已接受并实现
**关联设计**: `docs/superpowers/specs/2026-07-10-risk-triage-phase2-design.md`

### 决策

1. 风险规则只使用文件路径和 diff 文本的变化方向信号：`path`、`text:added`、
   `text:deleted`、`text:changed`。删除不是独立业务风险类别，而是保留为独立来源，
   因为删除保护代码与新增/修改代码具有不同的路由含义。
2. 规则覆盖 23 个细粒度 `RiskTag`，用 `RiskProfile.tag_scores` 聚合并限幅；路径信号
   不能单独制造具体标签。没有具体文本命中时统一生成 `GENERAL_REVIEW`，固定分派给
   ThreatModel、Behavior、Maintainability 三路。
3. TaskRank 使用既有 `ReviewBudget` 和 `TaskSelection`，默认总预算 100、单文件 10。
   排序分数只在节点内派生，不写入 State；跳过任务保留 `per_file_limit` 或 `total_limit`
   以及派生风险分数。
4. reviewer 分派由固定 catalog 从标签集合派生，不新增 `assigned_reviewers` State 字段。
   reviewer 没有匹配 task 时空运行并记录 `no_tasks_routed`；候选收集继续执行 selected
   gate 后再执行 routed gate，越界候选记录 `candidate_rejected_unrouted`。
5. 三路发现者仍保留 Direct/ReAct 两种执行方式和既有工具 allowlist；Phase 2 只缩小输入
   scope，不在规则节点引入 AST、Java Gateway 或 LLM 判断。

### 为什么这样选

- 将“是否值得审”与“是否真的有问题”分开，允许规则激进地保证召回，再由 ContextProvider、
  reviewer、EvidenceAgent 和 CouncilJudge 进行事实补正。
- 将路径、增加、删除、修改建模为信号来源/方向而不是最终标签，避免标签重叠和删除行为被
  新增文本规则吞掉。
- 把 reviewer 范围作为派生值，避免在 `RiskProfile` 和额外 assignment 字段之间维护两份事实，
  也让后续上下文节点可以直接消费同一份画像。

### 验证与边界

本阶段新增确定性规则、TaskRank、配置、路由和图回归测试；产品 `ReviewResult` / `Issue`、
`ReviewState` 字段形状和 Evidence 首次必经拓扑保持不变。AST 风险判断、RiskTag 感知的
ContextProvider、任务化 Evidence/Judge 和专项 Dashboard 指标留给后续阶段。

验证记录：全量 pytest `374 passed`，ruff/mypy clean；mock CLI EXIT=0；`pipeline-notools`
mock eval 成功运行 28 个样本，报告写入 `services/agent/evals/reports/pipeline.md`。mock
指标不代表审查质量，真实质量评测留待配置真实模型后执行。

---

## ADR-040: Phase 3 风险感知 ContextProvider

**日期**: 2026-07-11
**状态**: 已实现

### 决策

`TaskContextBundle.facts` 按已选任务的 `RiskProfile` 分层填充：Level0 对既有 AST 与敏感 API 结果做零新增调用的切片；Level1 仅按标签定向调用 `find_callers` 或 `get_code_metrics`，按调用键去重后以最多 8 个线程执行。失败工具响应只进入 trace，不作为事实写入。

每任务复用 `ReviewBudget.max_context_chars_per_task=4000` 截断事实。方法名必须从 AST 方法范围解析，无法解析则记录 `no_method_resolved`；不从 diff 猜测。未新增 State、Java 工具、HTTP 协议或风险判断。

### 验证

新增纯函数和图集成回归，覆盖 Level0 切片、Level1 去重、GENERAL_REVIEW、方法未解析、预算截断与失败响应隔离。pytest、ruff 与 mypy 通过；任务级独立审查未发现生产缺陷。

---

## ADR-041: Phase 5 风险标签驱动的证据规划与裁决链

**日期**: 2026-07-13
**状态**: 已实现
**关联**: ADR-032、ADR-038；`docs/superpowers/specs/2026-07-12-risk-routed-evidence-planning-phase5-design.md`

### 背景

原证据链把低置信候选变成泛化工具请求，并可能把任意非空工具输出当作支持证据；Judge
还能通过自由工具建议绕开既定调查边界。任务级 RiskTag 又只是审查先验，不能直接代表候选
声称的问题主题。继续在旧 EvidenceAgent 中追加判断会把规划、执行和裁决揉在一个节点里，
无法解释证据为什么足以 keep/drop/downgrade。

### 决策

1. **规划与执行分离**：在 Coordinator 与 EvidenceAgent 之间加入 `EvidencePlanner`。
   Planner 从候选语义解析 candidate evidence tag，按静态注册表写 `EvidenceRequest`；Judge
   回环只表达 `requested_purpose=support|counter|severity`，再回 Planner 选下一策略。
2. **候选主题优先，task RiskTag 只作先验**：23 个具体 RiskTag 与 `GENERAL_REVIEW`
   都注册可执行 counter/support/severity 策略。分类器先走确定性 exact/strong/weak 规则，
   歧义时才复用异源 judge LLM，失败退回 GENERAL，不让 task 标签覆盖候选语义。
3. **finding 取代字符串状态**：`EvidenceNote` 只保存 `findings[EvidenceFinding]`，每项明确
   evidence_id/source/observation/relation/strength/limitation。删除旧 status、supports/
   contradicts/unknowns/evidence_ids、`build_evidence_requests` 与全局 20 请求截断。
4. **安全回退方向固定为 insufficient**：工具失败/禁用/空结果、上下文截断、方法或调用方
   无法解析、LLM `None` 均不得推导支持或反证。AUTHORIZATION/TRANSACTION 的 direct
   counter 只接受当前 task 所属方法或类的确定性保护注解；analyst LLM 即使声称
   `direct + contradicts` 也强制降为 contextual。同文件其他方法、另一类和无法解析 scope
   都不能触发确定性 drop，已合法生成的 prior direct finding 仍可复用。
5. **Judge 使用目的感知矩阵**：direct counter 可确定性 drop；severity 反证只允许降级/保留；
   support 不 fast-keep；默认执行两轮，首轮 needs_more 回 Planner，第二轮强制收口。
   `CODEGUARD_MAX_EVIDENCE_ROUNDS` 只接受 1 或 2。最终 Issue 通过 Judge 的 survivor
   candidate ID 映射生成，不修改上游 CandidateIssue。
6. **观测不扩张业务 State**：六个过程指标写入既有 `CouncilRunStats` 侧信道并进入 eval
   schema/report/archive。EvidenceAgent 每次真实新工具调用发 `evidence_tool_called` trace；
   缓存命中只记 reused，因此不会把 ContextProvider/Discover 调用或 reducer 去重误算为成本。

### 权衡与放弃

- 不让 Judge/LLM 直接选择工具：牺牲开放探索，换取可审计的工具权限与稳定成本边界。
- 不从“未找到保护”推出漏洞成立：会保守保留部分候选，但避免工具/上下文缺陷制造伪证据。
- 不保留旧 EvidenceNote 兼容层和请求总量 cap：一次迁移所有消费者；成本先通过指标观测，
  若不可接受再设计候选预聚合，不能静默剥夺后序候选的反证机会。
- 不新增顶层 ReviewState、Java 工具或 Issue 字段；CandidateDossier、候选主题和策略均为节点
  内临时视图/静态值。

### 验证

实现 commits `5737c08…42dfe83`。确定性测试覆盖 24 个标签三目的注册、候选分类、counter-first
规划、回环 exhausted、请求字段校验、工具缓存、当前方法/类反证范围、空/失败/None 回退、
恶意 analyst direct 降级、prior direct 复用、默认两轮回环与 1/2 配置校验、Judge survivor
映射和六个指标的分子/分母/零分母语义。全量 pytest `611 passed`，Ruff/mypy clean，mock CLI
EXIT=0；`pipeline-notools` mock eval 完成 31 cases 并生成报告。mock 档实际证据工具调用为 0，
配置的本地 Gateway 健康检查超时，未声称真实 tool-profile 成本结果。

---

## ADR-042: GitHub Check Run 反馈采用严格请求契约、diff annotation 与有限重试

**日期**: 2026-07-14
**状态**: 已实现
**关联设计**: `docs/superpowers/specs/2026-07-14-github-check-run-feedback-hardening-design.md`

### 背景

PR 审查能成功获取 installation token 并创建 Check Run，但完成 PATCH 收到 GitHub HTML 400；
随后的无 annotations 降级请求又遇到 `Connection reset`。原客户端没有显式 JSON Content-Type、
固定 API 版本或应用 User-Agent；反馈层还会把所有 LLM issue 原样转为 annotation，不校验
文件与行号是否处于本次 PR diff。

### 决策

1. 所有 GitHub REST 请求统一发送 vendor Accept、JSON Content-Type、固定 API 版本与 Codeguard
   User-Agent。非成功 Check Run 响应记录状态码、GitHub request id、响应类型和受限长度摘要，
   不记录 Authorization/token。
2. 只有幂等的 Check Run PATCH 自动重放：网络 `IOException` 或 HTTP 502/503/504 最多重试一次，
   默认等待一秒；4xx 不重试，创建评论等可能产生重复副作用的 POST 也不重试。
3. Check Run annotations 只保留能映射到当前 unified diff new-side hunk 的 issue。先过滤再应用
   单次 50 条上限，避免非法前缀耗尽预算；无法映射的 issue 仍保留在 summary，CRITICAL 项继续
   走既有普通 PR 评论降级。
4. 保留秒级 `completed_at`、summary 65,000 字符和 annotation message 200 字符的防御性截断。

### 放弃与边界

- 不对所有请求做通用重试，避免重复创建 Check Run 或 PR 评论。
- 不把 400/422 当瞬态错误重试，也不做无限退避。
- 本期不实现 annotation 二分上传；通过过滤和 request id 诊断先消除已知无效输入。
- 不修改产品 `Issue`、ReviewJob 数据库结构或 GitHub App 权限。

---

## ADR-043: EvidencePlanner 与 EvidenceAgent 采用有界并发

**日期**: 2026-07-14
**状态**: 已实现
**关联**: ADR-041；`pipeline/concurrency.py`

### 背景

Phase 5 的证据语义已经稳定，但 Planner 对候选主题逐个解析、Agent 对工具与事实逐项执行，
使候选较多的审查耗时近似按 LLM 调用数线性增长。直接把整个 EvidenceRequest 扔进线程池会
让相同工具调用在缓存写入前重复执行，也会使 Note 与 trace 顺序随线程完成顺序漂移。

### 决策

1. Planner 仅并发候选主题解析，最多使用 8 个线程；解析结果按 dossier 输入顺序回收，初轮仍
   保持“全部 counter 在前、满足 gate 的 support 在后”的两遍规划，回环仍只处理
   `needs_more_evidence` 候选。
2. Agent 分为准备、工具执行、事实分析、稳定组装四步。准备阶段先完成 request/strategy 校验，
   并以 `(tool_name, canonical_arguments)` 对整批工具调用去重；唯一调用最多 8 路并发执行，
   同一结果再按各 task 作用域切片并回填。
3. 所有待分析事实扁平化后最多 8 路并发调用 analyst LLM，最后按 request 顺序和 fact 顺序重建
   `EvidenceNote` 与 finding trace。单项并发异常只把对应结果降级为 `analyst_error` 或明确的
   tool limitation，不丢弃其他请求。
4. 继续复用同步 `ToolClient` 与现有 `run_bounded_parallel`；不切 async，不新增 State、模型字段、
   Java 协议或产品 Issue 字段。`evidence_tool_called` 仍只统计去重后真实发生的调用，后续复用记
   `evidence_tool_reused`。

### 权衡与边界

- 本次只提供单次审查进程内的批次去重，不引入 Redis。Redis 不能缩短首次审查的 LLM 调用，
  且跨审查缓存必须额外解决 repo/head SHA、prompt/model/strategy 版本与失败结果失效问题。
- 每批并发调用沿用公共 helper 的硬上限 8，避免形成“每个 request 各开一组 worker”的乘法并发；
  该 helper 每批创建独立线程池，不代表跨节点或进程级全局限流。
- 保持输出稳定顺序优先于按完成时间流式写 State，便于 reducer、trace、eval 和故障复现继续使用
  确定性关联。

### 验证

新增测试分别测得 Planner candidate tag 解析、Agent 唯一工具调用和事实分析的并发峰值大于 1，
同时校验请求/Note 顺序稳定及跨请求 `find_sensitive_apis` 只真实调用一次。全量 Python
`638 passed`，Ruff 与 mypy clean；本次不修改 Java 代码，因此未重复执行 Gateway 构建。

---

## ADR-044: Java Gateway 采用单实例可靠审查执行模型

**日期**: 2026-07-15
**状态**: 已实现

### 背景

原 Java CI 路径把 git、Python 进程、H2 状态、同步 sleep 重试和 GitHub 反馈集中在一个执行器中；
共享 workspace、共享 JDBC Connection、递归重试和 token URL 注入会放大并发、恢复与凭据风险。

### 决策

1. `ReviewExecutor` 只执行一次审查并返回结构化 outcome。Python stdout 满足既有
   `ReviewResult(issues/summary)` 契约时，exit 0 与 exit 1 都是成功；exit 1 继续表达 CRITICAL 门禁。
2. `JobScheduler` 统一持久化状态、最多两次延迟重试、进程内 job 去重、反馈和优雅停机。
   重试使用独立定时器，不占用审查 worker；反馈失败不触发重新审查。
3. workspace 按 repo/PR/完整 SHA 隔离；进程超时清理完整后代树，输出有界保留但持续排空。
   GitHub token 只通过 Git 子进程环境传递，命令与错误统一脱敏。
4. 保留 H2 并串行化仓库操作，提供 `ping` 和幂等关闭。系统明确只支持单实例，不引入
   MySQL、Redis、MQ、租约或分布式锁。
5. 增加集中配置、live/ready、Prometheus 指标、JaCoCo 门槛和跨语言 CI。

### 权衡与边界

- 单实例恢复优先于分布式扩展；进程崩溃时 RUNNING/RETRYING 会在下次启动恢复为 PENDING。
- 可重试失败保留 workspace 便于复用和排障，成功、不可重试或重试耗尽后清理。
- 指标禁止使用 repo、PR、SHA、jobId 高基数字段；它们只出现在结构化日志中。

---

## ADR-045: 大 diff 由 Python 风险路由做确定性范围降级

**日期**: 2026-07-16
**状态**: 已实现

### 背景

Java Gateway 曾保留 `CODEGUARD_MAX_DIFF_LINES` 和“超限后伪造 WARNING 结果并跳过审查”的接口，
但真实审查成本发生在 Python 的 Summary、任务发现和取证链。Java 只按行数跳过既无法利用风险画像，
也会形成两套降级策略，并把“未审查”伪装成审查结论。

### 决策

1. Python 在 diff 超过 5000 行或 `ReviewTask` 超过 50 个时进入确定性降级；用户配置更严格时
   保留其配置，否则预算收紧为总任务 20、每文件 3、每任务上下文 2000 字符。
2. 图顺序改为 `DiffTaskBuilder → RiskTriage → TaskRank → Summary → ContextProvider`。
   Summary 与 diff AST 只消费选中任务重建出的 diff；选中 diff 限 60000 字符，单任务 patch
   限 12000 字符。大 diff 模式不执行无任务范围的前置敏感 API 全仓扫描。
3. 三路发现者继续按既有风险画像路由，不把低风险任务改成新的旁路。CouncilJudge 最终摘要
   明确给出总任务、已审任务、跳过任务和“不代表完整覆盖”，并建议拆分 PR。
4. Java 删除大 diff 阈值、降级 JSON 和基于异常文本的重试判断；Gateway 只保留入口限流，
   重试继续由结构化 `FailureCode` 驱动的 `JobScheduler` 负责。

### 权衡与边界

- 本轮不引入新 State、产品 Issue 字段、数据库、中间件或 Java 工具；大 diff 决策可由现有 State
  重复确定性派生。
- 固定阈值优先保证策略简单、可解释和快速落地；未来若 eval 显示不同语言/仓库需要不同预算，
  再把阈值集中到 Python Settings，而不是复制到 Java。
- 跳过前置全仓扫描不禁止后续 EvidenceAgent 针对已选候选调用受约束工具。

### 验证

确定性测试覆盖阈值边界、预算收紧、选中 diff/单任务截断、图节点顺序、Summary/AST 作用域、
大 diff 跳过广域扫描和最终部分覆盖提示；Java 测试确认 ReviewGuard 只保留 Webhook 限流。
