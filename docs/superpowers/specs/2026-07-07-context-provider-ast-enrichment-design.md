# ContextProvider AST 富化设计

**日期**: 2026-07-07
**状态**: 设计完成，待实现
**关联 ADR**: 待分配编号（写入 DECISIONS.md）

---

## 概述

为 `context_provider` 节点增加 diff 文件 AST 提取能力，使三个发现者 Agent 在推理前共享同一份代码结构上下文，减少冗余的 `get_file_content` 调用。

**两层策略**：
- **Layer 1（本次实现）**: `context_provider` 内联 AST — 只覆盖 diff 文件本身，不扩展邻域，零 Agent 工具调用
- **Layer 2（后续 change）**: 跨文件探索工具 — `get_method_definition`（新增）、`find_callers` 扩展（direction+depth）、`get_related_files`（暂缓）

设计参照 Diffguard 的 `ASTEnricher` + `ASTContextBuilder` 两层模式。

---

## 架构总览

```
                     context_provider
                          │
           ┌──────────────┼──────────────┐
           │              │              │
  parse_changed_files  find_sensitive_apis  get_diff_ast ★新增
           │              │              │
           ▼              ▼              ▼
    ContextFact        ContextFact     ContextFact × N
    (changed_file)     (sensitive_api) (ast_structure, 每文件一条)
           │              │              │
           └──────────────┼──────────────┘
                          ▼
                   ContextBundle
                          │
             ┌────────────┼────────────┐
             ▼            ▼            ▼
       ThreatModel    Behavior    Maintainability
       (共享同一份 AST 上下文，不再各自 get_file_content 理解骨架)
```

### 关键决策：独立 AST 体系，不复用 repomap Tag

`JavaTagExtractor` / `Tag` / `repomap/` 包服务于 PageRank 建图，其模型只含 `(relFile, name, kind, line, signature)`，不足以表达可见性/注解/控制流等完整 AST 信息。新建独立的 `ast/` 包处理 AST 提取与格式化，Tag 体系保持不动。

### 关键决策：按文件独立预算，不参与 ContextBundle 全局预算

每个文件的 AST 单独计算 `max(20% × diff_tokens, 600)`（floor=50），不和 ContextBundle 的 6000 全局预算争抢。AST 事实以 `ContextFact` 存储，但其内容长度不受 `_FACT_BUDGET(4000)` 约束——该约束仅用于 `find_sensitive_apis` 等可控事实源。

### 关键决策：注入 ContextBundle，不动 diff 文本

AST 以 `ContextFact(kind="ast_structure")` 存入 ContextBundle，在 `<shared_context>` 块中呈现。不修改原始 diff 文本。所有 Agent 看到统一的结构化视图。

---

## Java 侧新增

### 文件清单

| 文件 | 职责 |
|------|------|
| `agent/ast/DiffASTResult.java` | 数据容器：`filePath` + `classes` + `methods` + `controlFlowNodes` + `callEdges` |
| `agent/ast/DiffASTAnalyzer.java` | 用 JavaParser 解析单文件源码 → `DiffASTResult` |
| `agent/ast/ASTContextFormatter.java` | `DiffASTResult` + diff 文本 → 格式化 LLM 文本，按预算两级裁剪 |
| `agent/tools/GetDiffASTTool.java` | `AgentTool` 实现：遍历 `AgentContext.allowedFiles` 的每个 .java 文件，调 `DiffASTAnalyzer` + `ASTContextFormatter`，返回合并文本 |

所有类放在 `com.codeguard.agent.ast` 包下。

### 数据模型 (`DiffASTResult`)

```java
public record DiffASTResult(
    String filePath,
    boolean parseSucceeded,
    List<ClassDef> classes,
    List<MethodDef> methods,
    List<CFNode> controlFlowNodes,
    List<CallEdgeDef> callEdges
) {}

// ClassDef: name, type(class/interface/enum/record), superClass, interfaces, fields(type+name), startLine, endLine
// MethodDef: name, returnType, paramTypes[], paramNames[], visibility, modifiers[], annotations[], startLine, endLine
// CFNode: type(IF/FOR/WHILE/TRY_CATCH/SWITCH等), startLine, endLine, condition
// CallEdgeDef: callerMethod, calleeMethod, calleeScope, line
```

