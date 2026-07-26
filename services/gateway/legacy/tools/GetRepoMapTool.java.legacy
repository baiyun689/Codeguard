package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.repomap.RepoMapBuilder;

/**
 * get_repo_map —— 给审查员一份"与本次改动相关"的签名级代码地图,用于**导航**。
 * <p>
 * 借鉴 aider repo map(tree-sitter→PageRank→预算压缩),实现栈换成 JavaParser + 自实现 PageRank,
 * 且作用域收敛为 **diff 邻域**(以会话的改动文件集合为相关性种子)。它回答"该读哪个 diff 之外的
 * 文件":审查员据此再用 get_file_content 细读确认。本期无入参,纯由会话的 diff 种子驱动
 * (见 design.md Open Questions)。
 * <p>
 * 无状态、只读:每次现算(本期不缓存,见 design.md Risks);空地图返回可读提示而非报错。
 */
public final class GetRepoMapTool implements AgentTool {

    private final RepoMapBuilder builder;

    public GetRepoMapTool(RepoMapBuilder builder) {
        this.builder = builder;
    }

    @Override
    public String name() {
        return "get_repo_map";
    }

    @Override
    public String description() {
        return "获取与本次改动相关的代码地图(签名级):列出 diff 改动符号的定义文件与全局最相关的若干符号签名,"
                + "并列出**改动符号的直接调用方**(谁引用了被改的符号,据此判断改动是否破坏上游调用方的假设)。"
                + "当你看到 diff 调用/引用了一个定义不在 diff 内的符号、需要定位它在哪个文件,"
                + "或需要知道改动会波及哪些上游调用方时调用。无需入参。";
    }

    @Override
    public ToolResult execute(String input, AgentContext context) {
        String map = builder.build(context.getRepoRoot(), context.getAllowedFiles());
        if (map == null || map.isBlank()) {
            return ToolResult.ok("(未在仓库中找到与本次改动相关的可定位定义。"
                    + "请基于 diff 审查;如已知某文件路径,可直接用 get_file_content 读取。)");
        }
        return ToolResult.ok("# Repo map(与本次改动相关的代码定义 + 直接调用方,仅签名,实现以 ⋮... 省略)\n"
                + "# 需要看某定义的实现时,用 get_file_content 读取其所在文件。\n\n" + map);
    }
}
