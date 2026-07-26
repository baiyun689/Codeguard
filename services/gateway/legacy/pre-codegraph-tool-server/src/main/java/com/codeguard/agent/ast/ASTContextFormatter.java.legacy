package com.codeguard.agent.ast;

import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

/**
 * 将 DiffASTResult 格式化为 LLM 可读文本，带 Token 预算 + 两级裁剪。
 * <p>
 * 预算 = min(20% × diffTokens × CHARS_PER_TOKEN, 600), floor=50。
 * Tier 0: 完整——类信息 + 所有方法(变更行优先) + 变更行范围内的控制流。
 * Tier 1: diff-scoped——类信息 + 仅变更行重叠的方法 + 变更行范围内的控制流。
 * Tier 2: minimal——仅类名 + 方法签名列表(逗号分隔)。
 */
public final class ASTContextFormatter {

    private static final double MAX_BUDGET_FRACTION = 0.20;
    private static final int ABSOLUTE_MAX_CHARS = 600;
    private static final int CHARS_PER_TOKEN = 4;
    private static final int FLOOR_CHARS = 50;

    private static final Pattern HUNK_PATTERN =
            Pattern.compile("^@@\\s+-\\d+(?:,\\d+)?\\s+\\+(\\d+)(?:,(\\d+))?\\s+@@");

    private ASTContextFormatter() {}

    /**
     * 格式化 AST 结果，根据预算自动选择 Tier 0 → 1 → 2。
     *
     * @param result    AST 分析结果
     * @param diffText  原始 diff 文本（用于提取变更行号）
     * @param diffTokens diff 文本的 Token 数（用于计算预算）
     * @return 格式化后的文本，解析失败或无类时返回空串
     */
    public static String format(DiffASTResult result, String diffText, int diffTokens) {
        if (result == null || !result.parseSucceeded() || result.classes().isEmpty()) {
            return "";
        }
        int budget = Math.max(FLOOR_CHARS,
                Math.min((int) (diffTokens * MAX_BUDGET_FRACTION * CHARS_PER_TOKEN), ABSOLUTE_MAX_CHARS));
        Set<Integer> changedLines = extractChangedLines(diffText);

        String tier0 = renderTier0(result, changedLines);
        if (tier0.length() <= budget) {
            return tier0;
        }

        String tier1 = renderTier1(result, changedLines);
        if (tier1.length() <= budget) {
            return tier1;
        }

        return renderTier2(result);
    }

    // ========== 变更行解析 ==========

    /**
     * 从 unified diff 文本中解析所有 @@ 块头，提取变更行号（new file 侧）。
     */
    static Set<Integer> extractChangedLines(String diffText) {
        Set<Integer> lines = new HashSet<>();
        if (diffText == null || diffText.isEmpty()) {
            return lines;
        }
        for (String line : diffText.split("\n")) {
            Matcher m = HUNK_PATTERN.matcher(line);
            if (m.find()) {
                int start = Integer.parseInt(m.group(1));
                int count = m.group(2) != null ? Integer.parseInt(m.group(2)) : 1;
                for (int i = 0; i < count; i++) {
                    lines.add(start + i);
                }
            }
        }
        return lines;
    }

    // ========== Tier 0: 完整格式化 ==========

    static String renderTier0(DiffASTResult result, Set<Integer> changedLines) {
        StringBuilder sb = new StringBuilder();
        appendHeader(sb, result.filePath());
        appendClasses(sb, result.classes());
        appendMethods(sb, result.methods(), changedLines, false);
        appendControlFlow(sb, result.controlFlowNodes(), changedLines);
        appendCallEdges(sb, result.callEdges());
        return sb.toString();
    }

    // ========== Tier 1: Diff-scoped 格式化 ==========

    static String renderTier1(DiffASTResult result, Set<Integer> changedLines) {
        StringBuilder sb = new StringBuilder();
        appendHeader(sb, result.filePath());
        appendClasses(sb, result.classes());
        appendMethods(sb, result.methods(), changedLines, true);
        appendControlFlow(sb, result.controlFlowNodes(), changedLines);
        return sb.toString();
    }

    // ========== Tier 2: 最小化格式化 ==========

    static String renderTier2(DiffASTResult result) {
        StringBuilder sb = new StringBuilder();
        sb.append("AST for: ").append(result.filePath()).append("\n");
        if (!result.classes().isEmpty()) {
            DiffASTResult.ClassDef cls = result.classes().get(0);
            sb.append("  class: ").append(cls.name()).append("\n");
        }
        if (!result.methods().isEmpty()) {
            sb.append("  Methods: ");
            sb.append(result.methods().stream()
                    .map(m -> formatMethodSignature(m, true))
                    .collect(Collectors.joining(", ")));
            sb.append("\n");
        }
        return sb.toString();
    }

    // ========== 辅助渲染方法 ==========

    private static void appendHeader(StringBuilder sb, String filePath) {
        sb.append("AST for: ").append(filePath).append("\n");
    }

    private static void appendClasses(StringBuilder sb, List<DiffASTResult.ClassDef> classes) {
        for (DiffASTResult.ClassDef cls : classes) {
            sb.append("  class: ").append(cls.name()).append("\n");
        }
    }

