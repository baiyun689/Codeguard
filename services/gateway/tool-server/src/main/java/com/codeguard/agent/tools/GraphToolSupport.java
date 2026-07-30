package com.codeguard.agent.tools;

import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.GraphEdge;
import com.codeguard.agent.graph.GraphNode;
import com.codeguard.agent.graph.ProjectSnapshot;
import com.codeguard.agent.graph.ResolutionStatus;
import com.codeguard.agent.graph.SourceSet;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.util.Collection;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

final class GraphToolSupport {
    static final ObjectMapper JSON = new ObjectMapper();
    private static final long BUILD_TIMEOUT_SECONDS = 120;
    private static final int MAX_SYMBOLS = 100;
    private static final int MAX_RELATIONSHIPS = 200;

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
                sourceScope(snapshot, subject));
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
        try {
            ObjectNode root = JSON.createObjectNode();
            List<GraphNode> mainNodes = nodes.stream()
                    .filter(node -> node.sourceSet() == SourceSet.MAIN)
                    .toList();
            List<GraphNode> testNodes = nodes.stream()
                    .filter(node -> node.sourceSet() == SourceSet.TEST)
                    .toList();
            List<GraphNode> generatedNodes = nodes.stream()
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

            List<GraphNode> boundedMainNodes =
                    mainNodes.stream().limit(MAX_SYMBOLS).toList();
            List<GraphNode> boundedTestNodes =
                    testNodes.stream().limit(MAX_SYMBOLS).toList();
            List<GraphNode> boundedGeneratedNodes =
                    generatedNodes.stream().limit(MAX_SYMBOLS).toList();
            List<GraphEdge> boundedMainEdges =
                    mainEdges.stream().limit(MAX_RELATIONSHIPS).toList();
            List<GraphEdge> boundedTestEdges =
                    testEdges.stream().limit(MAX_RELATIONSHIPS).toList();
            List<GraphEdge> boundedGeneratedEdges =
                    generatedEdges.stream().limit(MAX_RELATIONSHIPS).toList();
            boolean mainTruncated = boundedMainNodes.size() < mainNodes.size()
                    || boundedMainEdges.size() < mainEdges.size();
            boolean testTruncated = boundedTestNodes.size() < testNodes.size()
                    || boundedTestEdges.size() < testEdges.size();
            boolean generatedTruncated =
                    boundedGeneratedNodes.size() < generatedNodes.size()
                            || boundedGeneratedEdges.size() < generatedEdges.size();
            boolean primaryTruncated = switch (sourceScope) {
                case MAIN -> mainTruncated;
                case TEST -> testTruncated;
                case GENERATED -> generatedTruncated;
            };
            boolean found = !primaryEdges.isEmpty()
                    || (subjectAloneIsFact && !primaryNodes.isEmpty());
            boolean locallyResolved = primaryEdges.stream()
                    .allMatch(edge -> edge.resolution() == ResolutionStatus.RESOLVED);
            boolean completeCoverage = !primaryTruncated
                    && locallyResolved
                    && "complete".equals(snapshot.coverageStatus(sourceScope));
            root.put("status", found && locallyResolved ? "confirmed" : (
                    !found && completeCoverage ? "not_found" : "unknown"));
            root.put("coverage", completeCoverage ? "complete" : "partial");
            root.put("source_scope", sourceScope.name());
            root.put("production_coverage",
                    snapshot.productionComplete() ? "complete" : "partial");
            root.put("main_coverage", snapshot.coverageStatus(SourceSet.MAIN));
            root.put("test_coverage", snapshot.coverageStatus(SourceSet.TEST));
            root.put("generated_coverage", snapshot.coverageStatus(SourceSet.GENERATED));
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
            root.set("main_relationships", JSON.valueToTree(boundedMainEdges));
            root.set("test_relationships", JSON.valueToTree(boundedTestEdges));
            root.set("generated_relationships", JSON.valueToTree(boundedGeneratedEdges));
            ArrayNode allLimitations = root.putArray("limitations");
            snapshot.diagnosticsFor(sourceScope).forEach(allLimitations::add);
            limitations.forEach(allLimitations::add);
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
}
