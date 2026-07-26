package com.codeguard.agent.tools;

import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.GraphEdge;
import com.codeguard.agent.graph.GraphNode;
import com.codeguard.agent.graph.ProjectSnapshot;
import com.codeguard.agent.graph.ResolutionStatus;
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
        try {
            ObjectNode root = JSON.createObjectNode();
            boolean found = !edges.isEmpty() || (subjectAloneIsFact && !nodes.isEmpty());
            List<GraphNode> boundedNodes = nodes.stream().limit(MAX_SYMBOLS).toList();
            List<GraphEdge> boundedEdges = edges.stream().limit(MAX_RELATIONSHIPS).toList();
            boolean truncated = boundedNodes.size() < nodes.size()
                    || boundedEdges.size() < edges.size();
            boolean locallyResolved = edges.stream()
                    .allMatch(edge -> edge.resolution() == ResolutionStatus.RESOLVED);
            boolean completeCoverage = !truncated && locallyResolved && snapshot.complete();
            root.put("status", found && locallyResolved ? "confirmed" : (
                    !found && completeCoverage ? "not_found" : "unknown"));
            root.put("coverage", completeCoverage ? "complete" : "partial");
            root.put("subject_symbol_id", subject);
            root.set("symbols", JSON.valueToTree(boundedNodes));
            root.set("relationships", JSON.valueToTree(boundedEdges));
            ArrayNode allLimitations = root.putArray("limitations");
            snapshot.diagnostics().forEach(allLimitations::add);
            limitations.forEach(allLimitations::add);
            if (truncated) {
                allLimitations.add("result_truncated");
            }
            return ToolResult.ok(JSON.writeValueAsString(root));
        } catch (Exception exception) {
            return ToolResult.error("graph_result_error: " + exception.getMessage());
        }
    }
}
