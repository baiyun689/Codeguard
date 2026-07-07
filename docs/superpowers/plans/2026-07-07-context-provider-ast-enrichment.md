# ContextProvider AST 富化 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `context_provider` 增加 diff 文件 AST 提取能力，三个发现者 Agent 共享代码结构上下文，减少冗余 `get_file_content` 调用。

**Architecture:** Java 侧新增 4 个文件（`DiffASTResult`/`DiffASTAnalyzer`/`ASTContextFormatter`/`GetDiffASTTool`），Python 侧改动 2 个文件（`context_provider.py`/`tool_client.py`），repomap 死代码迁移到 `legacy/`。

**Tech Stack:** Java 21 + JavaParser + JUnit 5, Python 3.12 + httpx + pytest

**Spec:** `docs/superpowers/specs/2026-07-07-context-provider-ast-enrichment-design.md`

---

## 文件结构

### 新建

| 文件 | 职责 |
|------|------|
| `services/gateway/src/main/java/com/codeguard/agent/ast/DiffASTResult.java` | 数据容器：`ClassDef`/`MethodDef`/`CFNode`/`CallEdgeDef` 的 record 定义 |
| `services/gateway/src/main/java/com/codeguard/agent/ast/DiffASTAnalyzer.java` | 用 JavaParser 解析单文件 → `DiffASTResult`（静态工具类） |
| `services/gateway/src/main/java/com/codeguard/agent/ast/ASTContextFormatter.java` | `DiffASTResult` + diff 文本 → 格式化 LLM 文本 + 两级裁剪（静态工具类） |
| `services/gateway/src/main/java/com/codeguard/agent/tools/GetDiffASTTool.java` | `AgentTool` 实现：遍历 diff 文件，调 Analyzer + Formatter，返回合并文本 |
| `services/gateway/src/test/java/com/codeguard/agent/ast/DiffASTAnalyzerTest.java` | Analyzer 单测 |
| `services/gateway/src/test/java/com/codeguard/agent/ast/ASTContextFormatterTest.java` | Formatter 单测 |
| `services/gateway/src/test/java/com/codeguard/agent/tools/GetDiffASTToolTest.java` | Tool 集成单测 |
| `services/agent/tests/test_context_provider_ast.py` | context_provider AST 接入单测 |

### 修改

| 文件 | 改动 |
|------|------|
| `services/gateway/src/main/java/com/codeguard/toolserver/ToolSessionManager.java:55-60` | 注册 `GetDiffASTTool`，删除 repomap 注释 |
| `services/gateway/src/main/java/com/codeguard/agent/tools/FindCallersTool.java:27` | 删除 javadoc 中的 `{@link JavaTagExtractor}` |
| `services/gateway/src/main/java/com/codeguard/agent/tools/FileAccessSandbox.java:15-19` | 删除 `get_repo_map` 相关过时注释 |
| `services/agent/src/codeguard_agent/pipeline/stages/context_provider.py:50-83` | 在 `find_sensitive_apis` 之后追加 `get_diff_ast` 调用 |
| `services/agent/src/codeguard_agent/tools/tool_client.py:73-75` | 新增 `get_diff_ast()` 方法 |

### 迁移到 legacy/

| 原路径 | 目标 |
|------|------|
| `agent/tools/GetRepoMapTool.java` | `services/gateway/legacy/tools/GetRepoMapTool.java` |
| `agent/repomap/*.java` (9 文件) | `services/gateway/legacy/repomap/*.java` |
| `src/test/.../tools/GetRepoMapToolTest.java` | `services/gateway/legacy/test/GetRepoMapToolTest.java` |
| `src/test/.../repomap/*Test.java` (4 文件) | `services/gateway/legacy/test/repomap/*Test.java` |

---

### Task 1: DiffASTResult 数据模型

**Files:**
- Create: `services/gateway/src/main/java/com/codeguard/agent/ast/DiffASTResult.java`

- [ ] **Step 1: 创建 DiffASTResult.java**

```java
package com.codeguard.agent.ast;

import java.util.List;

/**
 * 单个 Java 文件的 AST 分析结果。
 * <p>
 * 纯数据容器——不含任何解析逻辑。由 {@link DiffASTAnalyzer} 填充，
 * 供 {@link ASTContextFormatter} 消费。
 */
public record DiffASTResult(
        String filePath,
        boolean parseSucceeded,
        List<ClassDef> classes,
        List<MethodDef> methods,
        List<CFNode> controlFlowNodes,
        List<CallEdgeDef> callEdges) {

    /** 类/接口/枚举/record 的结构信息。 */
    public record ClassDef(
            String name,
            String type,        // "class" / "interface" / "enum" / "record"
            String superClass,  // extends 的父类名，无则为空串
            List<String> interfaces, // implements 的接口名列表
            List<String> fields,     // "type name" 格式的字段列表
            int startLine,
            int endLine) {}

    /** 方法/构造器的结构信息。 */
    public record MethodDef(
            String name,
            String returnType,
            List<String> paramTypes,
            List<String> paramNames,
            String visibility,      // "public" / "private" / "protected" / "package-private"
            List<String> modifiers, // static / final / synchronized / abstract
            List<String> annotations, // @Override / @Deprecated 等
            int startLine,
            int endLine) {}

    /** 控制流节点。 */
    public record CFNode(
            String type,        // IF / FOR / FOR_EACH / WHILE / DO_WHILE / TRY_CATCH / SWITCH / SYNCHRONIZED
            int startLine,
            int endLine,
            String condition) {}  // 条件文本，截断到 60 字符

    /** 调用边。 */
    public record CallEdgeDef(
            String callerMethod,  // 发起调用的方法名
            String calleeMethod,  // 被调用的方法名
            String calleeScope,   // 调用表达式的作用域（如 userService.save → "userService"），无为 ""
            int line) {}
}
```

- [ ] **Step 2: 编译验证**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn compile -q
```

Expected: BUILD SUCCESS

- [ ] **Step 3: Commit**

```bash
git add services/gateway/src/main/java/com/codeguard/agent/ast/DiffASTResult.java
git commit -m "feat(ast): 新增 DiffASTResult 数据模型——ClassDef/MethodDef/CFNode/CallEdgeDef"
```

---

### Task 2: DiffASTAnalyzer AST 解析器

**Files:**
- Create: `services/gateway/src/main/java/com/codeguard/agent/ast/DiffASTAnalyzer.java`
- Create: `services/gateway/src/test/java/com/codeguard/agent/ast/DiffASTAnalyzerTest.java`

- [ ] **Step 1: 写失败单测**

```java
package com.codeguard.agent.ast;

import org.junit.jupiter.api.Test;
import java.util.List;
import static org.junit.jupiter.api.Assertions.*;

class DiffASTAnalyzerTest {

    @Test
    void parsesSimpleClassWithMethod() {
        String source = """
            package com.example;
            import java.util.List;
            public class OrderService extends BaseService implements Auditable {
                private final OrderRepository orderRepo;
                public BigDecimal calculatePrice(Order order) {
                    return orderRepo.findById(order.getId());
                }
            }
            """;
        DiffASTResult r = DiffASTAnalyzer.analyze("src/main/java/com/example/OrderService.java", source);
        assertTrue(r.parseSucceeded());
        assertEquals(1, r.classes().size());
        DiffASTResult.ClassDef cls = r.classes().get(0);
        assertEquals("OrderService", cls.name());
        assertEquals("class", cls.type());
        assertEquals("BaseService", cls.superClass());
        assertEquals(List.of("Auditable"), cls.interfaces());
        assertTrue(cls.fields().contains("OrderRepository orderRepo"));
        assertEquals(1, r.methods().size());
        DiffASTResult.MethodDef m = r.methods().get(0);
        assertEquals("calculatePrice", m.name());
        assertEquals("BigDecimal", m.returnType());
        assertEquals("public", m.visibility());
        assertTrue(m.annotations().contains("Override"));
    }

