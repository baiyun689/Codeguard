package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.GraphEdge;
import com.codeguard.agent.graph.GraphEdgeKind;
import com.codeguard.agent.graph.GraphNode;
import com.codeguard.agent.graph.ProjectSnapshot;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.CompletableFuture;

/** ThreatModelAgent 的安全路径工具：入口、敏感调用与未解析边。 */
public final class InspectSecurityPathTool implements AgentTool {
    private static final List<String> SENSITIVE_TERMS = List.of(
            "execute", "exec", "query", "deserialize", "readobject", "getruntime",
            "processbuilder", "urlconnection", "xmlreader", "scriptengine", "cipher");
    private final CompletableFuture<ProjectSnapshot> snapshot;

    public InspectSecurityPathTool(CompletableFuture<ProjectSnapshot> snapshot) {
        this.snapshot = snapshot;
    }

    @Override
    public String name() {
        return "inspect_security_path";
    }

    @Override
    public String description() {
        return "按 symbol_id 查询框架入口、敏感 API 调用路径与静态分析限制";
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
            relationships.addAll(value.graph().incoming(symbol, GraphEdgeKind.EXPOSES_ROUTE));
            relationships.addAll(value.graph().incoming(symbol, GraphEdgeKind.LISTENS_TO_EVENT));
            Set<String> frontier = new LinkedHashSet<>(Set.of(symbol));
            Set<String> visited = new LinkedHashSet<>();
            for (int depth = 0; depth < 3 && !frontier.isEmpty(); depth++) {
                Set<String> next = new LinkedHashSet<>();
                for (String current : frontier) {
                    if (!visited.add(current)) {
                        continue;
                    }
                    for (GraphEdge edge : value.graph().outgoing(
                            current, GraphEdgeKind.CALLS)) {
                        if (sensitive(edge.targetId())
                                || edge.resolution()
                                == com.codeguard.agent.graph.ResolutionStatus.UNRESOLVED) {
                            relationships.add(edge);
                        }
                        next.add(edge.targetId());
                    }
                }
                frontier = next;
            }
            List<GraphNode> nodes = new ArrayList<>();
            value.graph().node(symbol).ifPresent(nodes::add);
            relationships.stream()
                    .map(GraphEdge::sourceId)
                    .map(value.graph()::node)
                    .flatMap(java.util.Optional::stream)
                    .forEach(nodes::add);
            List<String> limits = relationships.stream()
                    .filter(edge -> edge.resolution()
                            == com.codeguard.agent.graph.ResolutionStatus.UNRESOLVED)
                    .map(edge -> "unresolved_call:" + edge.targetId())
                    .toList();
            return GraphToolSupport.facts(value, symbol, nodes, relationships, limits);
        } catch (Exception exception) {
            return ToolResult.error("graph_unavailable: " + exception.getMessage());
        }
    }

    private static boolean sensitive(String symbol) {
        String lower = symbol.toLowerCase(Locale.ROOT);
        return SENSITIVE_TERMS.stream().anyMatch(lower::contains);
    }
}