### `DiffASTAnalyzer`：AST 提取

用 JavaParser `parse(source)` → `CompilationUnit`，提取：

1. **ClassDef**：`ClassOrInterfaceDeclaration` → name/type/superClass/interfaces；`FieldDeclaration` → fields
2. **MethodDef**：`MethodDeclaration` → name/returnType/paramTypes/paramNames；`getAccessSpecifier()` → visibility；`getModifiers()` → modifiers；`getAnnotations()` → annotations
3. **CFNode**：`IfStmt`/`ForStmt`/`ForEachStmt`/`WhileStmt`/`DoStmt`/`TryStmt`/`SwitchStmt`/`SynchronizedStmt` → type + startLine/endLine + condition 文本（截断到 60 字符）
4. **CallEdgeDef**：`MethodCallExpr` → callerMethod(所在方法名) + calleeMethod(被调用方法名) + calleeScope(表达式作用域如 `userService.save` → `userService`) + line

解析失败 → `parseSucceeded=false`，上层跳过该文件。

### `ASTContextFormatter`：格式化 + 裁剪

**输入**：`DiffASTResult` + 原始 diff 文本 + diff token 数

**预算**：`max(20% × diff_tokens, 600)`，floor=50

**Full 模式（Tier 0）输出格式**：

```
class: OrderService extends BaseService implements Auditable
  Fields: OrderRepository orderRepo, PricingEngine pricingEngine
  Methods:
    @Override public BigDecimal calculatePrice(Order order) [L42-L70] -> calls: orderRepo.findById, pricingEngine.calculate
    private void validate(String input) [L78-L95]
    public static OrderService create() [L100-L115]
  Control Flow:
    IF [L45-L55] order == null
    TRY_CATCH [L60-L68] 
```

**格式规则**：
- 方法按"是否 diff 行范围内 → 行号"排序，diff 范围内的优先
- 可见性 `package-private` 不打印（减少噪音）
- 调用边以 `-> calls: a, b, c` 格式逐方法内联
- 控制流仅展示 diff 行范围内的节点，diff 无行号时展示全部

**两级裁剪**：

```
Tier 0 (Full): 类结构 + 全部方法(含可见性/注解/调用边) + diff 范围内控制流
    │
    │ 超预算 → Tier 1 (Diff-scoped)
    ▼
  类结构 + 仅 diff 行范围内的方法 + diff 范围内控制流
    │
    │ 仍超 → Tier 2 (Minimal)
    ▼
  类名 + 方法签名列表 "method1(...), method2(...), ..."
```

**diff 行号提取**：从 unified diff 的 hunk header（`@@ -a,b +c,d @@`）解析 `+` 行号。

### `GetDiffASTTool`：工具实现

```java
public final class GetDiffASTTool implements AgentTool {
    // name() → "get_diff_ast"
    // description() → "获取本次改动涉及文件的 AST 结构信息..."
    // execute(input, context) → 
    //   input 为原始 diff 文本(string)
    //   从 input 解析各文件的 diff 片段(行号+内容)，计算总 diff 字符数
    //   遍历 context.getAllowedFiles()
    //     仅处理 .java 文件
    //     从 repoRoot 读文件内容(通过 FileAccessSandbox)
    //     调 DiffASTAnalyzer.analyze()
    //     解析失败 → 跳过(记录日志)
    //     从 diff 文本提取该文件的 changedLines
    //     调 ASTContextFormatter.format()，传入 diff 字符数 + changedLines
    //   返回多文件合并文本，每文件以 "AST for: <path>" 分隔
}
```

**diff 传递路径**：Python `context_provider` 持有 `context.diff_text`，通过 `tool_client.get_diff_ast(diff_text)` → HTTP body `{"input": "<diff_text>"}` → `GetDiffASTTool.execute(input, ...)`。diff 文本可能较大，但这是一次性调用（context_provider 内），不循环。

