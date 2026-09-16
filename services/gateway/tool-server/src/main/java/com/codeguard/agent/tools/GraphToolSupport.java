package com.codeguard.agent.tools;

import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.GraphEdge;
import com.codeguard.agent.graph.GraphEdgeKind;
import com.codeguard.agent.graph.GraphNode;
import com.codeguard.agent.graph.ProjectSnapshot;
import com.codeguard.agent.graph.ResolutionStatus;
import com.codeguard.agent.graph.SourceSet;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

final class GraphToolSupport {
    static final ObjectMapper JSON = new ObjectMapper();
    private static final long DEFAULT_BUILD_TIMEOUT_SECONDS = 120;
    private static final long BUILD_TIMEOUT_SECONDS = configuredBuildTimeoutSeconds();
    private static final int MAX_SYMBOLS = 100;
    private static final int MAX_RELATIONSHIPS = 200;
    private static final int MAX_UNRESOLVED_RELATIONSHIPS = 20;
    private static final int SCHEMA_VERSION = 2;

    /** 增量查询使用的可选分页和范围参数；省略时采用默认限制。 */
    record QueryOptions(int maxDepth, int limit, int cursor) {
        static QueryOptions legacy() {
            return new QueryOptions(0, MAX_RELATIONSHIPS, 0);
        }
    }

    private GraphToolSupport() {}

    private static long configuredBuildTimeoutSeconds() {
        String raw = System.getenv("CODEGUARD_GRAPH_BUILD_TIMEOUT_SECONDS");
        if (raw == null || raw.isBlank()) {
            return DEFAULT_BUILD_TIMEOUT_SECONDS;
        }
        try {
            long value = Long.parseLong(raw.trim());
            return value > 0 ? value : DEFAULT_BUILD_TIMEOUT_SECONDS;
        } catch (NumberFormatException ignored) {
            return DEFAULT_BUILD_TIMEOUT_SECONDS;
        }
    }

    static ProjectSnapshot await(CompletableFuture<ProjectSnapshot> future) throws Exception {
        return future.get(BUILD_TIMEOUT_SECONDS, TimeUnit.SECONDS);
    }

    static String symbolId(String input) {
        String value = input == null ? "" : input.trim();
        if (!value.startsWith("{")) {
            return value;
        }
        try {
            JsonNode root = JSON.readTree(value);
            return root.path("symbol_id").asText(root.path("subject").asText(""));
        } catch (Exception ignored) {
            return "";
        }
    }

    static SourceSet sourceScope(ProjectSnapshot snapshot, String subject) {
        return snapshot.graph().node(subject)
                .map(GraphNode::sourceSet)
                .orElse(SourceSet.MAIN);
    }

    static QueryOptions queryOptions(String input) {
        if (input == null || input.isBlank() || !input.trim().startsWith("{")) {
            return QueryOptions.legacy();
        }
        try {
            JsonNode root = JSON.readTree(input);
            int depth = positiveOrZero(root.path("max_depth").asInt(0), 3);
            int limit = positiveOrZero(root.path("limit").asInt(MAX_RELATIONSHIPS), MAX_RELATIONSHIPS);
            int cursor = positiveOrZero(root.path("cursor").asInt(0), 0);
            return new QueryOptions(Math.min(depth, 3), Math.min(limit, MAX_RELATIONSHIPS), cursor);
        } catch (Exception ignored) {
            return QueryOptions.legacy();
        }
    }

    private static int positiveOrZero(int value, int fallback) {
        return value < 0 ? fallback : value;
    }

    /** 统一 JavaParser 不同解析路径产生的签名空白差异，以懒索引中的符号标识为准。 */
    static String canonicalSymbol(ProjectSnapshot snapshot, String requested) {
        if (requested == null || requested.isBlank()
                || snapshot.graph().node(requested).isPresent()) {
            return requested == null ? "" : requested;
        }
        String compact = comparableSymbolId(requested);
        return snapshot.graph().nodes().stream()
                .filter(node -> comparableSymbolId(node.id()).equals(compact))
                .map(GraphNode::id)
                .findFirst()
                .orElse(requested);
    }

    private static String comparableSymbolId(String value) {
        if (!value.startsWith("java:")) {
            return value;
        }
        return value.replaceAll("\\s+", "");
    }

    static boolean inScope(GraphEdge edge, SourceSet sourceScope) {
        return edge.sourceSet() == sourceScope;
    }

