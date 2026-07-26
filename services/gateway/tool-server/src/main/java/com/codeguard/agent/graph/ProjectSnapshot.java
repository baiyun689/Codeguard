package com.codeguard.agent.graph;

import com.github.javaparser.ast.CompilationUnit;

import java.util.List;
import java.util.Map;

/** 一个代码版本的完整、只读源码/AST/图谱快照。 */
public record ProjectSnapshot(
        ProjectKey key,
        Map<String, String> sources,
        Map<String, CompilationUnit> astUnits,
        ProjectCodeGraph graph,
        List<String> diagnostics
) {
    public ProjectSnapshot {
        sources = Map.copyOf(sources);
        astUnits = Map.copyOf(astUnits);
        diagnostics = List.copyOf(diagnostics);
    }

    public boolean complete() {
        return diagnostics.isEmpty() && graph.edges().stream()
                .allMatch(edge -> edge.resolution() == ResolutionStatus.RESOLVED);
    }

    public String coverageStatus() {
        return complete() ? "complete" : "partial";
    }
}