    /**
     * 追加方法列表，changedLinesFirst 控制是否仅输出与变更行重叠的方法。
     * <p>
     * 方法排序：变更范围内的优先，其次按行号升序。
     */
    private static void appendMethods(StringBuilder sb, List<DiffASTResult.MethodDef> methods,
                                      Set<Integer> changedLines, boolean changedOnly) {
        if (methods.isEmpty()) {
            return;
        }

        List<DiffASTResult.MethodDef> filtered = new ArrayList<>();
        if (changedOnly && !changedLines.isEmpty()) {
            for (DiffASTResult.MethodDef m : methods) {
                if (overlaps(m.startLine(), m.endLine(), changedLines)) {
                    filtered.add(m);
                }
            }
        } else {
            filtered.addAll(methods);
        }

        if (filtered.isEmpty()) {
            return;
        }

        // 排序：变更行重叠的优先，其次按 startLine
        filtered.sort(Comparator
                .comparing((DiffASTResult.MethodDef m) ->
                        !overlaps(m.startLine(), m.endLine(), changedLines))
                .thenComparingInt(DiffASTResult.MethodDef::startLine));

        String label = changedOnly ? "  Methods (changed):\n" : "  Methods:\n";
        sb.append(label);
        for (DiffASTResult.MethodDef m : filtered) {
            sb.append("    ").append(formatMethodSignature(m, false)).append("\n");
        }
    }

    /**
     * 格式化单个方法签名。
     *
     * @param m           方法定义
     * @param compactOnly 是否为 Tier2 紧凑模式（仅返回类型+方法名+参数）
     * @return 格式化的方法签名字符串
     */
    private static String formatMethodSignature(DiffASTResult.MethodDef m, boolean compactOnly) {
        StringBuilder sig = new StringBuilder();

        // 注解
        for (String ann : m.annotations()) {
            sig.append(ann).append(" ");
        }

        // 可见性：省略 package-private
        if (!"package-private".equals(m.visibility())) {
            sig.append(m.visibility()).append(" ");
        }

        // modifiers (static/final/等)
        for (String mod : m.modifiers()) {
            sig.append(mod).append(" ");
        }

        // 返回类型
        if (m.returnType() != null && !m.returnType().isEmpty()) {
            sig.append(m.returnType()).append(" ");
        }

        // 方法名
        sig.append(m.name());

        // 参数
        sig.append("(");
        for (int i = 0; i < m.paramTypes().size(); i++) {
            if (i > 0) sig.append(", ");
            sig.append(m.paramTypes().get(i));
            if (i < m.paramNames().size() && !m.paramNames().get(i).isEmpty()) {
                sig.append(" ").append(m.paramNames().get(i));
            }
        }
        sig.append(")");

        // 行号范围
        if (!compactOnly) {
            sig.append(" [L").append(m.startLine()).append("-L").append(m.endLine()).append("]");
        }

        return sig.toString();
    }

    private static void appendControlFlow(StringBuilder sb, List<DiffASTResult.CFNode> nodes,
                                          Set<Integer> changedLines) {
        if (nodes.isEmpty()) {
            return;
        }
        // 如果 changedLines 为空，显示全部；否则只显示与变更行重叠的
        List<DiffASTResult.CFNode> filtered;
        if (changedLines.isEmpty()) {
            filtered = nodes;
        } else {
            filtered = new ArrayList<>();
            for (DiffASTResult.CFNode n : nodes) {
                if (overlaps(n.startLine(), n.endLine(), changedLines)) {
                    filtered.add(n);
                }
            }
        }
        if (filtered.isEmpty()) {
            return;
        }
        sb.append("  Control flow:\n");
        for (DiffASTResult.CFNode n : filtered) {
            sb.append("    ").append(n.type())
                    .append(" [L").append(n.startLine()).append("-L").append(n.endLine()).append("]");
            if (n.condition() != null && !n.condition().isEmpty()) {
                sb.append(" ").append(n.condition());
            }
            sb.append("\n");
        }
    }

    private static void appendCallEdges(StringBuilder sb, List<DiffASTResult.CallEdgeDef> edges) {
        // 按方法分组
        Map<String, List<DiffASTResult.CallEdgeDef>> byCaller = new LinkedHashMap<>();
        for (DiffASTResult.CallEdgeDef e : edges) {
            byCaller.computeIfAbsent(e.callerMethod(), k -> new ArrayList<>()).add(e);
        }
        if (byCaller.isEmpty()) {
            return;
        }
        sb.append("  Call edges:\n");
        for (Map.Entry<String, List<DiffASTResult.CallEdgeDef>> entry : byCaller.entrySet()) {
            sb.append("    ").append(entry.getKey()).append(" -> calls: ");
            sb.append(entry.getValue().stream()
                    .map(e -> {
                        if (e.calleeScope() != null && !e.calleeScope().isEmpty()) {
                            return e.calleeScope() + "." + e.calleeMethod();
                        }
                        return e.calleeMethod();
                    })
                    .collect(Collectors.joining(", ")));
            sb.append("\n");
        }
    }

    /**
     * 判断区间 [start, end] 是否与变更行集合有重叠。
     */
    private static boolean overlaps(int start, int end, Set<Integer> changedLines) {
        if (changedLines.isEmpty()) {
            return false;
        }
        for (int line = start; line <= end; line++) {
            if (changedLines.contains(line)) {
                return true;
            }
        }
        return false;
    }
}