    @Test
    void parsesMethodAnnotationsAndModifiers() {
        String source = """
            public class Util {
                @Override
                @Deprecated
                public static final synchronized void process(String input) {}
            }
            """;
        DiffASTResult r = DiffASTAnalyzer.analyze("Util.java", source);
        assertTrue(r.parseSucceeded());
        assertEquals(1, r.methods().size());
        DiffASTResult.MethodDef m = r.methods().get(0);
        assertEquals("process", m.name());
        assertEquals("public", m.visibility());
        assertTrue(m.modifiers().contains("static"));
        assertTrue(m.modifiers().contains("final"));
        assertTrue(m.modifiers().contains("synchronized"));
        assertTrue(m.annotations().contains("Override"));
        assertTrue(m.annotations().contains("Deprecated"));
    }

    @Test
    void parsesCallEdges() {
        String source = """
            public class Service {
                public void doWork() {
                    userRepo.save(new User());
                    log.info("done");
                    helper.audit();
                }
            }
            """;
        DiffASTResult r = DiffASTAnalyzer.analyze("Service.java", source);
        assertTrue(r.parseSucceeded());
        List<DiffASTResult.CallEdgeDef> edges = r.callEdges();
        assertEquals(3, edges.size());
        // 所有调用都来自 doWork
        assertTrue(edges.stream().allMatch(e -> e.callerMethod().equals("doWork")));
        // 有对应的 callee
        assertTrue(edges.stream().anyMatch(e -> e.calleeMethod().equals("save") && e.calleeScope().equals("userRepo")));
        assertTrue(edges.stream().anyMatch(e -> e.calleeMethod().equals("info") && e.calleeScope().equals("log")));
        assertTrue(edges.stream().anyMatch(e -> e.calleeMethod().equals("audit") && e.calleeScope().equals("helper")));
    }

    @Test
    void parsesControlFlow() {
        String source = """
            public class Logic {
                public void check(int x) {
                    if (x > 0) {
                        for (int i = 0; i < x; i++) {
                            try { doThing(); } catch (Exception e) {}
                        }
                    }
                }
            }
            """;
        DiffASTResult r = DiffASTAnalyzer.analyze("Logic.java", source);
        assertTrue(r.parseSucceeded());
        List<DiffASTResult.CFNode> cfs = r.controlFlowNodes();
        assertEquals(3, cfs.size());
        assertTrue(cfs.stream().anyMatch(n -> n.type().equals("IF")));
        assertTrue(cfs.stream().anyMatch(n -> n.type().equals("FOR")));
        assertTrue(cfs.stream().anyMatch(n -> n.type().equals("TRY_CATCH")));
    }

    @Test
    void parseFailureReturnsNotSucceeded() {
        String source = "not valid java {{{";
        DiffASTResult r = DiffASTAnalyzer.analyze("Bad.java", source);
        assertFalse(r.parseSucceeded());
        assertTrue(r.classes().isEmpty());
        assertTrue(r.methods().isEmpty());
    }

    @Test
    void emptyFileReturnsNotSucceeded() {
        DiffASTResult r = DiffASTAnalyzer.analyze("Empty.java", "");
        assertFalse(r.parseSucceeded());
    }

    @Test
    void interfaceAndEnum() {
        String source = """
            public interface Repository {
                void save(Entity e);
            }
            """;
        DiffASTResult r = DiffASTAnalyzer.analyze("Repository.java", source);
        assertTrue(r.parseSucceeded());
        assertEquals("interface", r.classes().get(0).type());
        assertEquals(1, r.methods().size());
    }

    @Test
    void packagePrivateVisibility() {
        String source = """
            class Helper {
                void doInternal() {}
            }
            """;
        DiffASTResult r = DiffASTAnalyzer.analyze("Helper.java", source);
        assertTrue(r.parseSucceeded());
        assertEquals("package-private", r.methods().get(0).visibility());
    }
}
```

- [ ] **Step 2: 跑单测确认失败**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn test -pl . -Dtest=DiffASTAnalyzerTest -DfailIfNoTests=false 2>&1 | Select-String "Tests run|FAIL|ERROR|BUILD"
```

Expected: BUILD FAILURE（类不存在）

- [ ] **Step 3: 实现 DiffASTAnalyzer**

