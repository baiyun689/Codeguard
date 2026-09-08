package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.ProjectSnapshotProvider;

/**
 * Stable public source-reading capability for the controlled reviewer.
 *
 * <p>The legacy wire name {@code get_file_content} remains registered for
 * compatibility.  New callers use this narrower name so a model cannot
 * mistake the operation for arbitrary file access.</p>
 */
public final class ReadSymbolTool implements AgentTool {
    private final GetFileContentTool delegate;

    public ReadSymbolTool(ProjectSnapshotProvider snapshot) {
        this.delegate = new GetFileContentTool(snapshot);
    }

    @Override
    public String name() {
        return "read_symbol";
    }

    @Override
    public String description() {
        return "读取已解析项目 symbol 的有界源码；只接受 GraphPlan 或前序关系查询返回的 symbol_id，"
                + "支持 start_line/end_line/cursor 续取，不接受文件路径、文件名或自行猜测的 symbol。";
    }

    @Override
    public ToolResult execute(String input, AgentContext context) {
        return delegate.execute(input, context);
    }
}
