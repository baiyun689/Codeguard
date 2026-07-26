package com.codeguard.agent.graph;

/** 带来源和解析状态的不可变语义关系。 */
public record GraphEdge(
        String sourceId,
        String targetId,
        GraphEdgeKind kind,
        String file,
        int line,
        ResolutionStatus resolution,
        String extractor
) {}
