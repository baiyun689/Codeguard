# 项目级语义代码图设计

## 目标

Java Gateway 为每个精确 revision 构建不可变 `ProjectSnapshot`。快照完整保留受审 Java 源码和 `CompilationUnit`，并投影出面向代码审查的 `ProjectCodeGraph`。完整图不进入 LLM 上下文；Python 只通过任务型工具取得有限、带来源的局部事实。

## 快照生命周期

工具会话以规范仓库路径、revision、图版本和分析配置组成 `ProjectKey`；本地工作树 revision 由完整 `HEAD` SHA 与本次 diff digest 共同组成。`ProjectSnapshotManager.getOrBuild` 对同键 single-flight，在会话创建时异步构建；ContextProvider 首次调用 `resolve_change_context` 时形成就绪屏障。缓存默认最多保留 4 个快照，访问后 30 分钟过期，活动 Session 持有直接引用。

快照包含全部源码文本、全部可解析 AST、符号索引、语义图和解析诊断。扫描排除 `.git`、`target`、`build`、`.gradle`、IDE 与依赖目录，并拒绝 Java 符号链接，防止快照越过仓库根目录。每个源码只读取一次，AST 与缓存文本来自同一字符串。工具不执行待审项目的 Maven 或 Gradle 脚本。

## 图模型与不确定性

节点包含文件、类型、方法、构造器、字段和框架入口；边包含声明、调用、字段读写、类型引用、继承、实现、重写、注解、注入、路由、事件和定时任务。每条边记录文件、行号、提取器以及 `resolved/ambiguous/unresolved`。

工具结果统一给出 `confirmed/not_found/unknown`、coverage、provenance 和 limitations。只有完整覆盖下的 `not_found` 才是有限的缺席事实；解析失败、外部依赖、反射、动态代理和生成代码均为 `unknown`。局部结果最多返回 100 个符号和 200 条关系；发生截断时 coverage 为 `partial` 并携带 `result_truncated`。

## 工具分配

- ContextProvider：`resolve_change_context`
- ThreatModelAgent：`get_file_content`、`inspect_security_path`
- BehaviorAgent：`get_file_content`、`inspect_change_impact`
- MaintainabilityAgent：`get_file_content`、`inspect_structure`

角色工具以 ContextProvider 给出的稳定 `symbol_id` 为输入。`get_file_content` 只读取当前 task 或快照确认的路径。旧 `get_diff_ast`、`find_callers`、`find_sensitive_apis`、`get_code_metrics` 是同一图谱实现上的兼容 Adapter。

## Evidence

EvidenceStrategy 声明 `CURRENT_IMPLEMENTATION`、`UPSTREAM_REACHABILITY`、`FRAMEWORK_ENTRY_REACHABILITY`、`SECURITY_PATH`、`STRUCTURAL_METRICS`、`INHERITANCE_IMPACT` 等证据能力；EvidenceAgent 再映射到具体工具。`unknown`、partial、超时和无效图谱信封一律产生 insufficient，Java 不判断事实是否构成漏洞。
