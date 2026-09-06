package com.codeguard.agent.graph;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.ast.CompilationUnit;

import java.io.IOException;
import java.nio.file.FileVisitResult;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.SimpleFileVisitor;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/** 为 get_file_content 构建目标文件级源码快照，不触发全项目索引。 */
final class SourceSnapshotBuilder {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final Set<String> EXCLUDED_SEGMENTS =
            Set.of(".git", "target", "build", ".gradle", ".idea", "node_modules");

    private SourceSnapshotBuilder() {}

    static ProjectSnapshot build(ProjectKey key, String input) {
        Path root = key.repoRoot();
        String symbolId = symbolId(input);
        Optional<Path> target = locateSourceFile(root, symbolId);
        if (target.isEmpty()) {
            return empty(key, "source_file_not_located: " + symbolId);
        }

        Path file = target.orElseThrow();
        String relative = normalize(root.relativize(file));
        try {
            String source = Files.readString(file);
            JavaParser parser = new JavaParser(new ParserConfiguration()
                    .setLanguageLevel(ParserConfiguration.LanguageLevel.BLEEDING_EDGE)
                    .setStoreTokens(true)
                    .setAttributeComments(true));
            ParseResult<CompilationUnit> parsed = parser.parse(source);
            if (parsed.isSuccessful() && parsed.getResult().isPresent()) {
                CompilationUnit unit = parsed.getResult().orElseThrow();
                return new ProjectSnapshot(
                        key,
                        Map.of(relative, source),
                        Map.of(relative, unit),
                        extractIndexGraph(Map.of(relative, unit)),
                        List.of());
            }
            return empty(key, relative + ": " + parsed.getProblems());
        } catch (Exception exception) {
            return empty(key, relative + ": " + exception.getMessage());
        }
    }

    private static ProjectSnapshot empty(ProjectKey key, String diagnostic) {
        return new ProjectSnapshot(
                key,
                Map.of(),
                Map.of(),
                new ProjectCodeGraph(List.of(), List.of()),
                diagnostic == null || diagnostic.isBlank() ? List.of() : List.of(diagnostic));
    }

    static String symbolId(String input) {
        if (input == null || input.isBlank()) {
            return "";
        }
        String value = input.trim();
        if (!value.startsWith("{")) {
            return value;
        }
        try {
            JsonNode root = JSON.readTree(value);
            return root.path("symbol_id").asText(root.path("subject").asText("")).trim();
        } catch (Exception ignored) {
            return "";
        }
    }

    private static Optional<Path> locateSourceFile(Path root, String symbolId) {
        if (!symbolId.startsWith("java:")) {
            return Optional.empty();
        }
        int memberSeparator = symbolId.indexOf('#');
        String owner = symbolId.substring("java:".length(),
                        memberSeparator >= 0 ? memberSeparator : symbolId.length())
                .replace('$', '.');
        if (owner.isBlank()) {
            return Optional.empty();
        }
        String[] parts = owner.split("\\.");
        List<String> suffixes = new ArrayList<>();
        // Nested types are declared in the outer type's file. Try the full path first,
        // then progressively remove nested type segments.
        for (int length = parts.length; length >= 1; length--) {
            StringBuilder suffix = new StringBuilder();
            for (int i = 0; i < length; i++) {
                if (i > 0) {
                    suffix.append('/');
                }
                suffix.append(parts[i]);
            }
            suffix.append(".java");
            suffixes.add(suffix.toString());
        }
        for (String suffix : suffixes) {
            Optional<Path> direct = directSourcePath(root, suffix);
            if (direct.isPresent()) {
                return direct;
            }
        }
        try {
            List<Path> files = new ArrayList<>();
            Files.walkFileTree(root, new SimpleFileVisitor<>() {
                @Override
                public FileVisitResult preVisitDirectory(
                        Path directory, BasicFileAttributes attributes
                ) {
                    return hasExcludedSegment(root.relativize(directory))
                            ? FileVisitResult.SKIP_SUBTREE
                            : FileVisitResult.CONTINUE;
                }

                @Override
                public FileVisitResult visitFile(
                        Path path, BasicFileAttributes attributes
                ) {
                    if (attributes.isRegularFile()
                            && path.getFileName().toString().endsWith(".java")) {
                        files.add(path);
                    }
                    return FileVisitResult.CONTINUE;
                }
            });
            files.sort(Comparator.comparing(Path::toString));
            for (String suffix : suffixes) {
                String normalizedSuffix = "/" + suffix;
                Path match = files.stream()
                        .filter(path -> normalize(root.relativize(path)).endsWith(suffix)
                                || normalize(root.relativize(path)).equals(suffix)
                                || normalize(path).endsWith(normalizedSuffix))
                        .findFirst()
                        .orElse(null);
                if (match != null) {
                    return Optional.of(match);
                }
            }
        } catch (IOException ignored) {
            // The caller will receive an empty source snapshot and a deterministic diagnostic.
        }
        return Optional.empty();
    }

    private static Optional<Path> directSourcePath(Path root, String suffix) {
        List<Path> candidates = List.of(
                root.resolve(suffix),
                root.resolve("src/main/java").resolve(suffix),
                root.resolve("src/test/java").resolve(suffix),
                root.resolve("src/main").resolve(suffix),
                root.resolve("src/test").resolve(suffix));
        return candidates.stream()
                .filter(path -> Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS))
                .findFirst();
    }

    private static boolean hasExcludedSegment(Path relative) {
        for (Path segment : relative) {
            if (EXCLUDED_SEGMENTS.contains(segment.toString())) {
                return true;
            }
        }
        return false;
    }

    /** 与轻量项目索引共享同一 symbol/声明语义，但只处理一个文件。 */
    private static ProjectCodeGraph extractIndexGraph(Map<String, CompilationUnit> units) {
        return ProjectSnapshotBuilder.extractIndexGraph(units);
    }

    private static String normalize(Path path) {
        return path.toString().replace('\\', '/');
    }
}
