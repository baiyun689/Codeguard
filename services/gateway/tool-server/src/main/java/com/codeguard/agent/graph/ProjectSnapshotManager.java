package com.codeguard.agent.graph;

import com.google.common.cache.Cache;
import com.google.common.cache.CacheBuilder;

import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executor;
import java.util.concurrent.ForkJoinPool;
import java.util.concurrent.TimeUnit;
import java.time.Duration;

/**
 * 项目快照缓存的唯一外部接缝。
 * 同一个 ProjectKey 只构建一次；Session 持有 future/快照引用，因此缓存淘汰不会中断活跃审查。
 */
public final class ProjectSnapshotManager {
    private final Cache<ProjectKey, CompletableFuture<ProjectSnapshot>> cache;
    private final Executor executor;

    public ProjectSnapshotManager() {
        this(4, Duration.ofMinutes(30), Duration.ofSeconds(120));
    }

    public ProjectSnapshotManager(
            int maximumSnapshots,
            Duration ttl,
            Duration buildTimeout
    ) {
        this(ForkJoinPool.commonPool(), maximumSnapshots, ttl, buildTimeout);
    }

    ProjectSnapshotManager(
            Executor executor,
            int maximumSnapshots,
            Duration ttl,
            Duration buildTimeout
    ) {
        this.executor = executor;
        this.cache = CacheBuilder.newBuilder()
                .maximumSize(maximumSnapshots)
                .expireAfterAccess(ttl.toMillis(), TimeUnit.MILLISECONDS)
                .build();
        this.buildTimeout = buildTimeout;
    }

    private final Duration buildTimeout;

    public CompletableFuture<ProjectSnapshot> getOrBuild(ProjectKey key) {
        try {
            CompletableFuture<ProjectSnapshot> snapshot = cache.get(key, () -> {
                CompletableFuture<ProjectSnapshot> build = CompletableFuture.supplyAsync(
                                () -> ProjectSnapshotBuilder.build(key), executor)
                        .orTimeout(buildTimeout.toMillis(), TimeUnit.MILLISECONDS);
                build.whenComplete((ignored, failure) -> {
                    if (failure != null) {
                        cache.asMap().remove(key, build);
                    }
                });
                return build;
            });
            return snapshot;
        } catch (Exception exception) {
            return CompletableFuture.failedFuture(exception);
        }
    }

    public void release(ProjectKey key) {
        // Session 持有直接引用；此方法是生命周期语义接缝，LRU/TTL 负责跨会话复用与最终回收。
        cache.cleanUp();
    }
}
