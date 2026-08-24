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
    private static final long BUILD_TIMEOUT_SECONDS = 120;
    private static final int MAX_SYMBOLS = 100;
    private static final int MAX_RELATIONSHIPS = 200;
    private static final int MAX_UNRESOLVED_RELATIONSHIPS = 20;
    private static final int SCHEMA_VERSION = 2;

    private GraphToolSupport() {}

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

    static boolean inScope(GraphEdge edge, SourceSet sourceScope) {
        return edge.sourceSet() == sourceScope;
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
            int suppressedUnresolvedCount
    ) {
        try {
            ObjectNode root = JSON.createObjectNode();
            List<GraphNode> uniqueNodes = uniqueNodes(nodes);
            List<GraphNode> mainNodes = uniqueNodes.stream()
                    .filter(node -> node.sourceSet() == SourceSet.MAIN)
                    .toList();
            List<GraphNode> testNodes = uniqueNodes.stream()
                    .filter(node -> node.sourceSet() == SourceSet.TEST)
                    .toList();
            List<GraphNode> generatedNodes = uniqueNodes.stream()
                    .filter(node -> node.sourceSet() == SourceSet.GENERATED)
                    .toList();
            List<GraphEdge> mainEdges = edges.stream()
                    .filter(edge -> edge.sourceSet() == SourceSet.MAIN)
                    .toList();
            List<GraphEdge> testEdges = edges.stream()
                    .filter(edge -> edge.sourceSet() == SourceSet.TEST)
                    .toList();
            List<GraphEdge> generatedEdges = edges.stream()
                    .filter(edge -> edge.sourceSet() == SourceSet.GENERATED)
                    .toList();
            List<GraphNode> primaryNodes = switch (sourceScope) {
                case MAIN -> mainNodes;
                case TEST -> testNodes;
                case GENERATED -> generatedNodes;
            };
            List<GraphEdge> primaryEdges = switch (sourceScope) {
                case MAIN -> mainEdges;
                case TEST -> testEdges;
                case GENERATED -> generatedEdges;
            };

            List<GraphEdge> resolvedMainEdges = resolved(mainEdges);
            List<GraphEdge> resolvedTestEdges = resolved(testEdges);
            List<GraphEdge> resolvedGeneratedEdges = resolved(generatedEdges);
            List<GraphEdge> resolvedPrimaryEdges = resolved(primaryEdges);
            List<GraphEdge> unresolvedPrimaryEdges = unresolved(primaryEdges);
            int totalUnresolvedCount = unresolvedPrimaryEdges.size()
                    + Math.max(0, suppressedUnresolvedCount);

            List<GraphNode> boundedMainNodes =
                    mainNodes.stream().limit(MAX_SYMBOLS).toList();
            List<GraphNode> boundedTestNodes =
                    testNodes.stream().limit(MAX_SYMBOLS).toList();
            List<GraphNode> boundedGeneratedNodes =
                    generatedNodes.stream().limit(MAX_SYMBOLS).toList();
            List<GraphEdge> boundedMainEdges =
                    resolvedMainEdges.stream().limit(MAX_RELATIONSHIPS).toList();
            List<GraphEdge> boundedTestEdges =
                    resolvedTestEdges.stream().limit(MAX_RELATIONSHIPS).toList();
            List<GraphEdge> boundedGeneratedEdges =
                    resolvedGeneratedEdges.stream().limit(MAX_RELATIONSHIPS).toList();
            List<GraphEdge> boundedUnresolvedEdges = unresolvedPrimaryEdges.stream()
                    .limit(MAX_UNRESOLVED_RELATIONSHIPS)
                    .toList();
            boolean mainTruncated = boundedMainNodes.size() < mainNodes.size()
                    || boundedMainEdges.size() < resolvedMainEdges.size();
            boolean testTruncated = boundedTestNodes.size() < testNodes.size()
                    || boundedTestEdges.size() < resolvedTestEdges.size();
            boolean generatedTruncated =
                    boundedGeneratedNodes.size() < generatedNodes.size()
                            || boundedGeneratedEdges.size() < resolvedGeneratedEdges.size();
            boolean primaryTruncated = switch (sourceScope) {
                case MAIN -> mainTruncated;
                case TEST -> testTruncated;
                case GENERATED -> generatedTruncated;
            };
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
            root.set("symbols", JSON.valueToTree(switch (sourceScope) {
                case MAIN -> boundedMainNodes;
                case TEST -> boundedTestNodes;
                case GENERATED -> boundedGeneratedNodes;
            }));
            root.set("main_symbols", JSON.valueToTree(boundedMainNodes));
            root.set("test_symbols", JSON.valueToTree(boundedTestNodes));
            root.set("generated_symbols", JSON.valueToTree(boundedGeneratedNodes));
            root.set("relationships", JSON.valueToTree(switch (sourceScope) {
                case MAIN -> boundedMainEdges;
                case TEST -> boundedTestEdges;
                case GENERATED -> boundedGeneratedEdges;
            }));
            root.set("unresolved_relationships", JSON.valueToTree(boundedUnresolvedEdges));
            root.put("unresolved_count", totalUnresolvedCount);
            root.set("main_relationships", JSON.valueToTree(boundedMainEdges));
            root.set("test_relationships", JSON.valueToTree(boundedTestEdges));
            root.set("generated_relationships", JSON.valueToTree(boundedGeneratedEdges));
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
            if (sourceScope != SourceSet.MAIN && mainTruncated) {
                allLimitations.add("main_result_truncated");
            }
            if (sourceScope != SourceSet.TEST && testTruncated) {
                allLimitations.add("test_result_truncated");
            }
            if (sourceScope != SourceSet.GENERATED && generatedTruncated) {
                allLimitations.add("generated_result_truncated");
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
