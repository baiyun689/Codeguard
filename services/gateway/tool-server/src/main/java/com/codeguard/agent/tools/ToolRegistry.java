package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentTool;

import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.Map;

/** 按名称注册和查找工具实例，供 HTTP 分发接口调用。 */
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
