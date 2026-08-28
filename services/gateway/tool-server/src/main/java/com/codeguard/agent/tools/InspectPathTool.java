package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.GraphEdge;
import com.codeguard.agent.graph.GraphEdgeKind;
import com.codeguard.agent.graph.GraphNode;
import com.codeguard.agent.graph.GraphNodeKind;
import com.codeguard.agent.graph.ProjectSnapshot;
import com.codeguard.agent.graph.ResolutionStatus;
import com.codeguard.agent.graph.SourceSet;
import com.fasterxml.jackson.databind.JsonNode;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.CompletableFuture;

/** 有界下游路径工具：行为执行路径或安全 source-to-sink 路径。 */
public final class InspectPathTool implements AgentTool {
    private static final int DEFAULT_MAX_DEPTH = 3;
    private static final List<String> SENSITIVE_TERMS = List.of(
            "execute", "exec", "query", "deserialize", "readobject", "getruntime",
            "processbuilder", "urlconnection", "xmlreader", "scriptengine", "cipher");

    private final CompletableFuture<ProjectSnapshot> snapshot;

    public InspectPathTool(CompletableFuture<ProjectSnapshot> snapshot) {
        this.snapshot = snapshot;
    }

    @Override
    public String name() {
        return "inspect_path";
    }

    @Override
    public String description() {
        return "按稳定 symbol_id 查询有界下游路径。path_kind=behavior 查询 callee、"
                + "callback、listener、接口实现和状态读写；path_kind=security 查询输入源、"
                + "传播、防护和敏感 sink。path_kind 只能是 behavior 或 security。";
    }

    @Override
    public ToolResult execute(String input, AgentContext context) {
        JsonNode query;
        try {
            query = GraphToolSupport.JSON.readTree(input == null ? "" : input);
        } catch (Exception exception) {
            return ToolResult.error("invalid_path_query");
        }
        if (query == null || !query.isObject()) {
            return ToolResult.error("invalid_path_query");
        }
        String symbol = query.path("symbol_id").asText("").trim();
        String pathKind = query.path("path_kind").asText("").trim();
        if (symbol.isBlank()) {
            return ToolResult.error("missing_symbol_id");
        }
        if (!pathKind.equals("behavior") && !pathKind.equals("security")) {
            return ToolResult.error("invalid_path_kind");
        }
        int maxDepth = query.path("max_depth").asInt(DEFAULT_MAX_DEPTH);
        if (maxDepth < 1 || maxDepth > DEFAULT_MAX_DEPTH) {
            return ToolResult.error("invalid_max_depth");
        }
        try {
            ProjectSnapshot value = GraphToolSupport.await(snapshot);
            ToolResult result = pathKind.equals("security")
                    ? securityPath(value, symbol, maxDepth)
                    : behaviorPath(value, symbol, maxDepth);
            return addPathKind(result, pathKind);
        } catch (Exception exception) {
            return ToolResult.error("graph_unavailable: " + exception.getMessage());
        }
    }

    private static ToolResult behaviorPath(
            ProjectSnapshot value,
            String symbol,
            int maxDepth
    ) {
        SourceSet sourceScope = GraphToolSupport.sourceScope(value, symbol);
        Set<String> frontier = new LinkedHashSet<>(Set.of(symbol));
        Set<String> visited = new LinkedHashSet<>();
        List<GraphEdge> relationships = new ArrayList<>();
        for (int depth = 0; depth < maxDepth && !frontier.isEmpty(); depth++) {
            Set<String> next = new LinkedHashSet<>();
            for (String current : frontier) {
                if (!visited.add(current)) {
                    continue;
                }
                relationships.addAll(value.graph().outgoing(current, GraphEdgeKind.CALLS));
                relationships.addAll(value.graph().outgoing(current, GraphEdgeKind.READS_FIELD));
                relationships.addAll(value.graph().outgoing(current, GraphEdgeKind.WRITES_FIELD));
                relationships.addAll(value.graph().outgoing(current, GraphEdgeKind.OVERRIDES));
                relationships.addAll(value.graph().outgoing(current, GraphEdgeKind.IMPLEMENTS));
                relationships.addAll(value.graph().incoming(current, GraphEdgeKind.OVERRIDES));
                relationships.addAll(value.graph().incoming(current, GraphEdgeKind.IMPLEMENTS));
                relationships.addAll(value.graph().incoming(current, GraphEdgeKind.EXPOSES_ROUTE));
                relationships.addAll(value.graph().incoming(current, GraphEdgeKind.LISTENS_TO_EVENT));
                relationships.addAll(value.graph().incoming(current, GraphEdgeKind.SCHEDULED_BY));
                value.graph().outgoing(current, GraphEdgeKind.CALLS).stream()
                        .filter(edge -> edge.resolution() == ResolutionStatus.RESOLVED)
                        .filter(edge -> GraphToolSupport.inScope(edge, sourceScope))
                        .map(GraphEdge::targetId)
                        .forEach(next::add);
            }
            frontier = next;
        }
        List<GraphNode> nodes = nodesFor(value, symbol, relationships);
        return GraphToolSupport.facts(
                value, symbol, nodes, relationships, List.of(), true, sourceScope);
    }

