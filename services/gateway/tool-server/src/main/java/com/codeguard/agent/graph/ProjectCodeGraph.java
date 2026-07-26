package com.codeguard.agent.graph;

import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/** 项目级只读语义图；工具只能通过该接口查询，不能修改底层 AST。 */
public final class ProjectCodeGraph {
    private final Map<String, GraphNode> nodesById;
    private final Map<String, List<GraphNode>> nodesByFile;
    private final List<GraphEdge> edges;
    private final Map<String, List<GraphEdge>> incoming;
    private final Map<String, List<GraphEdge>> outgoing;

    ProjectCodeGraph(Collection<GraphNode> nodes, Collection<GraphEdge> edges) {
        Map<String, GraphNode> ids = new LinkedHashMap<>();
        Map<String, List<GraphNode>> files = new LinkedHashMap<>();
        for (GraphNode node : nodes) {
            ids.putIfAbsent(node.id(), node);
            files.computeIfAbsent(node.file(), ignored -> new ArrayList<>()).add(node);
        }
        this.nodesById = Map.copyOf(ids);
        Map<String, List<GraphNode>> frozenFiles = new LinkedHashMap<>();
        files.forEach((key, value) -> frozenFiles.put(key, List.copyOf(value)));
        this.nodesByFile = Map.copyOf(frozenFiles);
        this.edges = List.copyOf(edges);

        Map<String, List<GraphEdge>> in = new LinkedHashMap<>();
        Map<String, List<GraphEdge>> out = new LinkedHashMap<>();
        for (GraphEdge edge : edges) {
            in.computeIfAbsent(edge.targetId(), ignored -> new ArrayList<>()).add(edge);
            out.computeIfAbsent(edge.sourceId(), ignored -> new ArrayList<>()).add(edge);
        }
        this.incoming = freezeEdges(in);
        this.outgoing = freezeEdges(out);
    }

    private static Map<String, List<GraphEdge>> freezeEdges(Map<String, List<GraphEdge>> source) {
        Map<String, List<GraphEdge>> result = new LinkedHashMap<>();
        source.forEach((key, value) -> result.put(key, List.copyOf(value)));
        return Map.copyOf(result);
    }

    public Collection<GraphNode> nodes() {
        return nodesById.values();
    }

    public List<GraphEdge> edges() {
        return edges;
    }

    public Optional<GraphNode> node(String symbolId) {
        return Optional.ofNullable(nodesById.get(symbolId));
    }

    public List<GraphNode> symbolsInFile(String file) {
        return nodesByFile.getOrDefault(normalize(file), List.of());
    }

    public List<GraphEdge> incoming(String symbolId, GraphEdgeKind kind) {
        return incoming.getOrDefault(symbolId, List.of()).stream()
                .filter(edge -> edge.kind() == kind)
                .toList();
    }

    public List<GraphEdge> outgoing(String symbolId, GraphEdgeKind kind) {
        return outgoing.getOrDefault(symbolId, List.of()).stream()
                .filter(edge -> edge.kind() == kind)
                .toList();
    }

    public Optional<GraphNode> symbolAt(String file, int line) {
        return symbolsInFile(file).stream()
                .filter(node -> node.kind() == GraphNodeKind.METHOD
                        || node.kind() == GraphNodeKind.CONSTRUCTOR
                        || node.kind() == GraphNodeKind.FIELD
                        || node.kind() == GraphNodeKind.TYPE)
                .filter(node -> node.startLine() <= line && node.endLine() >= line)
                .min((left, right) -> {
                    int span = Integer.compare(
                            left.endLine() - left.startLine(),
                            right.endLine() - right.startLine());
                    return span != 0 ? span : Integer.compare(
                            symbolPriority(left.kind()), symbolPriority(right.kind()));
                });
    }

    private static int symbolPriority(GraphNodeKind kind) {
        return switch (kind) {
            case METHOD, CONSTRUCTOR -> 0;
            case FIELD -> 1;
            case TYPE -> 2;
            default -> 3;
        };
    }

    static String normalize(String file) {
        return file.replace('\\', '/');
    }
}
