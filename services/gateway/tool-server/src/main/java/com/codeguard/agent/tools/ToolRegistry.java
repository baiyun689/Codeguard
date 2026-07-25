package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentTool;

import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 按名注册/查找工具的注册表。
 * <p>
 * 通用分发路由凭工具名在这里查到对应 {@link AgentTool} 实例后执行。
 * "加一个工具"只需 {@link #register} 一次,分发逻辑与 Python 客户端协议都不必改
 * (扩展接缝,见 design.md D2)。
 */
public final class ToolRegistry {

    private final Map<String, AgentTool> tools = new LinkedHashMap<>();

    public void register(AgentTool tool) {
        tools.put(tool.name(), tool);
    }

    /** 按名取工具;不存在返回 {@code null},由调用方转成结构化错误。 */
    public AgentTool get(String name) {
        return tools.get(name);
    }

    public boolean has(String name) {
        return tools.containsKey(name);
    }

    public Collection<AgentTool> all() {
        return tools.values();
    }
}
