package com.codeguard.agent.graph;

/**
 * 为工具提供版本固定的快照。
 *
 * <p>普通测试可以把一个已经构建的 {@link ProjectSnapshot} 包装进该接口；生产会话则
 * 使用按工具查询懒解析的实现。接口故意只暴露一个查询接缝，避免工具自行触发全项目
 * 图谱构建。</p>
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