```java
package com.codeguard.agent.ast;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.Modifier;
import com.github.javaparser.ast.NodeList;
import com.github.javaparser.ast.body.*;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.ObjectCreationExpr;
import com.github.javaparser.ast.stmt.*;
import com.github.javaparser.ast.type.ClassOrInterfaceType;
import com.github.javaparser.ast.visitor.VoidVisitorAdapter;

import java.util.ArrayList;
import java.util.List;

/**
 * 用 JavaParser 从单个 Java 源文件抽取完整的 AST 结构信息。
 * <p>
 * 纯函数——无状态、无副作用。解析失败返回 {@code parseSucceeded=false}，不抛异常。
 * 独立于 repomap Tag 体系——本类输出完整级 AST（可见性/注解/控制流/调用边），
 * 与 PageRank 建图目的的简化 Tag 模型关注点不同。
 */
public final class DiffASTAnalyzer {

    private DiffASTAnalyzer() {}

    /**
     * 解析单个 Java 源文件，返回结构化 AST 信息。
     *
     * @param filePath 相对仓库根的文件路径（仅用于填充结果，不读取文件）
     * @param source   文件源码内容
     * @return 解析结果；失败时 parseSucceeded=false，各列表为空
     */
    public static DiffASTResult analyze(String filePath, String source) {
        if (source == null || source.isBlank()) {
            return new DiffASTResult(filePath, false, List.of(), List.of(), List.of(), List.of());
        }
        CompilationUnit cu;
        try {
            ParseResult<CompilationUnit> result = new JavaParser().parse(source);
            if (!result.isSuccessful() || result.getResult().isEmpty()) {
                return new DiffASTResult(filePath, false, List.of(), List.of(), List.of(), List.of());
            }
            cu = result.getResult().get();
        } catch (Exception e) {
            return new DiffASTResult(filePath, false, List.of(), List.of(), List.of(), List.of());
        }

        List<DiffASTResult.ClassDef> classes = extractClasses(cu);
        List<DiffASTResult.MethodDef> methods = extractMethods(cu);
        List<DiffASTResult.CFNode> controlFlow = extractControlFlow(cu);
        List<DiffASTResult.CallEdgeDef> callEdges = extractCallEdges(cu);

        return new DiffASTResult(filePath, true, classes, methods, controlFlow, callEdges);
    }

    // ── ClassDef 提取 ──

    private static List<DiffASTResult.ClassDef> extractClasses(CompilationUnit cu) {
        List<DiffASTResult.ClassDef> result = new ArrayList<>();
        cu.findAll(ClassOrInterfaceDeclaration.class).forEach(decl -> {
            String type;
            if (decl.isInterface()) type = "interface";
            else if (decl.isEnumDeclaration()) type = "enum"; // ClassOrInterfaceDeclaration 不直接区分 enum
            // JavaParser 的 EnumDeclaration 是单独类型，这里从 ClassOrInterfaceDeclaration 只能取到 class/interface
            // 实际 enum 不会进 ClassOrInterfaceDeclaration.findAll，所以直接按 isInterface 判断
            else type = "class";

            String superClass = "";
            NodeList<ClassOrInterfaceType> extended = decl.getExtendedTypes();
            if (extended != null && extended.isNonEmpty()) {
                superClass = extended.get(0).getNameAsString();
            }
            List<String> interfaces = new ArrayList<>();
            NodeList<ClassOrInterfaceType> implemented = decl.getImplementedTypes();
            if (implemented != null) {
                implemented.forEach(t -> interfaces.add(t.getNameAsString()));
            }
            List<String> fields = new ArrayList<>();
            decl.getFields().forEach(f -> {
                String fieldType = f.getElementType().asString();
                f.getVariables().forEach(v -> fields.add(fieldType + " " + v.getNameAsString()));
            });
            int start = decl.getBegin().map(p -> p.line).orElse(-1);
            int end = decl.getEnd().map(p -> p.line).orElse(-1);
            result.add(new DiffASTResult.ClassDef(
                    decl.getNameAsString(), type, superClass, interfaces, fields, start, end));
        });
        // 也处理 EnumDeclaration（独立于 ClassOrInterfaceDeclaration）
        cu.findAll(com.github.javaparser.ast.body.EnumDeclaration.class).forEach(decl -> {
            List<String> interfaces = new ArrayList<>();
            NodeList<ClassOrInterfaceType> implemented = decl.getImplementedTypes();
            if (implemented != null) {
                implemented.forEach(t -> interfaces.add(t.getNameAsString()));
            }
            int start = decl.getBegin().map(p -> p.line).orElse(-1);
            int end = decl.getEnd().map(p -> p.line).orElse(-1);
            result.add(new DiffASTResult.ClassDef(
                    decl.getNameAsString(), "enum", "", interfaces, List.of(), start, end));
        });
        return result;
    }

    // ── MethodDef 提取 ──

    private static List<DiffASTResult.MethodDef> extractMethods(CompilationUnit cu) {
        List<DiffASTResult.MethodDef> result = new ArrayList<>();
        // 普通方法
        cu.findAll(MethodDeclaration.class).forEach(decl -> {
            result.add(buildMethodDef(decl.getNameAsString(), decl.getType().asString(),
                    decl.getParameters(), decl.getAccessSpecifier(), decl.getModifiers(),
                    decl.getAnnotations(), decl.getBegin().map(p -> p.line).orElse(-1),
                    decl.getEnd().map(p -> p.line).orElse(-1)));
        });
        // 构造器
        cu.findAll(ConstructorDeclaration.class).forEach(decl -> {
            result.add(buildMethodDef(decl.getNameAsString(), "",
                    decl.getParameters(), decl.getAccessSpecifier(), decl.getModifiers(),
                    decl.getAnnotations(), decl.getBegin().map(p -> p.line).orElse(-1),
                    decl.getEnd().map(p -> p.line).orElse(-1)));
        });
        return result;
    }

    private static DiffASTResult.MethodDef buildMethodDef(
            String name, String returnType,
            NodeList<Parameter> params,
            Modifier.AccessSpecifier accessSpec,
            NodeList<Modifier> modifiers,
            NodeList<com.github.javaparser.ast.expr.AnnotationExpr> annotations,
            int start, int end) {
        List<String> paramTypes = new ArrayList<>();
        List<String> paramNames = new ArrayList<>();
        for (Parameter p : params) {
            paramTypes.add(p.getType().asString());
            paramNames.add(p.getNameAsString());
        }
        String visibility = switch (accessSpec) {
            case PUBLIC -> "public";
            case PRIVATE -> "private";
            case PROTECTED -> "protected";
            case NONE -> "package-private";
        };
        List<String> modList = new ArrayList<>();
        for (Modifier m : modifiers) {
            String kw = m.getKeyword().asString();
            if (!kw.equals(visibility)) { // 可见性关键字也出现在 modifiers 中，跳过
                modList.add(kw);
            }
        }
        List<String> annList = new ArrayList<>();
        for (var ann : annotations) {
            String annName = ann.getNameAsString();
            annList.add("@" + annName);
        }
        return new DiffASTResult.MethodDef(
                name, returnType, paramTypes, paramNames, visibility, modList, annList, start, end);
    }

    // ── CFNode 提取 ──

    private static List<DiffASTResult.CFNode> extractControlFlow(CompilationUnit cu) {
        List<DiffASTResult.CFNode> result = new ArrayList<>();
        cu.findAll(IfStmt.class).forEach(n ->
                result.add(cfNode("IF", n, n.getCondition().toString())));
        cu.findAll(ForStmt.class).forEach(n ->
                result.add(cfNode("FOR", n, n.getCompare().map(Object::toString).orElse(""))));
        cu.findAll(ForEachStmt.class).forEach(n ->
                result.add(cfNode("FOR_EACH", n, n.getIterable().toString())));
        cu.findAll(WhileStmt.class).forEach(n ->
                result.add(cfNode("WHILE", n, n.getCondition().toString())));
        cu.findAll(DoStmt.class).forEach(n ->
                result.add(cfNode("DO_WHILE", n, n.getCondition().toString())));
        cu.findAll(TryStmt.class).forEach(n ->
                result.add(cfNode("TRY_CATCH", n, "")));
        cu.findAll(SwitchStmt.class).forEach(n ->
                result.add(cfNode("SWITCH", n, n.getSelector().toString())));
        cu.findAll(SynchronizedStmt.class).forEach(n ->
                result.add(cfNode("SYNCHRONIZED", n, n.getExpression().toString())));
        return result;
    }

    private static DiffASTResult.CFNode cfNode(String type, com.github.javaparser.ast.Node node, String condition) {
        int start = node.getBegin().map(p -> p.line).orElse(-1);
        int end = node.getEnd().map(p -> p.line).orElse(-1);
        String clipped = condition.length() > 60 ? condition.substring(0, 60) + "..." : condition;
        return new DiffASTResult.CFNode(type, start, end, clipped);
    }

    // ── CallEdgeDef 提取 ──

    private static List<DiffASTResult.CallEdgeDef> extractCallEdges(CompilationUnit cu) {
        List<DiffASTResult.CallEdgeDef> result = new ArrayList<>();
        cu.findAll(MethodCallExpr.class).forEach(call -> {
            String caller = findEnclosingMethod(call);
            String callee = call.getNameAsString();
            String scope = call.getScope().map(Expression::toString).orElse("");
            if (caller.isEmpty()) return;
            int line = call.getBegin().map(p -> p.line).orElse(-1);
            result.add(new DiffASTResult.CallEdgeDef(caller, callee, scope, line));
        });
        return result;
    }

    /** 从 AST 节点向上查找所在方法/构造器名。 */
    private static String findEnclosingMethod(com.github.javaparser.ast.Node node) {
        com.github.javaparser.ast.Node current = node.getParentNode().orElse(null);
        while (current != null) {
            if (current instanceof MethodDeclaration m) return m.getNameAsString();
            if (current instanceof ConstructorDeclaration c) return c.getNameAsString();
            if (current instanceof CompilationUnit) break;
            current = current.getParentNode().orElse(null);
        }
        return "";
    }
}
```

