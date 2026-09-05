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

    /** CompletableFuture whose cancellation is propagated to the actual expansion task. */
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
     * 获取不包含全量语义边的轻量源码索引。该 Future 只做 plain AST parse；工具查询
     * 通过 {@link #lazyProvider(ProjectKey)} 在此索引上按 symbol 扩展关系。即使索引
     * 被多个 session 共享，也不会触发旧的全项目 symbol-solver 构图。
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
            // Cache construction is deterministic and should not fail in normal
            // operation; keep a usable per-call cache if a cache loader is rejected.
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
        // Session 持有直接引用；此方法是生命周期语义接缝。semanticCache 不主动
        // invalidate，刻意让同一 revision 的后续 session 复用已解析文件边，最终由
        // LRU/TTL 回收，避免 session 销毁导致下一次审查重新做语义解析。
        cache.cleanUp();
        indexCache.cleanUp();
        semanticCache.cleanUp();
    }
}
