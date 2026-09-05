package com.codeguard.agent.graph;

import java.time.Duration;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/**
 * 版本固定、按工具查询扩展的快照提供器。
 *
 * <p>同一个 revision 只共享轻量 SourceIndex；每个规范化查询最多执行一次局部语义
 * 扩展。提供器不把局部快照写回共享索引，避免不同 reviewer 的查询顺序改变彼此结果。</p>
 */
final class LazyProjectSnapshotProvider implements ProjectSnapshotProvider {
    private final ProjectSnapshotManager manager;
    private final ProjectKey key;
    private final ProjectSemanticCache semanticCache;
    private final Duration queryTimeout;
    private final Map<String, CompletableFuture<ProjectSnapshot>> queries =
            new ConcurrentHashMap<>();

    LazyProjectSnapshotProvider(ProjectSnapshotManager manager, ProjectKey key) {
        this(manager, key, manager.semanticCache(key), Duration.ofSeconds(120));
    }

    LazyProjectSnapshotProvider(
            ProjectSnapshotManager manager,
            ProjectKey key,
            ProjectSemanticCache semanticCache,
            Duration queryTimeout
    ) {
        this.manager = manager;
        this.key = key;
        this.semanticCache = semanticCache;
        this.queryTimeout = queryTimeout;
    }

    @Override
    public ProjectSnapshot load(String toolName, String input) throws Exception {
        String queryKey = (toolName == null ? "" : toolName.trim())
                + "\n" + (input == null ? "" : input.trim());
        CompletableFuture<ProjectSnapshot> query = queries.computeIfAbsent(queryKey, ignored -> {
            ProjectSnapshotManager.CancellableFuture<ProjectSnapshot> result =
                    new ProjectSnapshotManager.CancellableFuture<>();
            manager.getOrBuildIndex(key).whenComplete((index, failure) -> {
                if (failure != null) {
                    result.completeExceptionally(failure);
                    return;
                }
                manager.submitCancellable(result, () -> ProjectSnapshotBuilder.expand(
                        index, toolName, input, semanticCache));
            });
            return result;
        });
        // 查询级失败不应被永久缓存：临时解析异常、线程池拒绝或超时后，后续
        // reviewer 仍需要有机会对同一 canonical query 重试。成功结果继续复用，
        // 同时保留 single-flight 语义，避免并发 reviewer 重复做局部解析。
        query.whenComplete((ignored, failure) -> {
            if (failure != null) {
                queries.remove(queryKey, query);
            }
        });
        try {
            return query.get(queryTimeout.toMillis(), TimeUnit.MILLISECONDS);
        } catch (TimeoutException exception) {
            query.cancel(true);
            queries.remove(queryKey, query);
            throw new TimeoutException(
                    "lazy graph query timed out after " + queryTimeout.toSeconds() + "s");
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            query.cancel(true);
            queries.remove(queryKey, query);
            throw exception;
        } catch (java.util.concurrent.ExecutionException exception) {
            Throwable cause = exception.getCause();
            if (cause instanceof Exception checked) {
                throw checked;
            }
            throw exception;
        }
    }
}