- [ ] **Step 4: 跑单测确认通过**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn test -pl . -Dtest=DiffASTAnalyzerTest 2>&1 | Select-String "Tests run|BUILD"
```

Expected: Tests run: 8, Failures: 0, BUILD SUCCESS

- [ ] **Step 5: Commit**

```bash
git add services/gateway/src/main/java/com/codeguard/agent/ast/DiffASTAnalyzer.java
git add services/gateway/src/test/java/com/codeguard/agent/ast/DiffASTAnalyzerTest.java
git commit -m "feat(ast): 新增 DiffASTAnalyzer——JavaParser AST 提取，含 ClassDef/MethodDef/CFNode/CallEdgeDef"
```

---

### Task 3: ASTContextFormatter 格式化 + 两级裁剪

**Files:**
- Create: `services/gateway/src/main/java/com/codeguard/agent/ast/ASTContextFormatter.java`
- Create: `services/gateway/src/test/java/com/codeguard/agent/ast/ASTContextFormatterTest.java`

- [ ] **Step 1: 写失败单测**

```java
package com.codeguard.agent.ast;

import org.junit.jupiter.api.Test;
import java.util.List;
import static org.junit.jupiter.api.Assertions.*;

class ASTContextFormatterTest {

    private static final int CHARS_PER_TOKEN = 4;

    @Test
    void formatsSimpleClassTier0() {
        DiffASTResult r = new DiffASTResult("Service.java", true,
                List.of(new DiffASTResult.ClassDef("Service", "class", "", List.of(),
                        List.of("Repo repo"), 1, 20)),
                List.of(new DiffASTResult.MethodDef("run", "void",
                        List.of("String"), List.of("input"),
                        "public", List.of(), List.of("@Override"), 5, 10)),
                List.of(new DiffASTResult.CFNode("IF", 6, 8, "input == null")),
                List.of(new DiffASTResult.CallEdgeDef("run", "repo.save", "repo", 7)));
        // 小文件，diff token 很大 → Tier 0
        String text = ASTContextFormatter.format(r, fakeDiff("Service.java", 5), 5000);
        assertTrue(text.contains("AST for:"));
        assertTrue(text.contains("class: Service"));
        assertTrue(text.contains("@Override public void run"));
        assertTrue(text.contains("-> calls: repo.save"));
        assertTrue(text.contains("IF [L6-L8] input == null"));
    }

    @Test
    void triggersTier1WhenOverBudget() {
        // 构造大文件：50 个方法，每个名字都长 → 超预算
        List<DiffASTResult.MethodDef> methods = new java.util.ArrayList<>();
        for (int i = 0; i < 50; i++) {
            methods.add(new DiffASTResult.MethodDef("veryLongMethodName" + i, "VeryLongReturnType",
                    List.of("VeryLongParamType"), List.of("veryLongParamName"),
                    "public", List.of("static"), List.of("@Override"), i * 2 + 2, i * 2 + 3));
        }
        DiffASTResult r = new DiffASTResult("Big.java", true,
                List.of(new DiffASTResult.ClassDef("Big", "class", "", List.of(), List.of(), 1, 100)),
                methods,
                List.of(),
                List.of());
        // diff token 少 → 预算紧 → Tier 1
        String text = ASTContextFormatter.format(r, fakeDiff("Big.java", 42), 200);
        // Tier 1 应该保留 diff 行范围内的方法（42 在 method index 21 的范围内）
        assertTrue(text.contains("AST for:"));
        assertTrue(text.contains("class: Big"));
        assertTrue(text.contains("Methods (changed)")); // Tier 1 标记
    }

    @Test
    void triggersTier2WhenStillOverBudget() {
        // 超多方法，极小 diff 范围
        List<DiffASTResult.MethodDef> methods = new java.util.ArrayList<>();
        for (int i = 0; i < 100; i++) {
            methods.add(new DiffASTResult.MethodDef("m" + i, "void", List.of(), List.of(),
                    "public", List.of(), List.of(), i * 2 + 2, i * 2 + 3));
        }
        DiffASTResult r = new DiffASTResult("Huge.java", true,
                List.of(new DiffASTResult.ClassDef("Huge", "class", "", List.of(), List.of(), 1, 200)),
                methods, List.of(), List.of());
        // 预算极小，只有 1 个方法在 diff 行范围内 → Tier 1 后仍超 → Tier 2
        String text = ASTContextFormatter.format(r, fakeDiff("Huge.java", 5), 60);
        assertTrue(text.contains("AST for:"));
        assertTrue(text.contains("class: Huge"));
        assertTrue(text.contains("Methods: "));  // Tier 2 极简模式，方法签名列表
    }

    @Test
    void methodsSortedWithChangedOnesFirst() {
        DiffASTResult r = new DiffASTResult("Svc.java", true,
                List.of(new DiffASTResult.ClassDef("Svc", "class", "", List.of(), List.of(), 1, 30)),
                List.of(
                        new DiffASTResult.MethodDef("unchanged", "void", List.of(), List.of(),
                                "public", List.of(), List.of(), 2, 4),
                        new DiffASTResult.MethodDef("changed", "void", List.of(), List.of(),
                                "public", List.of(), List.of(), 10, 12)
                ),
                List.of(), List.of());
        // diff 行 10 → changed 方法在范围内，应排在前面
        String text = ASTContextFormatter.format(r, fakeDiff("Svc.java", 10), 5000);
        int idxChanged = text.indexOf("changed");
        int idxUnchanged = text.indexOf("unchanged");
        assertTrue(idxChanged < idxUnchanged, "diff 范围内的方法应排在前面");
    }

    @Test
    void packagePrivateVisibilityOmitted() {
        DiffASTResult r = new DiffASTResult("Pkg.java", true,
                List.of(new DiffASTResult.ClassDef("Pkg", "class", "", List.of(), List.of(), 1, 5)),
                List.of(new DiffASTResult.MethodDef("doIt", "void", List.of(), List.of(),
                        "package-private", List.of(), List.of(), 2, 4)),
                List.of(), List.of());
        String text = ASTContextFormatter.format(r, fakeDiff("Pkg.java", 2), 5000);
        assertFalse(text.contains("package-private"), "package-private 不应打印");
        assertTrue(text.contains("void doIt"));
    }

    @Test
    void controlFlowOnlyForChangedLines() {
        DiffASTResult r = new DiffASTResult("Ctrl.java", true,
                List.of(new DiffASTResult.ClassDef("Ctrl", "class", "", List.of(), List.of(), 1, 20)),
                List.of(new DiffASTResult.MethodDef("run", "void", List.of(), List.of(),
                        "public", List.of(), List.of(), 2, 20)),
                List.of(
                        new DiffASTResult.CFNode("IF", 3, 5, "x > 0"),       // 在 diff 行内
                        new DiffASTResult.CFNode("FOR", 15, 18, "i < 10")    // 不在 diff 行内
                ),
                List.of());
        // diff 行 4 → IF 在范围内，FOR 不在
        String text = ASTContextFormatter.format(r, fakeDiff("Ctrl.java", 4), 5000);
        assertTrue(text.contains("IF"));
        assertFalse(text.contains("FOR"), "不在 diff 行范围内的控制流不应展示");
    }