**Token 估算**：沿用 `RepoMapRenderer` 的 `CHARS_PER_TOKEN=4` 廉价近似，不引入额外 tokenizer 依赖。

**输出格式**（多文件）：

```
AST for: src/main/java/com/example/OrderService.java
  class: OrderService
    ...

AST for: src/main/java/com/example/PricingEngine.java
  class: PricingEngine
    ...
```

**异常处理**：单文件解析失败不影响其他文件；全部失败返回 `"(无可解析的 Java AST 上下文)"`。

### 注册

在 `ToolSessionManager.create()` 中注册：

```java
this.registry.register(new GetDiffASTTool(sandbox));
```

sandbox 负责文件读取和路径验证，`GetDiffASTTool` 不直接访问文件系统。

---

## Python 侧改动

### `context_provider.py` 改动

在 `find_sensitive_apis` 之后、ContextBundle 构建之前，追加：

```python
# 4. AST 结构提取 (diff 内文件)
if context.tool_client is not None:
    resp = context.tool_client.get_diff_ast()
    content = resp.as_tool_output() if hasattr(resp, "as_tool_output") else str(resp)
    if content.strip() and "无可解析" not in content:
        for file_block in _split_ast_blocks(content):
            facts.append(ContextFact(
                source="tool:get_diff_ast",
                kind="ast_structure",
                content=file_block,
            ))
        gathered.append(GatheredContext("get_diff_ast", "{}", content))
        sources.append("tool:get_diff_ast")
```

其中 `_split_ast_blocks` 以 `"AST for:"` 为分隔符拆分多文件文本块。

### `tool_client.py` 新增方法

```python
def get_diff_ast(self) -> ToolResponse:
    return self._call("get_diff_ast", {})
```

`_call` 是已有通用方法，与其他工具一致。

---

## 死代码清理：repomap 迁移

### 背景

`get_repo_map` 工具早已断开注册（`ToolSessionManager.java:60` 注释），其调用方追踪能力被 `find_callers` 取代，邻域导航能力被 `GetDiffASTTool`（本次）+ Layer 2 `find_callers` 扩展（后续）覆盖。`repomap/` 包和 `GetRepoMapTool` 已成为死代码。

### 迁移清单

所有文件从 `src/main/java/com/codeguard/agent/` 移动到 `legacy/` 目录下，不再参与编译：

**主代码**：
| 原路径 | 迁移目标 |
|------|---------|
| `agent/tools/GetRepoMapTool.java` | `legacy/tools/GetRepoMapTool.java` |
| `agent/repomap/Tag.java` | `legacy/repomap/Tag.java` |
| `agent/repomap/TagExtractor.java` | `legacy/repomap/TagExtractor.java` |
| `agent/repomap/JavaTagExtractor.java` | `legacy/repomap/JavaTagExtractor.java` |
| `agent/repomap/TagExtractorRegistry.java` | `legacy/repomap/TagExtractorRegistry.java` |
| `agent/repomap/PageRank.java` | `legacy/repomap/PageRank.java` |
| `agent/repomap/RepoMapRanker.java` | `legacy/repomap/RepoMapRanker.java` |
| `agent/repomap/RepoMapRenderer.java` | `legacy/repomap/RepoMapRenderer.java` |
| `agent/repomap/RepoMapBuilder.java` | `legacy/repomap/RepoMapBuilder.java` |

**测试代码**：
| 原路径 | 迁移目标 |
|------|---------|
| `src/test/.../tools/GetRepoMapToolTest.java` | `legacy/tools/GetRepoMapToolTest.java` |
| `src/test/.../repomap/*Test.java` (4 个文件) | `legacy/repomap/*Test.java` |

