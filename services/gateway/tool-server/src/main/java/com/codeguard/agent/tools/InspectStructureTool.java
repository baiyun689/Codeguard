package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.GraphEdge;
import com.codeguard.agent.graph.GraphEdgeKind;
import com.codeguard.agent.graph.GraphNode;
import com.codeguard.agent.graph.ProjectSnapshot;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;

/** MaintainabilityAgent 的结构工具：声明、依赖、继承和耦合事实。 */
public final class InspectStructureTool implements AgentTool {
    private final CompletableFuture<ProjectSnapshot> snapshot;

    public InspectStructureTool(CompletableFuture<ProjectSnapshot> snapshot) {
        this.snapshot = snapshot;
    }

    @Override
    public String name() {
        return "inspect_structure";
    }

    @Override
    public String description() {
        return "按 symbol_id 查询声明、调用耦合、继承和字段关系";
    }

    @Override
    public ToolResult execute(String input, AgentContext context) {
        String symbol = GraphToolSupport.symbolId(input);
        if (symbol.isBlank()) {
            return ToolResult.error("缺少 symbol_id");
        }
        try {
            ProjectSnapshot value = GraphToolSupport.await(snapshot);
            List<GraphEdge> relationships = new ArrayList<>();
            for (GraphEdgeKind kind : List.of(
                    GraphEdgeKind.DECLARES, GraphEdgeKind.CALLS, GraphEdgeKind.EXTENDS,
                    GraphEdgeKind.IMPLEMENTS, GraphEdgeKind.OVERRIDES,
                    GraphEdgeKind.READS_FIELD, GraphEdgeKind.WRITES_FIELD)) {
                relationships.addAll(value.graph().outgoing(symbol, kind));
                relationships.addAll(value.graph().incoming(symbol, kind));
            }
            List<GraphNode> nodes = new ArrayList<>();
            value.graph().node(symbol).ifPresent(nodes::add);
            relationships.stream()
                    .flatMap(edge -> java.util.stream.Stream.of(
                            value.graph().node(edge.sourceId()),
                            value.graph().node(edge.targetId())))
                    .flatMap(java.util.Optional::stream)
                    .forEach(nodes::add);
            return GraphToolSupport.facts(
                    value, symbol, nodes, relationships, List.of(), true);
        } catch (Exception exception) {
            return ToolResult.error("graph_unavailable: " + exception.getMessage());
        }
    }
}
