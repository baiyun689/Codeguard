package com.codeguard.agent.core;

/**
 * Agent 工具的统一契约。
 * <p>
 * 工具服务里每个可被 Python Agent 回调的能力都实现这个接口。新增工具 = 实现本接口 +
 * 注册到 {@link com.codeguard.agent.tools.ToolRegistry},无需改动 HTTP 分发协议
 * (扩展接缝,见 design.md D2)。
 * <p>
 * 所有工具都是**只读**的(只提供代码事实,不修改任何状态),这是护栏层的基本约束。
 */
public interface AgentTool {

    /** 工具名称,与通用分发路由 {@code POST /api/v1/tools/{name}} 中的 name 对应。 */
    String name();

    /** 工具用途描述,供调用方/调试参考。 */
    String description();

    /**
     * 执行工具。
     *
     * @param input   工具输入(本期工具均为单字符串:文件路径或查询)
     * @param context 当前会话上下文(仓库根、允许文件集等)
     * @return 统一信封的执行结果
     */
    ToolResult execute(String input, AgentContext context);
}