    /** 解析源码工具使用的严格符号查询参数。 */
    static String symbolIdOnly(String input) {
        if (input == null || input.isBlank()) {
            return "";
        }
        try {
            JsonNode root = JSON.readTree(input);
            if (root == null || !root.isObject()) {
                return "";
            }
            JsonNode symbol = root.get("symbol_id");
            return symbol != null && symbol.isTextual() ? symbol.asText().trim() : "";
        } catch (Exception ignored) {
            return "";
        }
    }

    static List<GraphEdge> potentialUnresolvedCallers(
            ProjectSnapshot snapshot,
            String subject,
            SourceSet sourceScope
    ) {
        String unresolvedTarget = unresolvedMethodTarget(subject);
        if (unresolvedTarget.isBlank()) {
            return List.of();
        }
        return snapshot.graph().edges().stream()
                .filter(edge -> edge.sourceSet() == sourceScope)
                .filter(edge -> edge.kind() == GraphEdgeKind.CALLS)
                .filter(edge -> edge.resolution() != ResolutionStatus.RESOLVED)
                .filter(edge -> unresolvedTarget.equals(edge.targetId()))
                .toList();
    }

    static ToolResult facts(
            ProjectSnapshot snapshot,
            String subject,
            Collection<GraphNode> nodes,
            Collection<GraphEdge> edges,
            List<String> limitations
    ) {
        return facts(snapshot, subject, nodes, edges, limitations, false);
    }

    static ToolResult facts(
            ProjectSnapshot snapshot,
            String subject,
            Collection<GraphNode> nodes,
            Collection<GraphEdge> edges,
            List<String> limitations,
            boolean subjectAloneIsFact
    ) {
        return facts(
                snapshot,
                subject,
                nodes,
                edges,
                limitations,
                subjectAloneIsFact,
                sourceScope(snapshot, subject),
                0);
    }

    static ToolResult facts(
            ProjectSnapshot snapshot,
            String subject,
            Collection<GraphNode> nodes,
            Collection<GraphEdge> edges,
            List<String> limitations,
            boolean subjectAloneIsFact,
            SourceSet sourceScope
    ) {
        return facts(
                snapshot,
                subject,
                nodes,
                edges,
                limitations,
                subjectAloneIsFact,
                sourceScope,
                0);
    }

