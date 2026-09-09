package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.ProjectSnapshotProvider;

/** Reads bounded source for a resolved symbol through the session sandbox. */
public final class ReadSymbolTool implements AgentTool {
    private final SymbolSourceReader delegate;

    public ReadSymbolTool(ProjectSnapshotProvider snapshot) {
        this.delegate = new SymbolSourceReader(snapshot);
    }

    @Override
    public String name() {
        return "read_symbol";
    }

    @Override
    public String description() {
        return "读取已解析项目 symbol 的有界源码；接受初始符号、关系查询和源码成员目录返回的 symbol_id，"
                + "支持 start_line/end_line/cursor 续取，不接受文件路径、文件名或自行猜测的 symbol。";
    }

    @Override
    public ToolResult execute(String input, AgentContext context) {
        return delegate.execute(input, context);
    }
}
