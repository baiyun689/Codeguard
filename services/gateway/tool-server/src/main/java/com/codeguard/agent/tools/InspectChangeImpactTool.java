package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.GraphEdge;
import com.codeguard.agent.graph.GraphEdgeKind;
import com.codeguard.agent.graph.GraphNode;
import com.codeguard.agent.graph.ProjectSnapshot;
import com.codeguard.agent.graph.SourceSet;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CompletableFuture;

/** BehaviorAgent 的影响面工具：调用方、框架入口和解析限制一次返回。 */
public final class InspectChangeImpactTool implements AgentTool {
    private final CompletableFuture<ProjectSnapshot> snapshot;

    public InspectChangeImpactTool(CompletableFuture<ProjectSnapshot> snapshot) {
        this.snapshot = snapshot;
    }

    @Override
    public String name() {
        return "inspect_change_impact";
    }

    @Override
    public String description() {
        return "按稳定 symbol_id 查询调用方、框架入口、继承影响与解析覆盖";
    }

    @Override
    public ToolResult execute(String input, AgentContext context) {
        String symbol = GraphToolSupport.symbolId(input);
        if (symbol.isBlank()) {
            return ToolResult.error("缺少 symbol_id");
        }
        try {
            ProjectSnapshot value = GraphToolSupport.await(snapshot);
            SourceSet sourceScope = GraphToolSupport.sourceScope(value, symbol);
            List<GraphEdge> relationships = new ArrayList<>();
            Set<String> frontier = new LinkedHashSet<>(Set.of(symbol));
            Set<String> visited = new LinkedHashSet<>();
            for (int depth = 0; depth < 3 && !frontier.isEmpty(); depth++) {
                Set<String> next = new LinkedHashSet<>();
                for (String current : frontier) {
                    if (!visited.add(current)) {
                        continue;
                    }
                    List<GraphEdge> callers =
                            value.graph().incoming(current, GraphEdgeKind.CALLS);
                    relationships.addAll(callers);
                    callers.stream()
                            .filter(edge -> GraphToolSupport.inScope(edge, sourceScope))
                            .forEach(edge -> next.add(edge.sourceId()));
                    relationships.addAll(value.graph().incoming(
                            current, GraphEdgeKind.EXPOSES_ROUTE));
                    relationships.addAll(value.graph().incoming(
                            current, GraphEdgeKind.LISTENS_TO_EVENT));
                    relationships.addAll(value.graph().incoming(
                            current, GraphEdgeKind.SCHEDULED_BY));
                }
                frontier = next;
            }
            relationships.addAll(value.graph().incoming(symbol, GraphEdgeKind.OVERRIDES));
            List<GraphNode> nodes = new ArrayList<>();
            value.graph().node(symbol).ifPresent(nodes::add);
            relationships.stream()
                    .map(GraphEdge::sourceId)
                    .map(value.graph()::node)
                    .flatMap(java.util.Optional::stream)
                    .forEach(nodes::add);
            return GraphToolSupport.facts(value, symbol, nodes, relationships, List.of());
        } catch (Exception exception) {
            return ToolResult.error("graph_unavailable: " + exception.getMessage());
        }
    }
}