    static ToolResult facts(
            ProjectSnapshot snapshot,
            String subject,
            Collection<GraphNode> nodes,
            Collection<GraphEdge> edges,
            List<String> limitations,
            boolean subjectAloneIsFact,
            SourceSet sourceScope,
            int suppressedUnresolvedCount,
            QueryOptions options
    ) {
        try {
            ObjectNode root = JSON.createObjectNode();
            List<GraphNode> uniqueNodes = uniqueNodes(nodes);
            List<GraphNode> primaryNodes = uniqueNodes.stream()
                    .filter(node -> node.sourceSet() == sourceScope)
                    .toList();
            List<GraphEdge> primaryEdges = edges.stream()
                    .filter(edge -> edge.sourceSet() == sourceScope)
                    .toList();
            List<GraphEdge> resolvedPrimaryEdges = resolved(primaryEdges);
            List<GraphEdge> unresolvedPrimaryEdges = unresolved(primaryEdges);
            int totalUnresolvedCount = unresolvedPrimaryEdges.size()
                    + Math.max(0, suppressedUnresolvedCount);
            int start = Math.min(Math.max(0, options.cursor()), resolvedPrimaryEdges.size());
            int requestedLimit = options.limit() <= 0 ? MAX_RELATIONSHIPS : options.limit();
            int end = Math.min(resolvedPrimaryEdges.size(), start + requestedLimit);
            List<GraphEdge> pageEdges = resolvedPrimaryEdges.subList(start, end);
            List<GraphNode> boundedPrimaryNodes = primaryNodes.stream().limit(MAX_SYMBOLS).toList();
            List<GraphEdge> boundedUnresolvedEdges = unresolvedPrimaryEdges.stream()
                    .limit(MAX_UNRESOLVED_RELATIONSHIPS).toList();
            boolean pageTruncated = start > 0 || end < resolvedPrimaryEdges.size();
            boolean nodeTruncated = boundedPrimaryNodes.size() < primaryNodes.size();
            boolean subjectExists = snapshot.graph().node(subject).isPresent();
            boolean found = !pageEdges.isEmpty()
                    || (subjectAloneIsFact && !primaryNodes.isEmpty());
            List<String> queryDiagnostics = queryDiagnostics(snapshot, subject, sourceScope);
            boolean completeCoverage = subjectExists && !pageTruncated && !nodeTruncated
                    && totalUnresolvedCount == 0 && queryDiagnostics.isEmpty();
            String outcome = found ? "found" : (completeCoverage ? "not_found" : "indeterminate");
            root.put("schema_version", SCHEMA_VERSION);
            root.put("outcome", outcome);
            root.put("coverage", completeCoverage ? "complete" : "partial");
            root.put("source_scope", sourceScope.name());
            root.put("snapshot_production_coverage", snapshot.productionComplete() ? "complete" : "partial");
            root.put("snapshot_main_coverage", snapshot.coverageStatus(SourceSet.MAIN));
            root.put("snapshot_test_coverage", snapshot.coverageStatus(SourceSet.TEST));
            root.put("snapshot_generated_coverage", snapshot.coverageStatus(SourceSet.GENERATED));
            root.put("subject_symbol_id", subject);
            root.set("symbols", JSON.valueToTree(boundedPrimaryNodes));
            root.set("relationships", JSON.valueToTree(pageEdges));
            root.set("unresolved_relationships", JSON.valueToTree(boundedUnresolvedEdges));
            root.put("unresolved_count", totalUnresolvedCount);
            root.put("cursor", start);
            if (end < resolvedPrimaryEdges.size()) {
                root.put("next_cursor", end);
            } else {
                root.putNull("next_cursor");
            }
            ArrayNode allLimitations = root.putArray("limitations");
            queryDiagnostics.forEach(allLimitations::add);
            limitations.forEach(allLimitations::add);
            if (!subjectExists) allLimitations.add("subject_not_found");
            if (totalUnresolvedCount > 0) allLimitations.add("unresolved_relationships:" + totalUnresolvedCount);
            if (suppressedUnresolvedCount > 0) {
                allLimitations.add("unresolved_relationships_suppressed:" + suppressedUnresolvedCount);
            }
            if (pageTruncated || nodeTruncated) allLimitations.add("result_truncated");
            return ToolResult.ok(JSON.writeValueAsString(root));
        } catch (Exception exception) {
            return ToolResult.error("graph_result_error: " + exception.getMessage());
        }
    }

    static ToolResult facts(
            ProjectSnapshot snapshot,
            String subject,
            Collection<GraphNode> nodes,
            Collection<GraphEdge> edges,
            List<String> limitations,
            boolean subjectAloneIsFact,
            SourceSet sourceScope,
            int suppressedUnresolvedCount
    ) {
        try {
            ObjectNode root = JSON.createObjectNode();
            List<GraphNode> uniqueNodes = uniqueNodes(nodes);
            List<GraphNode> primaryNodes = uniqueNodes.stream()
                    .filter(node -> node.sourceSet() == sourceScope)
                    .toList();
            List<GraphEdge> primaryEdges = edges.stream()
                    .filter(edge -> edge.sourceSet() == sourceScope)
                    .toList();
            List<GraphEdge> resolvedPrimaryEdges = resolved(primaryEdges);
            List<GraphEdge> unresolvedPrimaryEdges = unresolved(primaryEdges);
            int totalUnresolvedCount = unresolvedPrimaryEdges.size()
                    + Math.max(0, suppressedUnresolvedCount);

            List<GraphNode> boundedPrimaryNodes =
                    primaryNodes.stream().limit(MAX_SYMBOLS).toList();
            List<GraphEdge> boundedPrimaryEdges =
                    resolvedPrimaryEdges.stream().limit(MAX_RELATIONSHIPS).toList();
            List<GraphEdge> boundedUnresolvedEdges = unresolvedPrimaryEdges.stream()
                    .limit(MAX_UNRESOLVED_RELATIONSHIPS)
                    .toList();
            boolean primaryTruncated = boundedPrimaryNodes.size() < primaryNodes.size()
                    || boundedPrimaryEdges.size() < resolvedPrimaryEdges.size();
            boolean subjectExists = snapshot.graph().node(subject).isPresent();
            boolean found = !resolvedPrimaryEdges.isEmpty()
                    || (subjectAloneIsFact && !primaryNodes.isEmpty());
            List<String> queryDiagnostics = queryDiagnostics(
                    snapshot, subject, sourceScope);
            boolean completeCoverage = subjectExists
                    && !primaryTruncated
                    && totalUnresolvedCount == 0
                    && queryDiagnostics.isEmpty();
            String outcome = found
                    ? "found"
                    : (completeCoverage ? "not_found" : "indeterminate");
            root.put("schema_version", SCHEMA_VERSION);
            root.put("outcome", outcome);
            root.put("coverage", completeCoverage ? "complete" : "partial");
            root.put("source_scope", sourceScope.name());
            root.put("snapshot_production_coverage",
                    snapshot.productionComplete() ? "complete" : "partial");
            root.put("snapshot_main_coverage", snapshot.coverageStatus(SourceSet.MAIN));
            root.put("snapshot_test_coverage", snapshot.coverageStatus(SourceSet.TEST));
            root.put("snapshot_generated_coverage",
                    snapshot.coverageStatus(SourceSet.GENERATED));
            root.put("subject_symbol_id", subject);
            root.set("symbols", JSON.valueToTree(boundedPrimaryNodes));
            root.set("relationships", JSON.valueToTree(boundedPrimaryEdges));
            root.set("unresolved_relationships", JSON.valueToTree(boundedUnresolvedEdges));
            root.put("unresolved_count", totalUnresolvedCount);
            ArrayNode allLimitations = root.putArray("limitations");
            queryDiagnostics.forEach(allLimitations::add);
            limitations.forEach(allLimitations::add);
            if (!subjectExists) {
                allLimitations.add("subject_not_found");
            }
            if (totalUnresolvedCount > 0) {
                allLimitations.add("unresolved_relationships:" + totalUnresolvedCount);
            }
            if (suppressedUnresolvedCount > 0) {
                allLimitations.add(
                        "unresolved_relationships_suppressed:"
                                + suppressedUnresolvedCount);
            }
            if (primaryTruncated) {
                allLimitations.add("result_truncated");
            }
            return ToolResult.ok(JSON.writeValueAsString(root));
        } catch (Exception exception) {
            return ToolResult.error("graph_result_error: " + exception.getMessage());
        }
    }

