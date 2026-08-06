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

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.CompletableFuture;

/** ThreatModelAgent 的安全路径工具：入口、敏感调用、字段读写与未解析边。
 *  按 symbol 类型查询安全暴露面——方法/构造器：框架入口+敏感调用链；
 *  字段：读写它的方法+敏感字段类型标记；类型：内部方法的敏感调用+继承者。 */
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
        return "按稳定 symbol_id 查询安全路径：方法/构造器返回框架入口与敏感调用链；"
                + "字段返回读写它的方法并标记敏感字段类型；类型返回内部方法的敏感调用与继承者；并附解析限制";
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
            GraphNodeKind kind = value.graph().node(symbol)
                    .map(GraphNode::kind).orElse(null);
            if (kind == GraphNodeKind.FIELD) {
                // 字段安全路径：哪些方法读写它（单层，不沿调用传播）。
                relationships.addAll(value.graph().incoming(
                        symbol, GraphEdgeKind.READS_FIELD));
                relationships.addAll(value.graph().incoming(
                        symbol, GraphEdgeKind.WRITES_FIELD));
            } else if (kind == GraphNodeKind.TYPE) {
                // 类型安全路径：内部方法的敏感调用链 + 谁继承/实现它。
                collectSensitiveCalls(value, internalMethods(value, symbol),
                        sourceScope, relationships);
                relationships.addAll(value.graph().incoming(
                        symbol, GraphEdgeKind.EXTENDS));
                relationships.addAll(value.graph().incoming(
                        symbol, GraphEdgeKind.IMPLEMENTS));
            } else {
                relationships.addAll(value.graph().incoming(
                        symbol, GraphEdgeKind.EXPOSES_ROUTE));
                relationships.addAll(value.graph().incoming(
                        symbol, GraphEdgeKind.LISTENS_TO_EVENT));
                collectSensitiveCalls(value, Set.of(symbol), sourceScope, relationships);
            }
            List<GraphNode> nodes = new ArrayList<>();
            value.graph().node(symbol).ifPresent(nodes::add);
            relationships.stream()
                    .map(GraphEdge::sourceId)
                    .map(value.graph()::node)
                    .flatMap(java.util.Optional::stream)
                    .forEach(nodes::add);
            List<String> limits = new ArrayList<>(relationships.stream()
                    .filter(edge -> GraphToolSupport.inScope(edge, sourceScope))
                    .filter(edge -> edge.resolution() == ResolutionStatus.UNRESOLVED)
                    .map(edge -> "unresolved_call:" + edge.targetId())
                    .toList());
            value.graph().node(symbol)
                    .filter(node -> node.kind() == GraphNodeKind.FIELD)
                    .map(GraphNode::signature)
                    .map(InspectSecurityPathTool::fieldType)
                    .filter(InspectSecurityPathTool::sensitive)
                    .ifPresent(type -> limits.add("field_type_sensitive: " + type));
            return GraphToolSupport.facts(value, symbol, nodes, relationships, limits);
        } catch (Exception exception) {
            return ToolResult.error("graph_unavailable: " + exception.getMessage());
        }
    }

    /** 类型声明的方法/构造器集合，作为敏感调用链查询的起点。 */
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

    /** 沿调用链收集敏感调用与未解析调用，保留原 3 层 BFS 语义。 */
    private static void collectSensitiveCalls(
            ProjectSnapshot value,
            Set<String> frontier,
            SourceSet sourceScope,
            List<GraphEdge> relationships
    ) {
        Set<String> visited = new LinkedHashSet<>();
        for (int depth = 0; depth < 3 && !frontier.isEmpty(); depth++) {
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
                    if (sensitive(edge.targetId())
                            || edge.resolution() == ResolutionStatus.UNRESOLVED) {
                        relationships.add(edge);
                    }
                    next.add(edge.targetId());
                }
            }
            frontier = next;
        }
    }

    /** 从字段签名中提取声明类型（"TokeniserState state" → "TokeniserState"）。 */
    private static String fieldType(String signature) {
        int separator = signature.lastIndexOf(' ');
        return separator > 0 ? signature.substring(0, separator) : signature;
    }

    private static boolean sensitive(String symbol) {
        String lower = symbol.toLowerCase(Locale.ROOT);
        return SENSITIVE_TERMS.stream().anyMatch(lower::contains);
    }
}