    @Test
    void returnsEmptyForParseFailure() {
        DiffASTResult r = new DiffASTResult("Bad.java", false, List.of(), List.of(), List.of(), List.of());
        String text = ASTContextFormatter.format(r, "", 1000);
        assertEquals("", text);
    }

    /** 构造最小 fake diff 文本，使特定行号被识别为变更行。 */
    private static String fakeDiff(String filePath, int changedLine) {
        return "diff --git a/" + filePath + " b/" + filePath + "\n"
                + "--- a/" + filePath + "\n"
                + "+++ b/" + filePath + "\n"
                + "@@ -" + (changedLine - 1) + ",1 +" + changedLine + ",1 @@\n"
                + "+changed line content\n";
    }
}
```

- [ ] **Step 2: 跑单测确认失败**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn test -pl . -Dtest=ASTContextFormatterTest -DfailIfNoTests=false 2>&1 | Select-String "Tests run|FAIL|ERROR|BUILD"
```

Expected: BUILD FAILURE

- [ ] **Step 3: 实现 ASTContextFormatter**

```java
package com.codeguard.agent.ast;

import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

/**
 * 将 {@link DiffASTResult} 格式化为 LLM 可读文本，受 token 预算约束。
 * <p>
 * 预算：max(20% × diff_tokens, 600 chars)，floor=50 chars。
 * 超预算时逐级裁剪：
 * <ol>
 *   <li>Tier 1: 只保留 diff 行范围内的方法 + 控制流</li>
 *   <li>Tier 2: 极简模式——仅类名 + 方法签名列表</li>
 * </ol>
 * Token 估算用廉价近似：chars / 4（与 {@code RepoMapRenderer.CHARS_PER_TOKEN} 一致）。
 */
public final class ASTContextFormatter {

    private static final double MAX_BUDGET_FRACTION = 0.20;
    private static final int ABSOLUTE_MAX_CHARS = 600;
    private static final int CHARS_PER_TOKEN = 4;
    private static final int FLOOR_CHARS = 50;

    // Hunk header: @@ -old_start,old_count +new_start,new_count @@
    private static final Pattern HUNK_PATTERN =
            Pattern.compile("^@@\\s+-\\d+(?:,\\d+)?\\s+\\+(\\d+)(?:,(\\d+))?\\s+@@");

    private ASTContextFormatter() {}

    /**
     * @param result     AST 分析结果
     * @param diffText   原始 diff 文本（用于提取变更行号）
     * @param diffTokens 整个 diff 的 token 数（用于计算预算）
     * @return 格式化文本；解析失败返回 ""
     */
    public static String format(DiffASTResult result, String diffText, int diffTokens) {
        if (result == null || !result.parseSucceeded() || result.classes().isEmpty()) {
            return "";
        }
        int budget = Math.max(FLOOR_CHARS, Math.min((int) (diffTokens * MAX_BUDGET_FRACTION * CHARS_PER_TOKEN), ABSOLUTE_MAX_CHARS));
        Set<Integer> changedLines = extractChangedLines(diffText);

        String tier0 = renderTier0(result, changedLines);
        if (charEstimate(tier0) <= budget) {
            return tier0;
        }
        String tier1 = renderTier1(result, changedLines);
        if (charEstimate(tier1) <= budget) {
            return tier1;
        }
        return renderTier2(result);
    }

    // ── Tier 0: 全量 ──

    private static String renderTier0(DiffASTResult result, Set<Integer> changedLines) {
        StringBuilder sb = new StringBuilder();
        sb.append("AST for: ").append(result.filePath()).append("\n");
        appendClassInfo(result, sb);
        appendMethods(result, changedLines, sb, false);
        appendControlFlow(result, changedLines, sb);
        return sb.toString();
    }

    // ── Tier 1: Diff-scoped ──

    private static String renderTier1(DiffASTResult result, Set<Integer> changedLines) {
        StringBuilder sb = new StringBuilder();
        sb.append("AST for: ").append(result.filePath()).append("\n");
        appendClassInfo(result, sb);
        appendMethods(result, changedLines, sb, true); // diff-scoped only
        appendControlFlow(result, changedLines, sb);
        return sb.toString();
    }

    // ── Tier 2: Minimal ──

    private static String renderTier2(DiffASTResult result) {
        StringBuilder sb = new StringBuilder();
        sb.append("AST for: ").append(result.filePath()).append("\n");
        for (var cls : result.classes()) {
            sb.append("  ").append(cls.type()).append(": ").append(cls.name()).append("\n");
        }
        if (!result.methods().isEmpty()) {
            sb.append("  Methods: ");
            sb.append(result.methods().stream()
                    .map(m -> m.name() + "(" + String.join(",", m.paramTypes()) + ")")
                    .collect(Collectors.joining(", ")));
            sb.append("\n");
        }
        return sb.toString();
    }

    // ── helpers ──

    private static void appendClassInfo(DiffASTResult result, StringBuilder sb) {
        for (var cls : result.classes()) {
            sb.append("  ").append(cls.type()).append(": ").append(cls.name());
            if (!cls.superClass().isEmpty()) {
                sb.append(" extends ").append(cls.superClass());
            }
            if (!cls.interfaces().isEmpty()) {
                sb.append(" implements ").append(String.join(", ", cls.interfaces()));
            }
            sb.append("\n");
            if (!cls.fields().isEmpty()) {
                sb.append("    Fields: ").append(String.join(", ", cls.fields())).append("\n");
            }
        }
    }

    private static void appendMethods(DiffASTResult result, Set<Integer> changedLines,
                                      StringBuilder sb, boolean scoped) {
        if (result.methods().isEmpty()) return;

        Map<String, List<String>> callsByMethod = new HashMap<>();
        for (var edge : result.callEdges()) {
            callsByMethod.computeIfAbsent(edge.callerMethod(), k -> new ArrayList<>())
                    .add(edge.calleeMethod());
        }

        // 排序：diff 范围内优先，然后按行号
        List<DiffASTResult.MethodDef> sorted = new ArrayList<>(result.methods());
        sorted.sort((a, b) -> {
            boolean aOver = overlaps(a, changedLines);
            boolean bOver = overlaps(b, changedLines);
            if (aOver != bOver) return aOver ? -1 : 1;
            return Integer.compare(a.startLine(), b.startLine());
        });

        String label = scoped ? "  Methods (changed):\n" : "  Methods:\n";
        sb.append(label);

        for (var m : sorted) {
            if (scoped && !overlaps(m, changedLines)) continue;
            sb.append("    ");
            // 可见性（跳过 package-private）
            if (!m.visibility().isEmpty() && !"package-private".equals(m.visibility())) {
                sb.append(m.visibility()).append(" ");
            }
            // 注解
            for (String ann : m.annotations()) {
                sb.append(ann).append(" ");
            }
            // 返回类型 + 方法签名
            if (!m.returnType().isEmpty()) {
                sb.append(m.returnType()).append(" ");
            }
            sb.append(m.name()).append("(");
            List<String> paramDescs = new ArrayList<>();
            for (int i = 0; i < m.paramTypes().size(); i++) {
                String pType = m.paramTypes().get(i);
                String pName = i < m.paramNames().size() ? m.paramNames().get(i) : "";
                paramDescs.add(pType + (pName.isEmpty() ? "" : " " + pName));
            }
            sb.append(String.join(", ", paramDescs));
            sb.append(") [L").append(m.startLine()).append("-L").append(m.endLine()).append("]");

            // 调用边
            List<String> callees = callsByMethod.get(m.name());
            if (callees != null && !callees.isEmpty()) {
                String uniqueCallees = callees.stream().distinct().collect(Collectors.joining(", "));
                sb.append(" -> calls: ").append(uniqueCallees);
            }
            sb.append("\n");
        }
    }

    private static void appendControlFlow(DiffASTResult result, Set<Integer> changedLines, StringBuilder sb) {
        List<DiffASTResult.CFNode> relevant = result.controlFlowNodes().stream()
                .filter(n -> overlaps(n, changedLines))
                .toList();
        if (relevant.isEmpty()) return;
        sb.append("  Control Flow:\n");
        for (var n : relevant) {
            sb.append("    ").append(n.type())
                    .append(" [L").append(n.startLine()).append("-L").append(n.endLine()).append("]");
            if (!n.condition().isEmpty()) {
                sb.append(" ").append(n.condition());
            }
            sb.append("\n");
        }
    }

    // ── diff 行号解析 ──

    static Set<Integer> extractChangedLines(String diffText) {
        Set<Integer> lines = new HashSet<>();
        if (diffText == null || diffText.isEmpty()) return lines;
        int currentLine = -1;
        int remaining = 0;
        for (String line : diffText.split("\n")) {
            Matcher m = HUNK_PATTERN.matcher(line);
            if (m.find()) {
                currentLine = Integer.parseInt(m.group(1));
                remaining = m.group(2) != null ? Integer.parseInt(m.group(2)) : 1;
                continue;
            }
            if (currentLine > 0 && remaining > 0) {
                if (line.startsWith("+") && !line.startsWith("++")) {
                    lines.add(currentLine);
                    currentLine++;
                    remaining--;
                } else if (!line.startsWith("-") && !line.startsWith("\\")) {
                    currentLine++;
                    remaining--;
                }
            }
        }
        return lines;
    }

    private static boolean overlaps(DiffASTResult.MethodDef m, Set<Integer> changedLines) {
        if (changedLines.isEmpty()) return true;
        if (m.startLine() <= 0) return false;
        return changedLines.stream().anyMatch(l -> l >= m.startLine() && l <= m.endLine());
    }

    private static boolean overlaps(DiffASTResult.CFNode n, Set<Integer> changedLines) {
        if (changedLines.isEmpty()) return true;
        if (n.startLine() <= 0) return false;
        return changedLines.stream().anyMatch(l -> l >= n.startLine() && l <= n.endLine());
    }

    /** 字符数估算（token 预算的廉价近似）。 */
    private static int charEstimate(String text) {
        return text == null ? 0 : text.length();
    }
}
```

