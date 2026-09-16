package com.codeguard.agent.core;

import java.nio.file.Path;
import java.util.concurrent.atomic.AtomicInteger;

/** 工具执行使用的审查会话上下文，保存仓库范围和调用计数，不执行缺陷判断。 */
public final class AgentContext {

    private final Path repoRoot;
    private final AtomicInteger toolCallCount = new AtomicInteger(0);

    public AgentContext(Path repoRoot) {
        this.repoRoot = repoRoot.normalize().toAbsolutePath();
    }

    public Path getRepoRoot() {
        return repoRoot;
    }

    public int incrementToolCalls() {
        return toolCallCount.incrementAndGet();
    }

    public int getToolCallCount() {
        return toolCallCount.get();
    }
}
