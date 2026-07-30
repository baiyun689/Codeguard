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
        return productionComplete();
    }

    public boolean productionComplete() {
        return sourceSetComplete(SourceSet.MAIN)
                && sourceSetComplete(SourceSet.GENERATED);
    }

    private boolean sourceSetComplete(SourceSet sourceSet) {
        return diagnosticsFor(sourceSet).isEmpty()
                && graph.edges().stream()
                .filter(edge -> edge.sourceSet() == sourceSet)
                .allMatch(edge -> edge.resolution() == ResolutionStatus.RESOLVED);
    }

    public boolean testComplete() {
        return sourceSetComplete(SourceSet.TEST);
    }

    public String coverageStatus() {
        return coverageStatus(SourceSet.MAIN);
    }

    public String coverageStatus(SourceSet sourceSet) {
        boolean complete = sourceSetComplete(sourceSet);
        return complete ? "complete" : "partial";
    }

    public List<String> diagnosticsFor(SourceSet sourceSet) {
        return diagnostics.stream()
                .filter(diagnostic ->
                        isGlobalDiagnostic(diagnostic)
                                || diagnosticSourceSet(diagnostic) == sourceSet)
                .toList();
    }

    private static boolean isGlobalDiagnostic(String diagnostic) {
        return diagnostic != null && diagnostic.startsWith("scan_failed: ");
    }

    private static SourceSet diagnosticSourceSet(String diagnostic) {
        String value = diagnostic == null ? "" : diagnostic;
        if (value.startsWith("symlink_rejected: ")) {
            return SourceSet.fromPath(value.substring("symlink_rejected: ".length()));
        }
        int separator = value.indexOf(':');
        if (separator > 0) {
            return SourceSet.fromPath(value.substring(0, separator));
        }
        return SourceSet.MAIN;
    }
}
