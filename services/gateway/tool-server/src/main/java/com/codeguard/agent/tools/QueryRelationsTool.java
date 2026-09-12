package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.GraphEdge;
import com.codeguard.agent.graph.GraphEdgeKind;
import com.codeguard.agent.graph.GraphNode;
import com.codeguard.agent.graph.ProjectSnapshot;
import com.codeguard.agent.graph.ProjectSnapshotProvider;
import com.codeguard.agent.graph.ResolutionStatus;
import com.codeguard.agent.graph.SourceSet;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * Unified, typed relation query used by the controlled reviewer.
 *
 * <p>The operation intentionally exposes relation intent instead of a free-form
 * graph query.  A request expands one relation family from one subject; callers
 * can continue the same page with {@code cursor} or explicitly expand a symbol
 * returned by a previous page.</p>
 */
public final class QueryRelationsTool implements AgentTool {
    private static final int DEFAULT_DEPTH = 1;
    private static final int MAX_DEPTH = 3;
    private static final int DEFAULT_LIMIT = 20;
    private static final int MAX_LIMIT = 200;
    private static final Set<String> SUPPORTED_RELATIONS = Set.of(
            "callers", "callees", "field_readers", "field_writers",
            "implementations", "overrides", "parents", "children",
            "type_users", "type_references", "entrypoints");

    private final ProjectSnapshotProvider snapshot;

    public QueryRelationsTool(ProjectSnapshotProvider snapshot) {
        this.snapshot = snapshot;
    }

    @Override
    public String name() {
        return "query_relations";
    }

    @Override
    public String description() {
        return "查询已知 symbol 的指定项目关系。relation 只能是 callers、callees、field_readers、"
                + "field_writers、implementations、overrides、parents、children、type_users、"
                + "type_references 或 entrypoints；默认一跳，depth 最大 3，超限使用 cursor 续取。"
                + "callers/callees 查询方法调用，field_readers/field_writers 查询字段读写，"
                + "implementations/overrides 查询实现或覆写，parents/children 查询继承层级，"
                + "type_users/type_references 查询类型使用方或类型引用，entrypoints 查询路由、事件和定时入口。"
                + "parents/type_references 沿边正向查找，children/type_users/entrypoints 沿边反向查找；"
                + "parents/children/type_users 需要 TYPE，type_references 需要 TYPE、METHOD、CONSTRUCTOR 或 FIELD，"
                + "entrypoints 需要 METHOD 或 CONSTRUCTOR。返回精确端点、调用位置、解析状态和覆盖信息，"
                + "不接受任意图查询或自行编造 symbol_id。";
    }

    @Override
    public ToolResult execute(String input, AgentContext context) {
        final JsonNode query;
        try {
            query = GraphToolSupport.JSON.readTree(input == null ? "" : input);
        } catch (Exception exception) {
            return ToolResult.error("invalid_relation_query");
        }
        if (query == null || !query.isObject()) {
            return ToolResult.error("invalid_relation_query");
        }
        String requested = query.path("subject_symbol_id")
                .asText(query.path("symbol_id").asText(""))
                .trim();
        String relation = query.path("relation").asText("").trim().toLowerCase(Locale.ROOT);
        if (requested.isBlank()) {
            return ToolResult.error("missing_subject_symbol_id");
        }
        if (!SUPPORTED_RELATIONS.contains(relation)) {
            return ToolResult.error("unsupported_relation: " + relation);
        }
        int depth = query.path("depth").asInt(query.path("max_depth").asInt(DEFAULT_DEPTH));
        int limit = query.path("limit").asInt(DEFAULT_LIMIT);
        int cursor = query.path("cursor").asInt(0);
        boolean includeCallsite = query.path("include_callsite").asBoolean(true);
        boolean includeContext = query.path("include_context").asBoolean(true);
        if (depth < 1 || depth > MAX_DEPTH) {
            return ToolResult.error("invalid_depth");
        }
        if (limit < 1 || limit > MAX_LIMIT || cursor < 0) {
            return ToolResult.error("invalid_page");
        }
        try {
            ProjectSnapshot value = snapshot.load(name(), input);
            String subject = GraphToolSupport.canonicalSymbol(value, requested);
            if (value.graph().node(subject).isEmpty()) {
                return ToolResult.error("symbol_not_found: " + subject);
            }
            var kind = value.graph().node(subject).orElseThrow().kind();
            if ((relation.equals("field_readers") || relation.equals("field_writers"))
                    && kind != com.codeguard.agent.graph.GraphNodeKind.FIELD) {
                return ToolResult.error("invalid_relation_subject: " + relation
                        + " requires FIELD, got " + kind
                        + "; use a resolved field symbol, not its containing method or type");
            }
            if ((relation.equals("callers") || relation.equals("callees"))
                    && kind != com.codeguard.agent.graph.GraphNodeKind.METHOD
                    && kind != com.codeguard.agent.graph.GraphNodeKind.CONSTRUCTOR) {
                return ToolResult.error("invalid_relation_subject: " + relation
                        + " requires METHOD or CONSTRUCTOR, got " + kind);
            }
            if ((relation.equals("parents") || relation.equals("children")
                    || relation.equals("type_users"))
                    && kind != com.codeguard.agent.graph.GraphNodeKind.TYPE) {
                return ToolResult.error("invalid_relation_subject: " + relation
                        + " requires TYPE, got " + kind);
            }
            if (relation.equals("type_references")
                    && kind != com.codeguard.agent.graph.GraphNodeKind.TYPE
                    && kind != com.codeguard.agent.graph.GraphNodeKind.METHOD
                    && kind != com.codeguard.agent.graph.GraphNodeKind.CONSTRUCTOR
                    && kind != com.codeguard.agent.graph.GraphNodeKind.FIELD) {
                return ToolResult.error("invalid_relation_subject: " + relation
                        + " requires TYPE, METHOD, CONSTRUCTOR or FIELD, got " + kind);
            }
            if (relation.equals("entrypoints")
                    && kind != com.codeguard.agent.graph.GraphNodeKind.METHOD
                    && kind != com.codeguard.agent.graph.GraphNodeKind.CONSTRUCTOR) {
                return ToolResult.error("invalid_relation_subject: " + relation
                        + " requires METHOD or CONSTRUCTOR, got " + kind);
            }
            SourceSet scope = GraphToolSupport.sourceScope(value, subject);
            List<GraphEdge> edges = collect(value, subject, relation, depth, scope);
            List<GraphNode> nodes = nodesFor(value, subject, edges);
            ToolResult facts = GraphToolSupport.facts(
                    value,
                    subject,
                    nodes,
                    edges,
                    List.of("relation_query:" + relation),
                    false,
                    scope,
                    0,
                    new GraphToolSupport.QueryOptions(depth, limit, cursor));
            return enrich(facts, value, relation, includeCallsite, includeContext);
        } catch (Exception exception) {
            return ToolResult.error("graph_unavailable: " + exception.getMessage());
        }
    }

