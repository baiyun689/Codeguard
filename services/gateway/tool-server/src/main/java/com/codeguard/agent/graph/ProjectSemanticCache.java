package com.codeguard.agent.graph;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.expr.FieldAccessExpr;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.ObjectCreationExpr;
import com.github.javaparser.ast.expr.NameExpr;
import com.github.javaparser.ast.type.ClassOrInterfaceType;
import com.google.common.cache.Cache;
import com.google.common.cache.CacheBuilder;
import com.google.common.util.concurrent.UncheckedExecutionException;

import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
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
    private volatile Map<String, List<String>> lexicalCandidateFiles;

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

    /**
     * 返回按源码 AST 预建的反向候选文件集合。
     *
     * <p>候选索引只用于筛选文件，关系仍需经过现有语义解析确认，因此不会把
     * 词法命中提升为 RESOLVED 事实。索引在同一 revision 内只构建一次，可避免
     * inspect_structure / inspect_change_impact 为每个 target 重扫全部 AST。</p>
     */
    List<String> indexedCandidateFiles(ProjectSnapshot snapshot, String key) {
        Map<String, List<String>> index = lexicalCandidateFiles;
        if (index == null) {
            synchronized (this) {
                index = lexicalCandidateFiles;
                if (index == null) {
                    index = buildLexicalCandidateIndex(snapshot);
                    lexicalCandidateFiles = index;
                }
            }
        }
        return index.getOrDefault(key, List.of());
    }

    private static Map<String, List<String>> buildLexicalCandidateIndex(
            ProjectSnapshot snapshot
    ) {
        Map<String, Set<String>> mutable = new LinkedHashMap<>();
        for (Map.Entry<String, CompilationUnit> entry : snapshot.astUnits().entrySet()) {
            String file = entry.getKey();
            CompilationUnit unit = entry.getValue();
            for (MethodCallExpr call : unit.findAll(MethodCallExpr.class)) {
                add(mutable, "METHOD|" + call.getNameAsString() + "|"
                        + call.getArguments().size(), file);
            }
            for (ObjectCreationExpr call : unit.findAll(ObjectCreationExpr.class)) {
                add(mutable, "CONSTRUCTOR|" + call.getType().getNameAsString() + "|"
                        + call.getArguments().size(), file);
            }
            for (var method : unit.findAll(com.github.javaparser.ast.body.MethodDeclaration.class)) {
                add(mutable, "OVERRIDE|" + method.getNameAsString() + "|"
                        + method.getParameters().size(), file);
            }
            for (NameExpr name : unit.findAll(NameExpr.class)) {
                add(mutable, "FIELD|" + name.getNameAsString(), file);
            }
            for (FieldAccessExpr field : unit.findAll(FieldAccessExpr.class)) {
                add(mutable, "FIELD|" + field.getNameAsString(), file);
            }
            for (ClassOrInterfaceDeclaration type : unit.findAll(ClassOrInterfaceDeclaration.class)) {
                type.getExtendedTypes().forEach(parent ->
                        add(mutable, "TYPE|" + simpleTypeName(parent), file));
                type.getImplementedTypes().forEach(parent ->
                        add(mutable, "TYPE|" + simpleTypeName(parent), file));
            }
            // Type users are indexed separately from inheritance candidates.
            // The same lexical index only filters files; Symbol Solver still
            // confirms the resolved REFERENCES_TYPE edge later.
            for (ClassOrInterfaceType type : unit.findAll(ClassOrInterfaceType.class)) {
                add(mutable, "TYPE_REF|" + simpleTypeName(type), file);
            }
        }
        Map<String, List<String>> frozen = new LinkedHashMap<>();
        mutable.forEach((key, files) -> {
            List<String> sorted = new ArrayList<>(files);
            sorted.sort(String::compareTo);
            frozen.put(key, List.copyOf(sorted));
        });
        return Map.copyOf(frozen);
    }

    private static void add(Map<String, Set<String>> index, String key, String file) {
        index.computeIfAbsent(key, ignored -> new LinkedHashSet<>()).add(file);
    }

    private static String simpleTypeName(com.github.javaparser.ast.type.ClassOrInterfaceType type) {
        String value = type.getNameWithScope();
        int separator = value.lastIndexOf('.');
        return separator >= 0 ? value.substring(separator + 1) : value;
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
