package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.expr.MethodCallExpr;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

/**
 * 查询指定方法在仓库内的所有直接调用方(逻辑审查员专属工具)。
 * <p>
 * 输入格式:"文件路径#方法名"(如 "src/main/java/com/example/OrderService.java#calculatePrice")。
 * 扫描仓库内所有 Java 源文件的方法调用表达式,按方法简单名匹配,返回 (文件, 行号, 调用代码片段) 列表。
 * <p>
 * 用 JavaParser 的 MethodCallExpr 做简单名匹配,免去全限定解析与 classpath 配置。
 * 调用方上限 K=50,超过截断并标注其余数量。
 */
public final class FindCallersTool implements AgentTool {

    private static final int MAX_CALLERS = 50;

    private final FileAccessSandbox sandbox;

    public FindCallersTool(FileAccessSandbox sandbox) {
        this.sandbox = sandbox;
    }

    @Override
    public String name() {
        return "find_callers";
    }

    @Override
    public String description() {
        return "查询指定方法在仓库内的所有直接调用方。"
                + "当你发现一个方法的签名/返回值被修改、需要确认哪些调用方可能受影响时调用。"
                + "入参格式:'文件路径#方法名'(如 src/main/java/OrderService.java#calculatePrice)。";
    }

    @Override
    public ToolResult execute(String input, AgentContext context) {
        String query = input == null ? "" : input.trim();
        if (query.isEmpty()) {
            return ToolResult.error("缺少参数。格式:'文件路径#方法名'(如 src/main/java/OrderService.java#calculatePrice)");
        }
        int sep = query.lastIndexOf('#');
        if (sep < 0) {
            return ToolResult.error("参数格式错误。应为'文件路径#方法名'(如 src/main/java/OrderService.java#calculatePrice),缺少 # 分隔符");
        }
        String targetFile = query.substring(0, sep).trim();
        String methodName = query.substring(sep + 1).trim();
        if (targetFile.isEmpty() || methodName.isEmpty()) {
            return ToolResult.error("文件路径与方法名均不能为空");
        }

        // 验证目标文件存在
        Path targetPath;
        try {
            targetPath = sandbox.resolveWithinRepo(targetFile);
        } catch (SecurityException e) {
            return ToolResult.error("目标文件路径超出仓库范围: " + targetFile);
        }
        if (!Files.isRegularFile(targetPath)) {
            return ToolResult.error("目标文件不存在: " + targetFile);
        }

        // 扫描仓库中所有 Java 源文件
        List<String> results = new ArrayList<>();
        int scanned = 0;
        int skipped = 0;
        Path repoRoot = sandbox.getRepoRoot();

        try (Stream<Path> walk = Files.walk(repoRoot)) {
            List<Path> javaFiles = walk
                    .filter(Files::isRegularFile)
                    .filter(p -> p.toString().endsWith(".java"))
                    .toList();

            for (Path filePath : javaFiles) {
                String relPath = repoRoot.relativize(filePath).toString().replace('\\', '/');
                // 排除目标文件自身的方法内调用,只找外部调用方
                // 但如果 targetFile 不在仓库中或名字不匹配就正常扫描

                String source;
                try {
                    source = Files.readString(filePath, StandardCharsets.UTF_8);
                } catch (IOException e) {
                    skipped++;
                    continue;
                }

                List<String> fileCallers = findCallersInFile(relPath, source, methodName, relPath.equals(targetFile));
                results.addAll(fileCallers);
                scanned++;
            }
        } catch (IOException e) {
            return ToolResult.error("扫描仓库文件失败: " + e.getMessage());
        }

        StringBuilder sb = new StringBuilder();
        sb.append("# find_callers 查询结果\n");
        sb.append(String.format("查询方法: `%s`\n", methodName));
        sb.append(String.format("扫描 %d 个文件(跳过 %d 个不可读文件)\n\n", scanned, skipped));

        if (results.isEmpty()) {
            sb.append("未找到直接调用方。\n");
        } else {
            int total = results.size();
            int shown = Math.min(total, MAX_CALLERS);
            sb.append(String.format("找到 %d 处调用方(显示前 %d):\n\n", total, shown));
            sb.append("| # | 文件 | 行号 | 调用代码 |\n");
            sb.append("|---|------|------|----------|\n");
            for (int i = 0; i < shown; i++) {
                sb.append(String.format("| %d | %s", i + 1, results.get(i)));
            }
            if (total > MAX_CALLERS) {
                sb.append(String.format("\n(+%d more, 请用 get_file_content 细读相关文件)\n", total - MAX_CALLERS));
            }
        }
        return ToolResult.ok(sb.toString());
    }

    private List<String> findCallersInFile(String relPath, String source, String targetMethod, boolean isTargetFile) {
        List<String> callers = new ArrayList<>();
        CompilationUnit cu;
        try {
            ParseResult<CompilationUnit> result = new JavaParser().parse(source);
            if (!result.isSuccessful() || result.getResult().isEmpty()) {
                return callers;
            }
            cu = result.getResult().get();
        } catch (Exception e) {
            return callers;
        }

        cu.findAll(MethodCallExpr.class).forEach(call -> {
            if (call.getNameAsString().equals(targetMethod)) {
                int line = call.getBegin().map(p -> p.line).orElse(-1);
                // 获取调用所在的代码行(从源码按行号读取)
                String codeLine = extractLine(source, line);
                // 特殊处理:如果该行包含 assertThrows 等测试框架模式,仍收录
                callers.add(String.format("| %s:%d | `%s` |\n",
                        relPath, line, truncate(codeLine, 80)));
            }
        });

        return callers;
    }

    private String extractLine(String source, int line) {
        if (line <= 0) return "(unknown)";
        String[] lines = source.split("\n");
        if (line > lines.length) return "(unknown)";
        return lines[line - 1].trim();
    }

    private static String truncate(String s, int maxLen) {
        if (s == null) return "";
        return s.length() <= maxLen ? s : s.substring(0, maxLen) + "...";
    }
}
