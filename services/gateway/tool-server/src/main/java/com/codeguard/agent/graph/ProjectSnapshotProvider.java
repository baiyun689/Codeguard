package com.codeguard.agent.graph;

/**
 * 为工具提供固定版本的项目快照。
 *
 * 支持已构建的快照或按查询懒解析的实现，工具统一通过该接口获取所需图谱。
 */
@FunctionalInterface
public interface ProjectSnapshotProvider {

    /**
     * 返回当前工具查询所需的快照视图。
     *
     * @param toolName 工具名
     * @param input    工具原始 query
     * @return 包含该查询所需事实的只读快照
     */
    ProjectSnapshot load(String toolName, String input) throws Exception;
}
