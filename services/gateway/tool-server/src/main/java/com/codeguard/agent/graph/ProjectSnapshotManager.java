package com.codeguard.agent.graph;

import com.google.common.cache.Cache;
import com.google.common.cache.CacheBuilder;

import java.time.Duration;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executor;
import java.util.concurrent.ForkJoinPool;
import java.util.concurrent.Future;
import java.util.concurrent.FutureTask;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.Callable;

/**
 * 项目快照缓存的唯一外部接缝。
 * 同一个 ProjectKey 只构建一次；Session 持有 future/快照引用，因此缓存淘汰不会中断活跃审查。
 */
public final class ProjectSnapshotManager {
    private final Cache<ProjectKey, CompletableFuture<ProjectSnapshot>> cache;
    private final Cache<ProjectKey, CompletableFuture<ProjectSnapshot>> indexCache;
    private final Cache<ProjectKey, ProjectSemanticCache> semanticCache;
    private final Executor executor;
    private final int maximumSnapshots;
    private final Duration cacheTtl;

    /** 取消时会同步取消实际语义扩展任务的 CompletableFuture。 */
    static final class CancellableFuture<T> extends CompletableFuture<T> {
        private volatile Future<?> task;

        void attach(Future<?> task) {
            this.task = task;
            if (isCancelled()) {
                task.cancel(true);
            }
        }

        @Override
        public boolean cancel(boolean mayInterruptIfRunning) {
            boolean cancelled = super.cancel(mayInterruptIfRunning);
            Future<?> current = task;
            if (current != null) {
                current.cancel(mayInterruptIfRunning);
            }
            return cancelled;
        }
    }

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
        this.maximumSnapshots = Math.max(1, maximumSnapshots);
        this.cacheTtl = ttl;
        this.cache = CacheBuilder.newBuilder()
                .maximumSize(this.maximumSnapshots)
                .expireAfterAccess(ttl.toMillis(), TimeUnit.MILLISECONDS)
                .build();
        this.indexCache = CacheBuilder.newBuilder()
                .maximumSize(this.maximumSnapshots)
                .expireAfterAccess(ttl.toMillis(), TimeUnit.MILLISECONDS)
                .build();
        this.semanticCache = CacheBuilder.newBuilder()
                .maximumSize(this.maximumSnapshots)
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

    /**
     * 获取不包含全量语义关系的项目轻量源码索引。
     *
     * 索引仅解析 AST，并可由同版本会话共享；工具通过 lazyProvider 按符号扩展关系。
     */
    public CompletableFuture<ProjectSnapshot> getOrBuildIndex(ProjectKey key) {
        try {
            return indexCache.get(key, () -> {
                CompletableFuture<ProjectSnapshot> build = CompletableFuture.supplyAsync(
                                () -> ProjectSnapshotBuilder.buildIndex(key), executor)
                        .orTimeout(buildTimeout.toMillis(), TimeUnit.MILLISECONDS);
                build.whenComplete((ignored, failure) -> {
                    if (failure != null) {
                        indexCache.asMap().remove(key, build);
                    }
                });
                return build;
            });
        } catch (Exception exception) {
            return CompletableFuture.failedFuture(exception);
        }
    }

    /** 返回一个不会在 session 创建阶段启动全项目语义构建的查询提供器。 */
    public ProjectSnapshotProvider lazyProvider(ProjectKey key) {
        return new LazyProjectSnapshotProvider(
                this,
                key,
                semanticCache(key),
                buildTimeout);
    }

    ProjectSemanticCache semanticCache(ProjectKey key) {
        try {
            return semanticCache.get(key, () -> new ProjectSemanticCache(
                    Math.max(256, 256 * maximumSnapshots),
                    cacheTtl));
        } catch (Exception exception) {
            // 共享缓存加载失败时，为本次调用创建可用的独立缓存。
            return new ProjectSemanticCache(256, buildTimeout);
        }
    }

    <T> void submitCancellable(CancellableFuture<T> result, Callable<T> callable) {
        FutureTask<Void> task = new FutureTask<>(() -> {
            if (result.isCancelled()) {
                return null;
            }
            try {
                result.complete(callable.call());
            } catch (Throwable failure) {
                result.completeExceptionally(failure);
            }
            return null;
        });
        result.attach(task);
        try {
            executor.execute(task);
        } catch (RuntimeException rejected) {
            task.cancel(false);
            result.completeExceptionally(rejected);
        }
    }

    public void release(ProjectKey key) {
        // 会话关闭时保留同版本语义缓存，供其他会话复用；缓存按容量和过期策略回收。
        cache.cleanUp();
        indexCache.cleanUp();
        semanticCache.cleanUp();
    }
}
