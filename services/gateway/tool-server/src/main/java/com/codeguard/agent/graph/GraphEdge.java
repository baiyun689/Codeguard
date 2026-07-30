package com.codeguard.agent.graph;

import com.fasterxml.jackson.annotation.JsonProperty;

/** 带来源和解析状态的不可变语义关系。 */
public record GraphEdge(
        String sourceId,
        String targetId,
        GraphEdgeKind kind,
        String file,
        int line,
        @JsonProperty("source_set") SourceSet sourceSet,
        ResolutionStatus resolution,
        String extractor
) {}
