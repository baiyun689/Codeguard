package com.codeguard.agent.core;

import java.nio.file.Path;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 一次审查会话的运行上下文,供工具执行时读取。
 * <p>
 * 只承载"事实"性的会话状态:仓库根目录、本次 diff 涉及的文件集合,以及调用计数。
 * 它不知道"在审查什么问题"——按职责边界(design.md D0 不变量④),
 * "是不是问题"的判断永远在 Python 侧,Java 只提供事实与护栏。
 */
public final class AgentContext {

    private final Path repoRoot;
    private final Set<String> allowedFiles;
    private final AtomicInteger toolCallCount = new AtomicInteger(0);

    public AgentContext(Path repoRoot, Set<String> allowedFiles) {
        this.repoRoot = repoRoot.normalize().toAbsolutePath();
        this.allowedFiles = Set.copyOf(allowedFiles);
    }

    public Path getRepoRoot() {
        return repoRoot;
    }

    public Set<String> getAllowedFiles() {
        return allowedFiles;
    }

    public int incrementToolCalls() {
        return toolCallCount.incrementAndGet();
    }

    public int getToolCallCount() {
        return toolCallCount.get();
    }
}
