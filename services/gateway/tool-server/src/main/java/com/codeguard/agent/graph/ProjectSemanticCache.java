package com.codeguard.agent.graph;

import com.github.javaparser.JavaParser;
import com.google.common.cache.Cache;
import com.google.common.cache.CacheBuilder;
import com.google.common.util.concurrent.UncheckedExecutionException;

import java.time.Duration;
import java.util.List;
import java.util.concurrent.ExecutionException;
import java.util.function.Supplier;

/**
 * Revision-scoped cache for lazy semantic expansion.
 *
 * <p>The lightweight source index is shared by {@link ProjectSnapshotManager}, but
 * the old lazy provider only cached an exact tool query.  That meant three tools
 * asking about the same source file each parsed and resolved the file again.  This
 * cache keeps the expensive immutable products of that work: a parser factory,
 * per-file edges, candidate files for reverse lookup, and complete incoming edge
 * sets.  Guava's {@code Cache.get} provides single-flight loading for equal keys;
 * failed loads are not retained by Guava.</p>
 */
final class ProjectSemanticCache {
    @FunctionalInterface
    interface CheckedSupplier<T> {
        T get() throws Exception;
    }

    private final Cache<String, List<GraphEdge>> fileEdges;
    private final Cache<String, List<String>> candidateFiles;
    private final Cache<String, List<GraphEdge>> incomingEdges;
    private volatile Supplier<JavaParser> parserFactory;

    ProjectSemanticCache(int maximumEntries, Duration ttl) {
        int boundedEntries = Math.max(64, maximumEntries);
        this.fileEdges = cache(boundedEntries, ttl);
        this.candidateFiles = cache(boundedEntries, ttl);
        this.incomingEdges = cache(boundedEntries, ttl);
    }

    private static <T> Cache<String, T> cache(int maximumEntries, Duration ttl) {
        return CacheBuilder.newBuilder()
                .maximumSize(maximumEntries)
                .expireAfterAccess(ttl.toMillis(), java.util.concurrent.TimeUnit.MILLISECONDS)
                .build();
    }

    Supplier<JavaParser> parserFactory(Supplier<JavaParser> factory) {
        Supplier<JavaParser> current = parserFactory;
        if (current != null) {
            return current;
        }
        synchronized (this) {
            if (parserFactory == null) {
                parserFactory = factory;
            }
            return parserFactory;
        }
    }

    List<GraphEdge> fileEdges(String file, CheckedSupplier<List<GraphEdge>> loader)
            throws Exception {
        return load(fileEdges, file, loader);
    }

    List<String> candidateFiles(String key, CheckedSupplier<List<String>> loader)
            throws Exception {
        return load(candidateFiles, key, loader);
    }

    List<GraphEdge> incomingEdges(String key, CheckedSupplier<List<GraphEdge>> loader)
            throws Exception {
        return load(incomingEdges, key, loader);
    }

    private static <T> T load(
            Cache<String, T> cache,
            String key,
            CheckedSupplier<T> loader
    ) throws Exception {
        try {
            return cache.get(key, () -> loader.get());
        } catch (ExecutionException exception) {
            Throwable cause = exception.getCause();
            if (cause instanceof Exception checked) {
                throw checked;
            }
            if (cause instanceof Error error) {
                throw error;
            }
            throw exception;
        } catch (UncheckedExecutionException exception) {
            Throwable cause = exception.getCause();
            if (cause instanceof Exception checked) {
                throw checked;
            }
            if (cause instanceof Error error) {
                throw error;
            }
            throw exception;
        }
    }
}
