package com.codeguard.agent.graph;

import java.nio.file.Path;

/** 精确项目快照键；版本变化会自然隔离旧图。 */
public record ProjectKey(
        Path repoRoot,
        String revision,
        String graphVersion,
        String analyzerConfig
) {
    public static final String CURRENT_GRAPH_VERSION = "java-spring-v1";

    public ProjectKey {
        repoRoot = repoRoot.normalize().toAbsolutePath();
        revision = revision == null || revision.isBlank() ? "working-tree" : revision;
        graphVersion = graphVersion == null || graphVersion.isBlank()
                ? CURRENT_GRAPH_VERSION : graphVersion;
        analyzerConfig = analyzerConfig == null ? "" : analyzerConfig;
    }

    public static ProjectKey of(Path repoRoot, String revision) {
        return new ProjectKey(repoRoot, revision, CURRENT_GRAPH_VERSION, "");
    }
}
