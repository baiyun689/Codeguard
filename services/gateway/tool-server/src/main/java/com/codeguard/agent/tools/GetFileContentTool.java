package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * 读取仓库内指定文件的完整内容。
 * <p>
 * 这是阶段 3 落地的第一个、也是本期唯一的工具。让审查员能看到 diff 之外的完整文件,
 * 从而审出"只看 diff 看不出"的问题。所有访问都经 {@link FileAccessSandbox} 护栏:
 * 路径穿越 / 范围外 / 超大 / 不存在,一律以结构化错误返回,绝不抛未处理异常。
 */
public final class GetFileContentTool implements AgentTool {

    /** 文件大小上限:超过则拒绝,提示改用更细粒度的方式(后续 get_method_definition 等)。 */
    private static final long MAX_FILE_SIZE_BYTES = 100_000L;

    private final FileAccessSandbox sandbox;

    public GetFileContentTool(FileAccessSandbox sandbox) {
        this.sandbox = sandbox;
    }

    @Override
    public String name() {
        return "get_file_content";
    }

    @Override
    public String description() {
        return "读取仓库中指定文件的完整内容。输入:文件相对路径(如 src/main/java/com/example/Service.java)";
    }

    @Override
    public ToolResult execute(String input, AgentContext context) {
        String filePath = input == null ? "" : input.trim();
        if (filePath.isEmpty()) {
            return ToolResult.error("文件路径不能为空");
        }

        // 显式拒绝 .. ——规范化校验已能拦穿越,这里再给一条更可读的早返回。
        if (filePath.contains("..")) {
            return ToolResult.error("不允许包含 .. 的路径: " + filePath);
        }

        final Path fullPath;
        try {
            fullPath = sandbox.resolveWithinRepo(filePath);
        } catch (SecurityException e) {
            return ToolResult.error(e.getMessage());
        }

        // 护栏放宽(design.md D5):授权从"仅 diff 改动文件"改为"repo 根内 + 源码扩展名白名单",
        // 使审查员能读 get_repo_map 指向的 diff 之外定义文件;非源码/配置/密钥类型仍拒。
        if (!sandbox.isReadableSource(filePath)) {
            return ToolResult.error("文件类型不可读(仅限源码文件): " + filePath);
        }

        if (!Files.isRegularFile(fullPath)) {
            return ToolResult.error("文件不存在: " + filePath);
        }

        try {
            long size = Files.size(fullPath);
            if (size > MAX_FILE_SIZE_BYTES) {
                return ToolResult.error(
                        "文件过大 (" + size + " 字节,上限 " + MAX_FILE_SIZE_BYTES + "),请聚焦具体方法/片段再查");
            }
            String content = Files.readString(fullPath, StandardCharsets.UTF_8);
            return ToolResult.ok("文件: " + filePath + "\n" + content);
        } catch (IOException e) {
            return ToolResult.error("读取文件失败: " + e.getMessage());
        }
    }
}
