package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.expr.BinaryExpr;
import com.github.javaparser.ast.expr.ConditionalExpr;
import com.github.javaparser.ast.stmt.BlockStmt;
import com.github.javaparser.ast.stmt.CatchClause;
import com.github.javaparser.ast.stmt.DoStmt;
import com.github.javaparser.ast.stmt.ForEachStmt;
import com.github.javaparser.ast.stmt.ForStmt;
import com.github.javaparser.ast.stmt.IfStmt;
import com.github.javaparser.ast.stmt.SwitchEntry;
import com.github.javaparser.ast.stmt.WhileStmt;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

/**
 * 计算指定文件的代码度量(质量审查员专属工具)。
 * <p>
 * 用 JavaParser 遍历方法体 AST,统计:
 * <ul>
 *   <li>圈复杂度(CC)—— 分支节点(if/for/while/case/catch/&&/||/?:)数 + 1</li>
 *   <li>代码行数(LOC)—— 方法体行数,排除空行和纯注释行</li>
 *   <li>嵌套深度—— BlockStmt 的最大嵌套层数</li>
 *   <li>参数数量—— 方法声明的参数个数</li>
 * </ul>
 * 按 CC 阈值标注状态:≤5=✓, 6-10=⚠️, >10=🔴。
 */
public final class GetCodeMetricsTool implements AgentTool {

    private final FileAccessSandbox sandbox;

    public GetCodeMetricsTool(FileAccessSandbox sandbox) {
        this.sandbox = sandbox;
    }

    @Override
    public String name() {
        return "get_code_metrics";
    }

    @Override
    public String description() {
        return "计算指定文件的代码度量(圈复杂度、代码行数、嵌套深度、参数数量)。"
                + "当你面对一个新增大段实现或怀疑某文件过度复杂、需要精确数据做判断时调用。"
                + "入参:文件路径(如 src/main/java/com/example/OrderService.java)。";
    }

    @Override
    public ToolResult execute(String input, AgentContext context) {
        String filePath = input == null ? "" : input.trim();
        if (filePath.isEmpty()) {
            return ToolResult.error("缺少文件路径参数");
        }

        Path fullPath;
        try {
            fullPath = sandbox.resolveWithinRepo(filePath);
        } catch (SecurityException e) {
            return ToolResult.error("路径超出仓库范围: " + filePath);
        }
        if (!Files.isRegularFile(fullPath)) {
            return ToolResult.error("文件不存在: " + filePath);
        }
        if (!filePath.endsWith(".java")) {
            return ToolResult.error("非 Java 源文件,无法计算代码度量: " + filePath);
        }

        String source;
        try {
            source = Files.readString(fullPath, StandardCharsets.UTF_8);
        } catch (IOException e) {
            return ToolResult.error("读取文件失败: " + e.getMessage());
        }

        CompilationUnit cu;
        try {
            ParseResult<CompilationUnit> result = new JavaParser().parse(source);
            if (!result.isSuccessful() || result.getResult().isEmpty()) {
                return ToolResult.error("文件解析失败,可能包含语法错误");
            }
            cu = result.getResult().get();
        } catch (Exception e) {
            return ToolResult.error("文件解析异常: " + e.getMessage());
        }

        List<MethodDecl> methods = new ArrayList<>();
        cu.findAll(MethodDeclaration.class).forEach(decl -> {
            // 跳过抽象/接口方法(无方法体,无法度量)
            if (decl.getBody().isPresent()) {
                methods.add(new MethodDecl(decl));
            }
        });
        String fullSource = source; // 保存源码引用供提取代码行

        if (methods.isEmpty()) {
            return ToolResult.ok("# 代码度量\n"
                    + "文件: " + filePath + "\n\n"
                    + "该文件无可度量的方法(均为抽象/接口方法或空文件)。\n");
        }

        StringBuilder sb = new StringBuilder();
        sb.append("# 代码度量\n");
        sb.append(String.format("文件: `%s`\n\n", filePath));
        sb.append("| 方法 | CC | LOC | 嵌套 | 参数 | 状态 |\n");
        sb.append("|------|----|-----|------|------|------|\n");

        int totalMethods = methods.size();
        int totalLoc = 0;
        double totalCc = 0;

        for (MethodDecl md : methods) {
            int cc = computeCC(md.decl);
            int loc = computeLOC(md.decl, fullSource);
            int nesting = computeMaxNesting(md.decl);
            int params = md.decl.getParameters().size();
            String status = cc <= 5 ? "✓" : cc <= 10 ? "⚠️" : "🔴";

            totalLoc += loc;
            totalCc += cc;

            sb.append(String.format("| `%s` | %d | %d | %d | %d | %s |\n",
                    md.name(), cc, loc, nesting, params, status));
        }

        double avgCc = totalCc / totalMethods;
        sb.append(String.format("\n**文件总计**: %d 方法, %d LOC, 平均 CC=%.1f\n", totalMethods, totalLoc, avgCc));

        // 建议段
        List<String> warnings = new ArrayList<>();
        for (MethodDecl md : methods) {
            int cc = computeCC(md.decl);
            if (cc > 10) {
                warnings.add(String.format("`%s` 圈复杂度 %d 过高(>10), 建议拆分", md.name(), cc));
            }
        }
        if (!warnings.isEmpty()) {
            sb.append("\n### 建议\n");
            for (String w : warnings) {
                sb.append("- ").append(w).append("\n");
            }
        }

        return ToolResult.ok(sb.toString());
    }