    private static List<GraphNode> uniqueNodes(Collection<GraphNode> nodes) {
        Map<String, GraphNode> byId = new LinkedHashMap<>();
        nodes.forEach(node -> byId.putIfAbsent(node.id(), node));
        return List.copyOf(byId.values());
    }

    private static List<GraphEdge> resolved(Collection<GraphEdge> edges) {
        return edges.stream()
                .filter(edge -> edge.resolution() == ResolutionStatus.RESOLVED)
                .toList();
    }

    private static List<GraphEdge> unresolved(Collection<GraphEdge> edges) {
        return edges.stream()
                .filter(edge -> edge.resolution() != ResolutionStatus.RESOLVED)
                .toList();
    }

    private static List<String> queryDiagnostics(
            ProjectSnapshot snapshot,
            String subject,
            SourceSet sourceScope
    ) {
        String subjectFile = snapshot.graph().node(subject)
                .map(GraphNode::file)
                .orElse("");
        return snapshot.diagnosticsFor(sourceScope).stream()
                .filter(diagnostic -> diagnostic.startsWith("scan_failed: ")
                        || (!subjectFile.isBlank()
                        && diagnostic.startsWith(subjectFile + ":")))
                .toList();
    }

    private static String unresolvedMethodTarget(String subject) {
        int ownerSeparator = subject.indexOf('#');
        int parametersStart = subject.indexOf('(', ownerSeparator + 1);
        int parametersEnd = subject.lastIndexOf(')');
        if (ownerSeparator < 0 || parametersStart < 0 || parametersEnd < parametersStart) {
            return "";
        }
        String method = subject.substring(ownerSeparator + 1, parametersStart);
        String parameters = subject.substring(parametersStart + 1, parametersEnd).trim();
        int arity = parameters.isEmpty() ? 0 : topLevelParameterCount(parameters);
        return "unresolved:method:" + method + "/" + arity;
    }

    private static int topLevelParameterCount(String parameters) {
        int count = 1;
        int genericDepth = 0;
        for (int index = 0; index < parameters.length(); index++) {
            char current = parameters.charAt(index);
            if (current == '<') {
                genericDepth++;
            } else if (current == '>') {
                genericDepth = Math.max(0, genericDepth - 1);
            } else if (current == ',' && genericDepth == 0) {
                count++;
            }
        }
        return count;
    }
}
