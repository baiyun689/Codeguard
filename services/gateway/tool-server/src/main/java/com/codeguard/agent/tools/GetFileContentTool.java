package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.CompletableFuture;
import com.codeguard.agent.graph.ProjectSnapshot;

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
    private final CompletableFuture<ProjectSnapshot> snapshot;

    public GetFileContentTool(FileAccessSandbox sandbox) {
        this.sandbox = sandbox;
        this.snapshot = null;
    }

    public GetFileContentTool(
            FileAccessSandbox sandbox,
            CompletableFuture<ProjectSnapshot> snapshot
    ) {
        this.sandbox = sandbox;
        this.snapshot = snapshot;
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
        if (filePath.startsWith("file:")) {
            filePath = filePath.substring("file:".length());
        }
        if (filePath.isEmpty()) {
            return ToolResult.error("文件路径不能为空");
        }

        final Path fullPath;
        try {
            fullPath = sandbox.resolveReadableFile(filePath);
        } catch (FileAccessSandbox.AccessException e) {
            return ToolResult.error(e.getMessage());
        }

        if (snapshot != null) {
            try {
                ProjectSnapshot value = GraphToolSupport.await(snapshot);
                String requestedPath = filePath.replace('\\', '/');
                String realRelativePath = sandbox.getRealRepoRoot()
                        .relativize(fullPath).toString().replace('\\', '/');
                if (!context.getAllowedFiles().contains(requestedPath)
                        && !context.getAllowedFiles().contains(realRelativePath)
                        && !value.sources().containsKey(realRelativePath)) {
                    return ToolResult.error("unconfirmed_path: " + filePath);
                }
                String cached = value.sources().get(realRelativePath);
                if (cached != null) {
                    long size = cached.getBytes(StandardCharsets.UTF_8).length;
                    if (size > MAX_FILE_SIZE_BYTES) {
                        return ToolResult.error(
                                "文件过大 (" + size + " 字节,上限 "
                                        + MAX_FILE_SIZE_BYTES + "),请聚焦具体方法/片段再查");
                    }
                    return ToolResult.ok("文件: " + requestedPath + "\n" + cached);
                }
            } catch (Exception exception) {
                return ToolResult.error("graph_unavailable: " + exception.getMessage());
            }
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
