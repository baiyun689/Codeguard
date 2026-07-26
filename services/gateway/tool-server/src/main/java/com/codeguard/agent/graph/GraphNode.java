package com.codeguard.agent.graph;

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
        List<String> annotations
) {
    public GraphNode {
        annotations = List.copyOf(annotations);
    }
}