    private static List<GraphEdge> collect(
            ProjectSnapshot value,
            String subject,
            String relation,
            int depth,
            SourceSet scope
    ) {
        Set<String> frontier = new LinkedHashSet<>(Set.of(subject));
        Set<String> visited = new LinkedHashSet<>();
        List<GraphEdge> result = new ArrayList<>();
        for (int level = 0; level < depth && !frontier.isEmpty(); level++) {
            Set<String> next = new LinkedHashSet<>();
            for (String current : frontier) {
                if (!visited.add(current)) {
                    continue;
                }
                List<GraphEdge> edges = matching(value, current, relation);
                if (relation.equals("callers")) {
                    edges = new ArrayList<>(edges);
                    edges.addAll(GraphToolSupport.potentialUnresolvedCallers(value, current, scope));
                }
                result.addAll(edges);
                edges.stream()
                        .filter(edge -> edge.sourceSet() == scope)
                        .filter(edge -> edge.resolution() == ResolutionStatus.RESOLVED)
                        .map(edge -> nextSubject(current, relation, edge))
                        .forEach(next::add);
            }
            frontier = next;
        }
        return result;
    }

    private static List<GraphEdge> matching(ProjectSnapshot value, String subject, String relation) {
        return switch (relation) {
            case "callers" -> value.graph().incoming(subject, GraphEdgeKind.CALLS);
            case "callees" -> value.graph().outgoing(subject, GraphEdgeKind.CALLS);
            case "field_readers" -> value.graph().incoming(subject, GraphEdgeKind.READS_FIELD);
            case "field_writers" -> value.graph().incoming(subject, GraphEdgeKind.WRITES_FIELD);
            case "implementations" -> value.graph().incoming(subject, GraphEdgeKind.IMPLEMENTS);
            case "parents" -> value.graph().outgoing(subject, GraphEdgeKind.EXTENDS);
            case "children" -> value.graph().incoming(subject, GraphEdgeKind.EXTENDS);
            case "type_users" -> value.graph().incoming(subject, GraphEdgeKind.REFERENCES_TYPE);
            case "type_references" -> value.graph().outgoing(subject, GraphEdgeKind.REFERENCES_TYPE);
            case "entrypoints" -> {
                List<GraphEdge> edges = new ArrayList<>();
                edges.addAll(value.graph().incoming(subject, GraphEdgeKind.EXPOSES_ROUTE));
                edges.addAll(value.graph().incoming(subject, GraphEdgeKind.LISTENS_TO_EVENT));
                edges.addAll(value.graph().incoming(subject, GraphEdgeKind.SCHEDULED_BY));
                yield edges;
            }
            case "overrides" -> {
                List<GraphEdge> edges = new ArrayList<>();
                edges.addAll(value.graph().incoming(subject, GraphEdgeKind.OVERRIDES));
                edges.addAll(value.graph().outgoing(subject, GraphEdgeKind.OVERRIDES));
                yield edges;
            }
            default -> List.of();
        };
    }

