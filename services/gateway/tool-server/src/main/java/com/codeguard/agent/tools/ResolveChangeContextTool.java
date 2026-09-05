package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.GraphNode;
import com.codeguard.agent.graph.ProjectSnapshot;
import com.codeguard.agent.graph.ProjectSnapshotProvider;
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
    private final ProjectSnapshotProvider snapshot;

    public ResolveChangeContextTool(CompletableFuture<ProjectSnapshot> snapshot) {
        this.snapshot = (toolName, input) -> GraphToolSupport.await(snapshot);
    }

    public ResolveChangeContextTool(ProjectSnapshotProvider snapshot) {
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
            ProjectSnapshot value = snapshot.load(name(), input);
            JsonNode request = GraphToolSupport.JSON.readTree(input);
            ObjectNode result = GraphToolSupport.JSON.createObjectNode();
            EnumSet<SourceSet> requestedSourceSets = EnumSet.noneOf(SourceSet.class);
            Set<String> requestedFiles = new LinkedHashSet<>();
            for (JsonNode change : request.path("changes")) {
                String file = change.path("file").asText("").replace('\\', '/');
                requestedFiles.add(file);
                requestedSourceSets.add(SourceSet.fromPath(file));
            }
            if (requestedSourceSets.isEmpty()) {
                requestedSourceSets.add(SourceSet.MAIN);
            }
            SourceSet sourceScope = requestedSourceSets.size() == 1
                    ? requestedSourceSets.iterator().next()
                    : SourceSet.MAIN;
            List<String> relevantDiagnostics = requestedSourceSets.stream()
                    .flatMap(sourceSet -> value.diagnosticsFor(sourceSet).stream())
                    .filter(diagnostic -> diagnostic.startsWith("scan_failed: ")
                            || requestedFiles.stream().anyMatch(
                            file -> diagnostic.startsWith(file + ":")))
                    .distinct()
                    .toList();
            // 这里回答的是“变更行能否定位到项目符号”，不能因为项目中任意外部
            // 调用未解析不能污染所有已定位符号。inspect 查询工具会按本次
            // 关系返回 outcome/coverage；这里的解析诊断只描述上下文快照。
            result.put("schema_version", 2);
            result.put("source_scope", sourceScope.name());
            result.set(
                    "source_scopes",
                    GraphToolSupport.JSON.valueToTree(requestedSourceSets));
            result.put("snapshot_production_coverage",
                    value.productionComplete() ? "complete" : "partial");
            result.put("snapshot_main_coverage", value.coverageStatus(SourceSet.MAIN));
            result.put("snapshot_test_coverage", value.coverageStatus(SourceSet.TEST));
            result.put("snapshot_generated_coverage",
                    value.coverageStatus(SourceSet.GENERATED));
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
            boolean completeCoverage = relevantDiagnostics.isEmpty();
            result.put("outcome", !contexts.isEmpty()
                    ? "found"
                    : (completeCoverage ? "not_found" : "indeterminate"));
            result.put("coverage", completeCoverage ? "complete" : "partial");
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