    // ---- 度量计算 ----

    /** 圈复杂度:统计方法体内的分支节点 + 1 */
    private int computeCC(MethodDeclaration decl) {
        int branches = 1; // base

        // if / else-if
        branches += decl.findAll(IfStmt.class).size();
        // for / for-each
        branches += decl.findAll(ForStmt.class).size();
        branches += decl.findAll(ForEachStmt.class).size();
        // while
        branches += decl.findAll(WhileStmt.class).size();
        // do-while
        branches += decl.findAll(DoStmt.class).size();
        // case + default (SwitchEntry)
        branches += decl.findAll(SwitchEntry.class).size();
        // catch
        branches += decl.findAll(CatchClause.class).size();
        // && and || (BinaryExpr)
        branches += (int) decl.findAll(BinaryExpr.class).stream()
                .filter(e -> e.getOperator() == BinaryExpr.Operator.AND
                        || e.getOperator() == BinaryExpr.Operator.OR)
                .count();
        // ?: (ConditionalExpr / ternary)
        branches += decl.findAll(ConditionalExpr.class).size();

        return branches;
    }

    /** LOC:方法体有效行数(不含空行和纯注释行) */
    private int computeLOC(MethodDeclaration decl, String source) {
        Optional<BlockStmt> bodyOpt = decl.getBody();
        if (bodyOpt.isEmpty()) {
            return 0; // abstract / interface method
        }
        BlockStmt body = bodyOpt.get();
        int startLine = body.getBegin().map(p -> p.line).orElse(-1);
        int endLine = body.getEnd().map(p -> p.line).orElse(-1);
        if (startLine < 0 || endLine < 0 || endLine < startLine) {
            return 0;
        }

        String[] allLines = source.split("\n", -1);
        int count = 0;
        for (int i = startLine - 1; i < endLine && i < allLines.length; i++) {
            String line = allLines[i].trim();
            if (line.isEmpty()) continue;
            if (line.startsWith("//")) continue;
            if (line.startsWith("/*") || line.startsWith("*")) continue;
            count++;
        }
        return count;
    }

    /** 嵌套深度:BlockStmt 的最大嵌套层级 */
    private int computeMaxNesting(MethodDeclaration decl) {
        Optional<BlockStmt> bodyOpt = decl.getBody();
        if (bodyOpt.isEmpty()) {
            return 0;
        }
        return maxBlockDepth(bodyOpt.get(), 0);
    }

    private int maxBlockDepth(com.github.javaparser.ast.Node node, int currentDepth) {
        if (!(node instanceof BlockStmt)) {
            if (node == null) return currentDepth;
            // Children of non-BlockStmt don't increase depth
            int maxDepth = currentDepth;
            for (com.github.javaparser.ast.Node child : node.getChildNodes()) {
                maxDepth = Math.max(maxDepth, maxBlockDepth(child, currentDepth));
            }
            return maxDepth;
        }
        // We're in a BlockStmt — depth increases
        int newDepth = currentDepth + 1;
        int maxDepth = newDepth;
        for (com.github.javaparser.ast.Node child : node.getChildNodes()) {
            // Don't double-count nested BlockStmts - the child BlockStmt will add its own depth
            maxDepth = Math.max(maxDepth, maxBlockDepth(child, newDepth));
        }
        return maxDepth;
    }

    // ---- helper ----

    /**
     * 方法摘要:名称 + 参数签名的简短形式。
     */
    private record MethodDecl(MethodDeclaration decl) {
        String name() {
            String params = decl.getParameters().stream()
                    .map(p -> p.getType().asString())
                    .reduce((a, b) -> a + ", " + b)
                    .orElse("");
            return decl.getNameAsString() + "(" + params + ")";
        }
    }
}