**遗留引用清理**：
- `ToolSessionManager.java`: 删除 `// get_repo_map 已断开调用...` 注释行
- `FindCallersTool.java`: 删除 javadoc 中的 `{@link com.codeguard.agent.repomap.JavaTagExtractor}`（仅注释引用，无代码依赖）
- `FileAccessSandbox.java`: 删除 javadoc 中关于 `get_repo_map` 的过时说明

### 放置位置

沿用项目已有的 legacy 模式（`services/agent/legacy/`），Java 侧对应：
```
services/gateway/legacy/
├── tools/
│   └── GetRepoMapTool.java
├── repomap/
│   ├── Tag.java
│   ├── TagExtractor.java
│   ├── JavaTagExtractor.java
│   ├── TagExtractorRegistry.java
│   ├── PageRank.java
│   ├── RepoMapRanker.java
│   ├── RepoMapRenderer.java
│   └── RepoMapBuilder.java
└── test/
    ├── GetRepoMapToolTest.java
    ├── JavaTagExtractorTest.java
    ├── RepoMapRankerTest.java
    ├── RepoMapRendererTest.java
    └── TagExtractorRegistryTest.java
```

---

## 不受影响的部分

- `DEFAULT_REVIEWERS` 的 `tool_allowlist`：不加 `get_diff_ast`（这是 context_provider 专属工具）
- 现有四个 Agent 工具：不变
- `parse_changed_files`：保留，是 `find_sensitive_apis` 和 `get_diff_ast` 的上游依赖
- `ContextBundle.changed_files` 和 `diff_summary`：保留，AST 与其并列不替代

---

## 测试策略

### Java 单测

| 测试 | 内容 |
|------|------|
| `DiffASTAnalyzerTest` | 正常 Java 文件输出完整 ClassDef/MethodDef/CFNode/CallEdgeDef；空文件/语法错误返回 `parseSucceeded=false`；验证可见性+注解+修饰符正确 |
| `ASTContextFormatterTest` | 小文件 Tier 0 包含全部方法；大文件触发 Tier 1 仅含 diff 行内方法；超大触发 Tier 2 极简模式；控制流仅展示 diff 行范围内节点 |
| `GetDiffASTToolTest` | mock 仓库含 3 个 .java 文件，验证输出含 3 段 "AST for:"；无 Java 文件返回空提示；单文件解析失败不影响其余 |

### Python 单测

| 测试 | 内容 |
|------|------|
| `test_context_provider_ast` | mock tool_client 返回多文件 AST，验证 ContextBundle.facts 含对应 `ast_structure` 条目；`_split_ast_blocks` 正确拆分 |

### 评测对照

新增 profile `pipeline-file-ast`（`pipeline-file` + `get_diff_ast`），与 `pipeline-file` 对照，关注：
- 工具调用次数下降（`get_file_content` 调用减少）
- Token 效率（审查输出质量不变或提升，但输入 token 因 AST 替代了部分原始文件内容）

---

## Layer 2 预留（本次不实现）

| 工具 | 范围 | 用途 |
|------|------|------|
| `get_method_definition` | callee | 查 `文件#方法名` 完整签名+注解+所在类结构 |
| `find_callers`（扩展） | caller/callee/both + depth 1-3 | BFS 遍历调用链，替代独立的 `get_call_graph` |
| `get_related_files`（暂缓） | 类级 | 实现类/子类/依赖文件 |

---

## 分步实施

| 步骤 | 内容 | 预估工作量 |
|------|------|-----------|
| Step 1 | Java 侧：`DiffASTResult` + `DiffASTAnalyzer` | 中 |
| Step 2 | Java 侧：`ASTContextFormatter` + 裁剪逻辑 | 中 |
| Step 3 | Java 侧：`GetDiffASTTool` + 注册到 ToolSessionManager | 小 |
| Step 4 | Python 侧：`tool_client.get_diff_ast()` + context_provider 接入 | 小 |
| Step 5 | 死代码清理：repomap 迁移到 legacy/，清理遗留注释引用 | 小 |
| Step 6 | 单测 + eval 对照 | 中 |
| Step 7 | ADR 写入 DECISIONS.md | 小 |
