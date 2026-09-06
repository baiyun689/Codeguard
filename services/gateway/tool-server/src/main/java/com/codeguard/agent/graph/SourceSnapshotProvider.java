package com.codeguard.agent.graph;

/**
 * 源码读取专用的快照接缝。
 *
 * <p>源码工具只需要目标 symbol 所在文件的 AST，不应为了读取一个方法而等待
 * 全项目轻量索引或语义图构建。生产懒加载提供器实现该接口，测试和旧调用方仍可
 * 使用 {@link ProjectSnapshotProvider} 的通用接缝。</p>
 */
@FunctionalInterface
public interface SourceSnapshotProvider {

    /** 返回包含目标 symbol 所需源码和局部 AST 的只读快照。 */
    ProjectSnapshot loadSource(String input) throws Exception;
}