- [ ] **Step 4: 跑单测确认通过**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn test -pl . -Dtest=ASTContextFormatterTest 2>&1 | Select-String "Tests run|BUILD"
```

Expected: Tests run: 7, Failures: 0, BUILD SUCCESS

- [ ] **Step 5: 修正可见性解析——access specifier 和 modifier 关键字区分**

在 `DiffASTAnalyzer` 和 `ASTContextFormatter` 的两个单测都通过后，跑全量单测确认没有回归：

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn test 2>&1 | Select-String "Tests run|Failures|BUILD"
```

Expected: BUILD SUCCESS, 0 Failures

- [ ] **Step 6: Commit**

```bash
git add services/gateway/src/main/java/com/codeguard/agent/ast/ASTContextFormatter.java
git add services/gateway/src/test/java/com/codeguard/agent/ast/ASTContextFormatterTest.java
git commit -m "feat(ast): 新增 ASTContextFormatter——LLM 文本格式化 + 两级裁剪(Tier0→1→2)"
```

---

### Task 4: GetDiffASTTool 工具实现 + 注册

**Files:**
- Create: `services/gateway/src/main/java/com/codeguard/agent/tools/GetDiffASTTool.java`
- Create: `services/gateway/src/test/java/com/codeguard/agent/tools/GetDiffASTToolTest.java`
- Modify: `services/gateway/src/main/java/com/codeguard/toolserver/ToolSessionManager.java:55-60`

- [ ] **Step 1: 写失败单测**

```java
package com.codeguard.agent.tools;

import com.codeguard.agent.ast.DiffASTResult;
import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.ToolResult;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.*;

class GetDiffASTToolTest {

    @Test
    void outputsAstForJavaFiles(@TempDir Path repo) throws Exception {
        Path f = repo.resolve("Service.java");
        String code = """
            public class Service {
                public void run(String input) {
                    if (input != null) {
                        helper.process(input);
                    }
                }
            }
            """;
        Files.writeString(f, code, StandardCharsets.UTF_8);

        FileAccessSandbox sandbox = new FileAccessSandbox(repo, Set.of("Service.java"));
        AgentContext ctx = new AgentContext(repo, Set.of("Service.java"));
        String diffText = "diff --git a/Service.java b/Service.java\n"
                + "--- a/Service.java\n"
                + "+++ b/Service.java\n"
                + "@@ -2,0 +3,3 @@\n"
                + "+    if (input != null) {\n"
                + "+        helper.process(input);\n"
                + "+    }\n";
        ToolResult r = new GetDiffASTTool(sandbox).execute(diffText, ctx);
        assertTrue(r.isSuccess());
        String output = r.getResult();
        assertTrue(output.contains("AST for: Service.java"));
        assertTrue(output.contains("class: Service"));
        assertTrue(output.contains("run"));
    }

    @Test
    void skipsNonJavaFiles(@TempDir Path repo) throws Exception {
        Path f = repo.resolve("config.xml");
        Files.writeString(f, "<config/>", StandardCharsets.UTF_8);

        FileAccessSandbox sandbox = new FileAccessSandbox(repo, Set.of("config.xml"));
        AgentContext ctx = new AgentContext(repo, Set.of("config.xml"));
        ToolResult r = new GetDiffASTTool(sandbox).execute("diff text", ctx);
        assertTrue(r.isSuccess());
        assertTrue(r.getResult().contains("无可解析的 Java AST 上下文"));
    }

    @Test
    void handlesParseFailureGracefully(@TempDir Path repo) throws Exception {
        // 一个可解析，一个不可解析
        Path good = repo.resolve("Good.java");
        Files.writeString(good, "class Good {}", StandardCharsets.UTF_8);
        Path bad = repo.resolve("Bad.java");
        Files.writeString(bad, "not java {{{", StandardCharsets.UTF_8);

        FileAccessSandbox sandbox = new FileAccessSandbox(repo, Set.of("Good.java", "Bad.java"));
        AgentContext ctx = new AgentContext(repo, Set.of("Good.java", "Bad.java"));
        ToolResult r = new GetDiffASTTool(sandbox).execute("diff text", ctx);
        assertTrue(r.isSuccess());
        String output = r.getResult();
        assertTrue(output.contains("AST for: Good.java"));
        // Bad.java 解析失败，不出现 AST for
        assertFalse(output.contains("AST for: Bad.java"));
    }

    @Test
    void emptyAllowedFiles(@TempDir Path repo) {
        FileAccessSandbox sandbox = new FileAccessSandbox(repo, Set.of());
        AgentContext ctx = new AgentContext(repo, Set.of());
        ToolResult r = new GetDiffASTTool(sandbox).execute("", ctx);
        assertTrue(r.isSuccess());
        assertTrue(r.getResult().contains("无可解析的 Java AST 上下文"));
    }
}
```