    private static ToolResult securityPath(ProjectSnapshot value, String symbol, int maxDepth) {
        SourceSet sourceScope = GraphToolSupport.sourceScope(value, symbol);
        List<GraphEdge> relationships = new ArrayList<>();
        int suppressedUnresolvedCount = 0;
        GraphNodeKind kind = value.graph().node(symbol).map(GraphNode::kind).orElse(null);
        if (kind == GraphNodeKind.FIELD) {
            relationships.addAll(value.graph().incoming(symbol, GraphEdgeKind.READS_FIELD));
            relationships.addAll(value.graph().incoming(symbol, GraphEdgeKind.WRITES_FIELD));
        } else if (kind == GraphNodeKind.TYPE) {
            suppressedUnresolvedCount = collectSensitiveCalls(
                    value, internalMethods(value, symbol), sourceScope, relationships, maxDepth);
            relationships.addAll(value.graph().incoming(symbol, GraphEdgeKind.EXTENDS));
            relationships.addAll(value.graph().incoming(symbol, GraphEdgeKind.IMPLEMENTS));
        } else {
            relationships.addAll(value.graph().incoming(symbol, GraphEdgeKind.EXPOSES_ROUTE));
            relationships.addAll(value.graph().incoming(symbol, GraphEdgeKind.LISTENS_TO_EVENT));
            suppressedUnresolvedCount = collectSensitiveCalls(
                    value, Set.of(symbol), sourceScope, relationships, maxDepth);
        }
        List<String> limits = new ArrayList<>(relationships.stream()
                .filter(edge -> GraphToolSupport.inScope(edge, sourceScope))
                .filter(edge -> edge.resolution() == ResolutionStatus.UNRESOLVED)
                .map(edge -> "unresolved_call:" + edge.targetId())
                .toList());
        value.graph().node(symbol)
                .filter(node -> node.kind() == GraphNodeKind.FIELD)
                .map(GraphNode::signature)
                .map(InspectPathTool::fieldType)
                .filter(InspectPathTool::sensitive)
                .ifPresent(type -> limits.add("field_type_sensitive: " + type));
        return GraphToolSupport.facts(
                value,
                symbol,
                nodesFor(value, symbol, relationships),
                relationships,
                limits,
                false,
                sourceScope,
                suppressedUnresolvedCount);
    }

    private static List<GraphNode> nodesFor(
            ProjectSnapshot value,
            String symbol,
            List<GraphEdge> relationships
    ) {
        List<GraphNode> nodes = new ArrayList<>();
        value.graph().node(symbol).ifPresent(nodes::add);
        relationships.stream()
                .flatMap(edge -> java.util.stream.Stream.of(
                        value.graph().node(edge.sourceId()), value.graph().node(edge.targetId())))
                .flatMap(java.util.Optional::stream)
                .forEach(nodes::add);
        return nodes;
    }

    private static int collectSensitiveCalls(
            ProjectSnapshot value,
            Set<String> frontier,
            SourceSet sourceScope,
            List<GraphEdge> relationships,
            int maxDepth
    ) {
        Set<String> visited = new LinkedHashSet<>();
        int suppressedUnresolvedCount = 0;
        for (int depth = 0; depth < maxDepth && !frontier.isEmpty(); depth++) {
            Set<String> next = new LinkedHashSet<>();
            for (String current : frontier) {
                if (!visited.add(current)) {
                    continue;
                }
                for (GraphEdge edge : value.graph().outgoing(current, GraphEdgeKind.CALLS)) {
                    if (!GraphToolSupport.inScope(edge, sourceScope)) {
                        relationships.add(edge);
                        continue;
                    }
                    boolean resolved = edge.resolution() == ResolutionStatus.RESOLVED;
                    if (sensitive(edge.targetId())) {
                        relationships.add(edge);
                    } else if (!resolved) {
                        suppressedUnresolvedCount++;
                    }
                    if (resolved) {
                        next.add(edge.targetId());
                    }
                }
            }
            frontier = next;
        }
        return suppressedUnresolvedCount;
    }

    private static Set<String> internalMethods(ProjectSnapshot value, String typeId) {
        Set<String> methods = new LinkedHashSet<>();
        GraphNode type = value.graph().node(typeId).orElse(null);
        if (type == null) {
            return methods;
        }
        value.graph().symbolsInFile(type.file()).stream()
                .filter(node -> typeId.equals(node.ownerId()))
                .filter(node -> node.kind() == GraphNodeKind.METHOD
                        || node.kind() == GraphNodeKind.CONSTRUCTOR)
                .forEach(node -> methods.add(node.id()));
        return methods;
    }

    private static String fieldType(String signature) {
        int separator = signature.lastIndexOf(' ');
        return separator > 0 ? signature.substring(0, separator) : signature;
    }

    private static boolean sensitive(String symbol) {
        String lower = symbol.toLowerCase(Locale.ROOT);
        return SENSITIVE_TERMS.stream().anyMatch(lower::contains);
    }

    private static ToolResult addPathKind(ToolResult result, String pathKind) {
        if (!result.isSuccess()) {
            return result;
        }
        try {
            JsonNode payload = GraphToolSupport.JSON.readTree(result.getResult());
            ((com.fasterxml.jackson.databind.node.ObjectNode) payload).put("path_kind", pathKind);
            return ToolResult.ok(GraphToolSupport.JSON.writeValueAsString(payload));
        } catch (Exception exception) {
            return ToolResult.error("graph_result_error: " + exception.getMessage());
        }
    }
}
