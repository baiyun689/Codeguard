package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.GraphNode;
import com.codeguard.agent.graph.ProjectSnapshot;
import com.codeguard.agent.graph.SourceSet;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.util.EnumSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CompletableFuture;

/** 将 diff 文件/行批量解析为真实、稳定的图谱符号。 */
public final class ResolveChangeContextTool implements AgentTool {
    private final CompletableFuture<ProjectSnapshot> snapshot;

    public ResolveChangeContextTool(CompletableFuture<ProjectSnapshot> snapshot) {
        this.snapshot = snapshot;
    }

    @Override
    public String name() {
        return "resolve_change_context";
    }

    @Override
    public String description() {
        return "把变更文件和行号批量解析为稳定 symbol_id 与局部结构事实";
    }

    @Override
    public ToolResult execute(String input, AgentContext context) {
        try {
            ProjectSnapshot value = GraphToolSupport.await(snapshot);
            JsonNode request = GraphToolSupport.JSON.readTree(input);
            ObjectNode result = GraphToolSupport.JSON.createObjectNode();
            EnumSet<SourceSet> requestedSourceSets = EnumSet.noneOf(SourceSet.class);
            for (JsonNode change : request.path("changes")) {
                requestedSourceSets.add(
                        SourceSet.fromPath(change.path("file").asText("")));
            }
            if (requestedSourceSets.isEmpty()) {
                requestedSourceSets.add(SourceSet.MAIN);
            }
            SourceSet sourceScope = requestedSourceSets.size() == 1
                    ? requestedSourceSets.iterator().next()
                    : SourceSet.MAIN;
            boolean completeCoverage = requestedSourceSets.stream()
                    .allMatch(sourceSet ->
                            "complete".equals(value.coverageStatus(sourceSet)));
            List<String> relevantDiagnostics = requestedSourceSets.stream()
                    .flatMap(sourceSet -> value.diagnosticsFor(sourceSet).stream())
                    .distinct()
                    .toList();
            // 这里回答的是“变更行能否定位到项目符号”，不能因为项目中任意外部
            // 调用未解析就把所有已定位符号降为 unknown。查询级工具仍会按具体
            // 关系返回 unknown/partial；解析诊断则代表快照本身不完整。
            result.put(
                    "status",
                    relevantDiagnostics.isEmpty() ? "confirmed" : "partial");
            result.put("coverage", completeCoverage ? "complete" : "partial");
            result.put("source_scope", sourceScope.name());
            result.set(
                    "source_scopes",
                    GraphToolSupport.JSON.valueToTree(requestedSourceSets));
            result.put("production_coverage",
                    value.productionComplete() ? "complete" : "partial");
            result.put("main_coverage", value.coverageStatus(SourceSet.MAIN));
            result.put("test_coverage", value.coverageStatus(SourceSet.TEST));
            result.put("generated_coverage", value.coverageStatus(SourceSet.GENERATED));
            ArrayNode contexts = result.putArray("contexts");
            for (JsonNode change : request.path("changes")) {
                String file = change.path("file").asText("").replace('\\', '/');
                Set<String> emitted = new LinkedHashSet<>();
                for (JsonNode lineNode : change.path("lines")) {
                    int line = lineNode.asInt();
                    GraphNode symbol = value.graph().symbolAt(file, line).orElse(null);
                    if (symbol == null || !emitted.add(symbol.id())) {
                        continue;
                    }
                    ObjectNode item = contexts.addObject();
                    item.put("file_id", "file:" + file);
                    item.put("file", file);
                    item.put("symbol_id", symbol.id());
                    item.put("kind", symbol.kind().name().toLowerCase());
                    item.put("start_line", symbol.startLine());
                    item.put("end_line", symbol.endLine());
                    item.put("signature", symbol.signature());
                    item.put("owner_type", symbol.ownerId());
                    item.put("source_set", symbol.sourceSet().name());
                    item.set("annotations", GraphToolSupport.JSON.valueToTree(symbol.annotations()));
                    item.set("control_flow", GraphToolSupport.JSON.valueToTree(
                            controlFlow(value, symbol)));
                    item.put("resolution", "resolved");
                }
            }
            result.set(
                    "limitations",
                    GraphToolSupport.JSON.valueToTree(relevantDiagnostics));
            return ToolResult.ok(GraphToolSupport.JSON.writeValueAsString(result));
        } catch (Exception exception) {
            return ToolResult.error("graph_unavailable: " + exception.getMessage());
        }
    }

    private static Set<String> controlFlow(ProjectSnapshot snapshot, GraphNode symbol) {
        Set<String> result = new LinkedHashSet<>();
        snapshot.astUnits().get(symbol.file()).findAll(com.github.javaparser.ast.Node.class).stream()
                .filter(node -> node.getBegin().map(position -> position.line >= symbol.startLine()).orElse(false))
                .filter(node -> node.getEnd().map(position -> position.line <= symbol.endLine()).orElse(false))
                .map(node -> node.getClass().getSimpleName())
                .filter(name -> Set.of(
                        "IfStmt", "SwitchStmt", "ForStmt", "ForEachStmt",
                        "WhileStmt", "DoStmt", "TryStmt", "ThrowStmt").contains(name))
                .forEach(result::add);
        return result;
    }
}