    private static String nextSubject(String current, String relation, GraphEdge edge) {
        if (relation.equals("overrides")) {
            return edge.sourceId().equals(current) ? edge.targetId() : edge.sourceId();
        }
        if (Set.of("callers", "field_readers", "field_writers", "implementations",
                "children", "type_users", "entrypoints").contains(relation)) {
            return edge.sourceId();
        }
        return edge.targetId();
    }

    private static List<GraphNode> nodesFor(
            ProjectSnapshot value,
            String subject,
            List<GraphEdge> edges
    ) {
        List<GraphNode> nodes = new ArrayList<>();
        value.graph().node(subject).ifPresent(nodes::add);
        edges.stream()
                .flatMap(edge -> java.util.stream.Stream.of(
                        value.graph().node(edge.sourceId()), value.graph().node(edge.targetId())))
                .flatMap(java.util.Optional::stream)
                .forEach(nodes::add);
        return nodes;
    }

    private static ToolResult enrich(
            ToolResult result,
            ProjectSnapshot value,
            String relation,
            boolean includeCallsite,
            boolean includeContext
    ) {
        if (!result.isSuccess()) {
            return result;
        }
        try {
            ObjectNode root = (ObjectNode) GraphToolSupport.JSON.readTree(result.getResult());
            root.put("tool", "query_relations");
            root.put("relation", relation);
            JsonNode relationships = root.get("relationships");
            if (relationships != null && relationships.isArray()) {
                for (JsonNode edge : relationships) {
                    if (!edge.isObject()) {
                        continue;
                    }
                    String file = edge.path("file").asText("");
                    int line = edge.path("line").asInt(0);
                    if (includeCallsite) {
                        ObjectNode object = (ObjectNode) edge;
                        ObjectNode callsite = object.putObject("callsite");
                        callsite.put("file", file);
                        callsite.put("line", line);
                        callsite.put("resolved", edge.path("resolution").asText("").equals("RESOLVED"));
                        if (includeContext) {
                            String source = value.sources().get(file);
                            if (source != null && line > 0) {
                                callsite.put("context", lineContext(source, line));
                            }
                        }
                    }
                }
            }
            if (includeContext) {
                addEndpointSource(root, value);
            }
            return ToolResult.ok(GraphToolSupport.JSON.writeValueAsString(root));
        } catch (Exception exception) {
            return ToolResult.error("graph_result_error: " + exception.getMessage());
        }
    }

    /** Source facts only: bounded excerpts of direct endpoints on this page. */
    private static void addEndpointSource(ObjectNode root, ProjectSnapshot value) {
        String subject = root.path("subject_symbol_id").asText();
        java.util.Map<String, Integer> endpoints = new java.util.LinkedHashMap<>();
        for (JsonNode edge : root.path("relationships")) {
            if (!edge.path("resolution").asText().equals("RESOLVED")) continue;
            String from = edge.path("sourceId").asText();
            String to = edge.path("targetId").asText();
            // Callers/readers/writers: center on their use of the subject.
            // Callees/parent declarations: begin at the endpoint declaration.
            if (subject.equals(to) && !subject.equals(from)) {
                endpoints.putIfAbsent(from, edge.path("line").asInt());
            } else if (subject.equals(from) && !subject.equals(to)) {
                endpoints.putIfAbsent(to, 0);
            }
        }
        int included = 0;
        for (JsonNode symbol : root.path("symbols")) {
            String id = symbol.path("id").asText();
            if (included >= 3 || !endpoints.containsKey(id)
                    || !symbol.path("source_set").asText().equals(root.path("source_scope").asText())) continue;
            String source = value.sources().get(symbol.path("file").asText());
            if (source == null) continue;
            String[] lines = source.split("\\R", -1);
            int first = symbol.path("startLine").asInt();
            int last = symbol.path("endLine").asInt();
            if (first < 1 || last < first || last > lines.length) continue;
            int useLine = endpoints.get(id);
            int start = useLine >= first && useLine <= last ? Math.max(first, useLine - 6) : first;
            int end = start - 1;
            StringBuilder text = new StringBuilder();
            for (int line = start; line <= Math.min(last, start + 23); line++) {
                String next = lines[line - 1] + "\n";
                if (text.length() + next.length() > 1000) break;
                text.append(next);
                end = line;
            }
            if (text.isEmpty()) continue;
            ObjectNode excerpt = ((ObjectNode) symbol).putObject("source_excerpt");
            excerpt.put("start_line", start);
            excerpt.put("end_line", end);
            excerpt.put("text", text.toString());
            excerpt.put("truncated", start > first || end < last);
            if (end < last) excerpt.put("next_cursor", end + 1);
            included++;
        }
        root.put("omitted_source_excerpt_count", endpoints.size() - included);
    }

    private static String lineContext(String source, int line) {
        String[] lines = source.split("\\R", -1);
        if (line <= 0 || line > lines.length) {
            return "";
        }
        return lines[line - 1].trim();
    }
}