- [ ] **Step 2: 跑单测确认失败**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn test -pl . -Dtest=GetDiffASTToolTest -DfailIfNoTests=false 2>&1 | Select-String "Tests run|FAIL|BUILD"
```

Expected: BUILD FAILURE

- [ ] **Step 3: 实现 GetDiffASTTool**

```java
package com.codeguard.agent.tools;

import com.codeguard.agent.ast.ASTContextFormatter;
import com.codeguard.agent.ast.DiffASTAnalyzer;
import com.codeguard.agent.ast.DiffASTResult;
import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * 获取本次 diff 涉及文件的 AST 结构信息（供 context_provider 使用）。
 * <p>
 * 遍历会话的 allowedFiles 中所有 .java 文件，提取完整 AST（类/方法/控制流/调用边），
 * 按文件独立预算格式化后返回。解析失败的单文件跳过，不影响其余。
 * <p>
 * 这是 context_provider 专属工具——Agent 不应直接调用它，
 * AST 结果通过 ContextBundle 共享给所有发现者。
 */
public final class GetDiffASTTool implements AgentTool {

    private final FileAccessSandbox sandbox;

    public GetDiffASTTool(FileAccessSandbox sandbox) {
        this.sandbox = sandbox;
    }

    @Override
    public String name() {
        return "get_diff_ast";
    }

    @Override
    public String description() {
        return "获取本次 diff 涉及文件的完整 AST 结构信息"
                + "（类层次/方法签名+可见性+注解+调用边/控制流节点），"
                + "用于在审查前建立共享的代码结构上下文。无需入参——自动扫描会话的允许文件。";
    }

    @Override
    public ToolResult execute(String input, AgentContext context) {
        // input 为原始 diff 文本（通过 query 字段传入）
        String diffText = input == null ? "" : input;
        var allowedFiles = context.getAllowedFiles();
        if (allowedFiles.isEmpty()) {
            return ToolResult.ok("(无可解析的 Java AST 上下文)");
        }

        int diffTokens = Math.max(1, diffText.length() / 4);
        StringBuilder all = new StringBuilder();
        int parsed = 0;
        int failed = 0;

        for (String relPath : allowedFiles) {
            if (!relPath.endsWith(".java")) continue;
            Path fullPath;
            try {
                fullPath = sandbox.resolveWithinRepo(relPath);
            } catch (SecurityException e) {
                failed++;
                continue;
            }
            if (!Files.isRegularFile(fullPath)) {
                failed++;
                continue;
            }
            String source;
            try {
                source = Files.readString(fullPath, StandardCharsets.UTF_8);
            } catch (IOException e) {
                failed++;
                continue;
            }

            DiffASTResult result = DiffASTAnalyzer.analyze(relPath, source);
            if (!result.parseSucceeded() || result.classes().isEmpty()) {
                failed++;
                continue;
            }

            String formatted = ASTContextFormatter.format(result, diffText, diffTokens);
            if (!formatted.isEmpty()) {
                if (!all.isEmpty()) {
                    all.append("\n");
                }
                all.append(formatted);
                parsed++;
            }
        }

        if (all.isEmpty()) {
            return ToolResult.ok("(无可解析的 Java AST 上下文)");
        }
        return ToolResult.ok(all.toString());
    }
}
```

- [ ] **Step 4: 在 ToolSessionManager 中注册**

编辑 `services/gateway/src/main/java/com/codeguard/toolserver/ToolSessionManager.java`，在 `create()` 中：

```java
// 在现有注册之后追加：
this.registry.register(new GetDiffASTTool(sandbox));
```

并删除第 60 行注释：`// get_repo_map 已断开调用—— Java 实现(GetRepoMapTool / repomap/)保留不删备参考`

- [ ] **Step 5: 跑单测确认通过**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn test -pl . -Dtest=GetDiffASTToolTest 2>&1 | Select-String "Tests run|BUILD"
```

Expected: Tests run: 4, Failures: 0, BUILD SUCCESS

- [ ] **Step 6: 跑全量 Java 单测确认无回归**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn test 2>&1 | Select-String "Tests run|Failures|BUILD"
```

Expected: BUILD SUCCESS, 0 Failures

- [ ] **Step 7: Commit**

```bash
git add services/gateway/src/main/java/com/codeguard/agent/tools/GetDiffASTTool.java
git add services/gateway/src/test/java/com/codeguard/agent/tools/GetDiffASTToolTest.java
git add services/gateway/src/main/java/com/codeguard/toolserver/ToolSessionManager.java
git commit -m "feat(ast): 新增 GetDiffASTTool + 注册到 ToolSessionManager"
```

---

### Task 5: Python 侧接入

**Files:**
- Create: `services/agent/tests/test_context_provider_ast.py`
- Modify: `services/agent/src/codeguard_agent/tools/tool_client.py:73-75`
- Modify: `services/agent/src/codeguard_agent/pipeline/stages/context_provider.py:50-83`

- [ ] **Step 1: 在 tool_client.py 新增 get_diff_ast 方法**

在 `services/agent/src/codeguard_agent/tools/tool_client.py` 的 `ToolClient` 类中，`find_sensitive_apis` 方法之后追加：

```python
def get_diff_ast(self, diff_text: str) -> ToolResponse:
    """获取 diff 涉及文件的 AST 结构信息（context_provider 专属）。"""
    return self._post_tool("get_diff_ast", {"query": diff_text})
```

- [ ] **Step 2: 修改 context_provider.py**

编辑 `services/agent/src/codeguard_agent/pipeline/stages/context_provider.py`，在 `find_sensitive_apis` 块之后、`ContextBundle(...)` 构建之前，追加：

```python
        # 4. AST 结构提取（diff 内文件）
        if context.tool_client is not None:
            resp = context.tool_client.get_diff_ast(context.diff_text)
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

并在文件顶部（`ContextProviderStage` 类之前）追加辅助函数：

```python
import re

def _split_ast_blocks(text: str) -> list[str]:
    """将多文件 AST 文本按 'AST for:' 分隔符拆分为单文件块。"""
    if not text.strip():
        return []
    blocks = re.split(r'\n(?=AST for:)', text.strip())
    return [b.strip() for b in blocks if b.strip()]
```

- [ ] **Step 3: 写 Python 单测**

```python
"""context_provider AST 接入单测。"""

from codeguard_agent.models.council import ContextBundle, ContextFact
from codeguard_agent.pipeline.stages.context_provider import _split_ast_blocks


def test_split_ast_blocks_single_file():
    text = "AST for: Foo.java\n  class: Foo\n    Methods:\n      void bar()"
    blocks = _split_ast_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].startswith("AST for: Foo.java")


def test_split_ast_blocks_multiple_files():
    text = "AST for: Foo.java\n  class: Foo\n\nAST for: Bar.java\n  class: Bar"
    blocks = _split_ast_blocks(text)
    assert len(blocks) == 2
    assert "Foo.java" in blocks[0]
    assert "Bar.java" in blocks[1]


def test_split_ast_blocks_empty():
    assert _split_ast_blocks("") == []
    assert _split_ast_blocks("   ") == []


def test_split_ast_blocks_no_header():
    """无 AST for 头时返回整个文本作为一个块。"""
    text = "some other content"
    blocks = _split_ast_blocks(text)
    assert len(blocks) == 1
    assert blocks[0] == text
