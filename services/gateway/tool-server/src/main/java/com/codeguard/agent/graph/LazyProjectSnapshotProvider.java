package com.codeguard.agent.graph;

import java.time.Duration;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/**
 * 固定版本、按工具查询扩展关系的快照提供器。
 *
 * 共享轻量索引与语义缓存，规范查询结果在提供器内复用；
 * 局部查询生成独立快照，不修改共享索引。
 */
final class LazyProjectSnapshotProvider implements ProjectSnapshotProvider, SourceSnapshotProvider {
    private final ProjectSnapshotManager manager;
    private final ProjectKey key;
    private final ProjectSemanticCache semanticCache;
    private final Duration queryTimeout;
    private final Map<String, CompletableFuture<ProjectSnapshot>> queries =
            new ConcurrentHashMap<>();
    private final Map<String, CompletableFuture<ProjectSnapshot>> sourceQueries =
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

    /**
     * 源码读取的快速路径：只定位并解析 symbol 所在文件，不等待全项目轻量索引。
     * 失败时返回带诊断的空快照，由 SymbolSourceReader 按原有 symbol_not_found 合同处理。
     */
    @Override
    public ProjectSnapshot loadSource(String input) throws Exception {
        String queryKey = input == null ? "" : input.trim();
        CompletableFuture<ProjectSnapshot> query = sourceQueries.computeIfAbsent(queryKey, ignored -> {
            ProjectSnapshotManager.CancellableFuture<ProjectSnapshot> result =
                    new ProjectSnapshotManager.CancellableFuture<>();
            manager.submitCancellable(result, () -> SourceSnapshotBuilder.build(key, input));
            return result;
        });
        query.whenComplete((ignored, failure) -> {
            if (failure != null) {
                sourceQueries.remove(queryKey, query);
            }
        });
        try {
            ProjectSnapshot source = query.get(queryTimeout.toMillis(), TimeUnit.MILLISECONDS);
            // 框架入口标识不对应 Java 声明，使用完整索引定位；普通 Java 符号使用源码定位。
            if (source.sources().isEmpty()
                    && !SourceSnapshotBuilder.symbolId(input).startsWith("java:")) {
                return load("read_symbol", input);
            }
            return source;
        } catch (TimeoutException exception) {
            query.cancel(true);
            sourceQueries.remove(queryKey, query);
            throw new TimeoutException(
                    "source query timed out after " + queryTimeout.toSeconds() + "s");
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            query.cancel(true);
            sourceQueries.remove(queryKey, query);
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
