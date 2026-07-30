package com.codeguard.agent.graph;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;

/** 不可变语义节点，行号均为一基。 */
public record GraphNode(
        String id,
        GraphNodeKind kind,
        String file,
        int startLine,
        int endLine,
        String signature,
        String ownerId,
        @JsonProperty("source_set") SourceSet sourceSet,
        List<String> annotations
) {
    public GraphNode {
        annotations = List.copyOf(annotations);
    }
}
