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
        String diffText = input == null ? "" : input;
        var allowedFiles = context.getAllowedFiles();
        if (allowedFiles.isEmpty()) {
            return ToolResult.ok("(无可解析的 Java AST 上下文)");
        }

        int diffTokens = Math.max(1, diffText.length() / 4);
        StringBuilder all = new StringBuilder();
        int parsed = 0;

        for (String relPath : allowedFiles) {
            if (!relPath.endsWith(".java")) continue;
            Path fullPath;
            try {
                fullPath = sandbox.resolveWithinRepo(relPath);
            } catch (SecurityException e) {
                continue;
            }
            if (!Files.isRegularFile(fullPath)) continue;

            String source;
            try {
                source = Files.readString(fullPath, StandardCharsets.UTF_8);
            } catch (IOException e) {
                continue;
            }

            DiffASTResult result = DiffASTAnalyzer.analyze(relPath, source);
            if (!result.parseSucceeded() || result.classes().isEmpty()) continue;

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
