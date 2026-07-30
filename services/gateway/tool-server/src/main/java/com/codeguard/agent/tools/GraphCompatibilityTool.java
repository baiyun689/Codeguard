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
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CompletableFuture;

/** 旧工具名到同一 ProjectSnapshot/ProjectCodeGraph 实现的临时兼容 Adapter。 */
public final class GraphCompatibilityTool implements AgentTool {
    private final String name;
    private final CompletableFuture<ProjectSnapshot> snapshot;

    public GraphCompatibilityTool(String name, CompletableFuture<ProjectSnapshot> snapshot) {
        this.name = name;
        this.snapshot = snapshot;
    }

    @Override
    public String name() {
        return name;
    }

    @Override
    public String description() {
        return "兼容工具；事实来自项目语义图";
    }

    @Override
    public ToolResult execute(String input, AgentContext context) {
        try {
            ProjectSnapshot value = GraphToolSupport.await(snapshot);
            return switch (name) {
                case "find_callers" -> callers(value, input);
                case "find_sensitive_apis" -> sensitive(value);
                case "get_code_metrics" -> structure(value, input);
                case "get_diff_ast" -> changedStructure(value, context);
                default -> ToolResult.error("未知兼容工具: " + name);
            };
        } catch (Exception exception) {
            return ToolResult.error("graph_unavailable: " + exception.getMessage());
        }
    }

    private static ToolResult callers(ProjectSnapshot snapshot, String query) {
        int separator = query == null ? -1 : query.lastIndexOf('#');
        if (separator < 1) {
            return ToolResult.error("参数格式应为 文件路径#方法名");
        }
        String file = query.substring(0, separator).replace('\\', '/');
        String method = query.substring(separator + 1);
        GraphNode target = snapshot.graph().symbolsInFile(file).stream()
                .filter(node -> node.signature().contains(method + "("))
                .findFirst()
                .orElse(null);
        if (target == null) {
            return GraphToolSupport.facts(snapshot, query, List.of(), List.of(),
                    List.of("symbol_not_resolved"));
        }
        List<GraphEdge> edges = snapshot.graph().incoming(target.id(), GraphEdgeKind.CALLS);
        List<GraphNode> nodes = edges.stream()
                .map(GraphEdge::sourceId)
                .map(snapshot.graph()::node)
                .flatMap(java.util.Optional::stream)
                .toList();
        return GraphToolSupport.facts(snapshot, target.id(), nodes, edges, List.of());
    }

    private static ToolResult sensitive(ProjectSnapshot snapshot) {
        List<GraphEdge> edges = snapshot.graph().edges().stream()
                .filter(edge -> edge.kind() == GraphEdgeKind.CALLS)
                .filter(edge -> isSensitive(edge.targetId()))
                .toList();
        return GraphToolSupport.facts(snapshot, "project", List.of(), edges, List.of());
    }

    private static ToolResult structure(ProjectSnapshot snapshot, String input) {
        String file = input == null ? "" : input.replace('\\', '/');
        List<GraphNode> nodes = snapshot.graph().symbolsInFile(file);
        List<GraphEdge> edges = new ArrayList<>();
        for (GraphNode node : nodes) {
            edges.addAll(snapshot.graph().outgoing(node.id(), GraphEdgeKind.CALLS));
            edges.addAll(snapshot.graph().outgoing(node.id(), GraphEdgeKind.DECLARES));
        }
        return GraphToolSupport.facts(
                snapshot, "file:" + file, nodes, edges, List.of(), true);
    }

    private static ToolResult changedStructure(ProjectSnapshot snapshot, AgentContext context) {
        List<GraphNode> nodes = context.getAllowedFiles().stream()
                .flatMap(file -> snapshot.graph().symbolsInFile(file).stream())
                .toList();
        boolean onlyTests = !context.getAllowedFiles().isEmpty()
                && context.getAllowedFiles().stream()
                .allMatch(file -> SourceSet.fromPath(file).isTest());
        return GraphToolSupport.facts(
                snapshot,
                "changed_files",
                nodes,
                List.of(),
                List.of(),
                true,
                onlyTests ? SourceSet.TEST : SourceSet.MAIN);
    }

    private static boolean isSensitive(String symbol) {
        String lower = symbol.toLowerCase(Locale.ROOT);
        return List.of("execute", "exec", "query", "deserialize", "readobject",
                        "processbuilder", "urlconnection", "scriptengine")
                .stream().anyMatch(lower::contains);
    }
}
