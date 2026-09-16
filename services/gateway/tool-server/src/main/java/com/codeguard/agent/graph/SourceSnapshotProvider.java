package com.codeguard.agent.graph;

/**
 * 提供符号源码读取所需的文件快照。
 *
 * 仅解析目标符号所在文件的 AST，无需等待项目级索引或语义图完成。
 * 不提供该接口的调用方可使用通用项目快照接口。
 */
@FunctionalInterface
public interface SourceSnapshotProvider {

    /** 返回包含目标 symbol 所需源码和局部 AST 的只读快照。 */
    ProjectSnapshot loadSource(String input) throws Exception;
}
