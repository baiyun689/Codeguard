package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.GraphEdge;
import com.codeguard.agent.graph.GraphEdgeKind;
import com.codeguard.agent.graph.GraphNode;
import com.codeguard.agent.graph.GraphNodeKind;
import com.codeguard.agent.graph.ProjectSnapshot;
import com.codeguard.agent.graph.SourceSet;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CompletableFuture;

/** 影响面工具：调用方、框架入口、字段读写、类型继承与解析覆盖一次返回。
 *  按 symbol 类型查询对应影响面——方法/构造器：调用方+框架入口+继承覆盖；
 *  字段：读写它的方法；类型：继承/实现它的类型。 */
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
        return "按稳定 symbol_id 查询影响面：方法/构造器返回调用方、框架入口与继承覆盖；"
                + "字段返回读写它的方法；类型返回继承/实现它的类型；并附解析覆盖状态";
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
            GraphNodeKind kind = value.graph().node(symbol)
                    .map(GraphNode::kind).orElse(null);
            if (kind == GraphNodeKind.FIELD) {
                // 字段影响面：哪些方法读写它（单层，不沿调用传播）。
                relationships.addAll(value.graph().incoming(
                        symbol, GraphEdgeKind.READS_FIELD));
                relationships.addAll(value.graph().incoming(
                        symbol, GraphEdgeKind.WRITES_FIELD));
            } else if (kind == GraphNodeKind.TYPE) {
                // 类型影响面：谁继承/实现它（单层）。
                relationships.addAll(value.graph().incoming(
                        symbol, GraphEdgeKind.EXTENDS));
                relationships.addAll(value.graph().incoming(
                        symbol, GraphEdgeKind.IMPLEMENTS));
            } else {
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
            }
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