```

- [ ] **Step 4: 跑 Python 单测**

```powershell
cd E:\java_develop\my_project\Codeguard\services\agent
conda run -n codeguard --no-capture-output python -m pytest tests/test_context_provider_ast.py -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add services/agent/src/codeguard_agent/tools/tool_client.py
git add services/agent/src/codeguard_agent/pipeline/stages/context_provider.py
git add services/agent/tests/test_context_provider_ast.py
git commit -m "feat(ast): context_provider 接入 get_diff_ast——diff 文件 AST 注入 ContextBundle"
```

---

### Task 6: 死代码清理——repomap 迁移

**Files:**
- Move: `agent/tools/GetRepoMapTool.java` → `legacy/tools/`
- Move: `agent/repomap/*.java` (9 files) → `legacy/repomap/`
- Move: `src/test/.../GetRepoMapToolTest.java` → `legacy/test/`
- Move: `src/test/.../repomap/*Test.java` (4 files) → `legacy/test/repomap/`
- Modify: `FindCallersTool.java` (删除 javadoc link)
- Modify: `FileAccessSandbox.java` (删除过时注释)

- [ ] **Step 1: 创建 legacy 目录结构**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
New-Item -ItemType Directory -Force -Path legacy/tools, legacy/repomap, legacy/test/repomap
```

- [ ] **Step 2: 移动主代码**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway

# GetRepoMapTool
Move-Item src/main/java/com/codeguard/agent/tools/GetRepoMapTool.java legacy/tools/GetRepoMapTool.java

# repomap 全部 9 个文件
$repomapDir = "src/main/java/com/codeguard/agent/repomap"
Get-ChildItem $repomapDir -Name | ForEach-Object {
    Move-Item "$repomapDir/$_" "legacy/repomap/$_"
}
# 删除空目录
Remove-Item $repomapDir
```

- [ ] **Step 3: 移动测试代码**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway

# GetRepoMapToolTest
$testToolsDir = "src/test/java/com/codeguard/agent/tools"
if (Test-Path "$testToolsDir/GetRepoMapToolTest.java") {
    Move-Item "$testToolsDir/GetRepoMapToolTest.java" legacy/test/GetRepoMapToolTest.java
}

# repomap tests
$testRepomapDir = "src/test/java/com/codeguard/agent/repomap"
if (Test-Path $testRepomapDir) {
    Get-ChildItem $testRepomapDir -Name | ForEach-Object {
        Move-Item "$testRepomapDir/$_" "legacy/test/repomap/$_"
    }
    Remove-Item $testRepomapDir
}
```

- [ ] **Step 4: 清理遗留注释引用**

编辑 `FindCallersTool.java:27`，将：
```java
 * 复用 {@link com.codeguard.agent.repomap.JavaTagExtractor} 的 ref 抽取思路:
```
改为：
```java
 * 用 JavaParser 的 MethodCallExpr 做简单名匹配:
```

编辑 `FileAccessSandbox.java:15-19`，删除以下过时注释块：
```java
 * 护栏放宽说明(design.md D5):自 get_repo_map 落地后,审查员需要读 diff 之外、由地图指向的
 * 定义文件,故授权从"仅本次 diff 改动文件集合"放宽为"repo 根内 + 源码扩展名白名单"。仍保留
 * 路径穿越防御与(由 {@link GetFileContentTool} 施加的)大小上限,并以"只读源码类型"排除二进制/
 * 配置/密钥文件 —— 放宽边界,但不等于任意读。{@code allowedFiles}(diff 改动集合)保留下来,
 * 作为 get_repo_map 的相关性种子,不再用于读授权。
```
替换为精简版：
```java
 * 护栏已放宽:审查员可读取 repo 根内任意源码/配置/密钥类型文件(受扩展名白名单约束),
 * 不再限制为仅 diff 文件。仍保留路径穿越防御与(由 {@link GetFileContentTool} 施加的)大小上限。
```

编辑 `FileAccessSandbox.java:55`，注释从：
```java
    /** 该相对路径是否落在本次 diff 的允许文件集合内(保留供 get_repo_map 种子等用途,不再用于读授权)。 */
```
改为：
```java
    /** 该相对路径是否落在本次 diff 的允许文件集合内。 */
```

- [ ] **Step 5: 跑全量 Java 单测确认无回归**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn test 2>&1 | Select-String "Tests run|Failures|BUILD"
```

Expected: BUILD SUCCESS, 0 Failures（确认移动的文件已不在 classpath 中，无编译错误）

- [ ] **Step 6: Commit**

```bash
git add -A services/gateway/legacy/
git add -u services/gateway/src/
git commit -m "chore(legacy): 迁移 repomap 死代码到 legacy/——get_repo_map 已下线，清理注释引用"
```

---

### Task 7: 全量单测 + ADR

- [ ] **Step 1: 跑 Python 全量单测**

```powershell
cd E:\java_develop\my_project\Codeguard\services\agent
conda run -n codeguard --no-capture-output python -m pytest tests/ -q
```

Expected: all passed

- [ ] **Step 2: 跑 Java 全量单测**

```powershell
cd E:\java_develop\my_project\Codeguard\services\gateway
mvn test 2>&1 | Select-String "Tests run|Failures|BUILD"
```

Expected: BUILD SUCCESS, 0 Failures

- [ ] **Step 3: 写 ADR 到 DECISIONS.md**

在 `E:\java_develop\my_project\Codeguard\DECISIONS.md` 末尾追加：

```markdown
---

## ADR-034: ContextProvider AST 富化（Layer 1）

**日期**: 2026-07-07
**状态**: 已实现

### 背景

三个发现者 Agent 各自通过 `get_file_content` 理解 diff 文件的代码结构，存在重复劳动和 token 浪费。Diffguard 的 `ASTEnricher` 模式证明：在审查前将 diff 文件的 AST 注入共享上下文，可以减少冗余工具调用。

### 决策

- **Layer 1（本次）**: `context_provider` 新增 `get_diff_ast` 调用，对每个 diff 内的 Java 文件提取完整 AST（类结构/方法签名+可见性+注解/控制流/调用边），以 `ContextFact(kind="ast_structure")` 存入 `ContextBundle`。预算按文件独立：`max(20% × diff_tokens, 600 chars)`，超预算两级裁剪。
- **独立 AST 体系**: 不复用 repomap 的 `Tag` 模型——新建 `com.codeguard.agent.ast` 包，用 JavaParser 直接解析，产出 `DiffASTResult`（含 `ClassDef`/`MethodDef`/`CFNode`/`CallEdgeDef`）。
- **Layer 2（后续）**: 跨文件探索工具（`get_method_definition`、`find_callers` 扩展 direction+depth）留待独立 change。

### 放弃的方案

- 复用 repomap `JavaTagExtractor`/`Tag`: Tag 模型只有 `(name, kind, line, signature)`，无法承载可见性/注解/控制流。
- 全局 ContextBundle 预算共享: 改为文件独立预算，避免大 diff 的 AST 被非 AST 事实挤占。

### 影响

- `repomap/` + `GetRepoMapTool` 迁移到 `services/gateway/legacy/`，不再编译。
- 新增 Java 4 文件（`ast/` 包 + `GetDiffASTTool`），Python 2 文件改动（`tool_client.py` + `context_provider.py`）。
- 后续参考: ADR-032 ReviewCouncil 编排、ADR-020 repo map 下线决策。
```

- [ ] **Step 4: 最终 Commit**

```bash
git add DECISIONS.md
git commit -m "docs: ADR-034 ContextProvider AST 富化决策记录"
```
