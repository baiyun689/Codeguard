package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.GraphNode;
import com.codeguard.agent.graph.GraphNodeKind;
import com.codeguard.agent.graph.ProjectSnapshot;
import com.codeguard.agent.graph.ProjectSnapshotProvider;
import com.codeguard.agent.graph.SourceSnapshotProvider;
import com.fasterxml.jackson.databind.JsonNode;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.body.ConstructorDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.TypeDeclaration;
import com.github.javaparser.ast.nodeTypes.NodeWithAnnotations;
import com.github.javaparser.Position;

import java.nio.charset.StandardCharsets;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;

/**
 * 读取已解析符号的源码片段。
 *
 * 调用方提供符号解析或图谱查询返回的 symbol_id，服务在同一版本快照中
 * 确定声明范围，并通过会话沙箱读取有界源码；不接受任意文件路径。
 */
public final class SymbolSourceReader {

    /** 单次源码片段的硬限制，防止类型或异常长方法重新膨胀上下文。 */
    private static final int MAX_SOURCE_BYTES = 16_384;
    private static final int MAX_SOURCE_LINES = 240;

    private final ProjectSnapshotProvider snapshot;
    private final SourceSnapshotProvider sourceSnapshot;

    public SymbolSourceReader(CompletableFuture<ProjectSnapshot> snapshot) {
        this.snapshot = (toolName, input) -> GraphToolSupport.await(snapshot);
        this.sourceSnapshot = null;
    }

    public SymbolSourceReader(ProjectSnapshotProvider snapshot) {
        this.snapshot = snapshot;
        this.sourceSnapshot = null;
    }

    /** 生产懒加载会话使用的源码专用快速路径。 */
    public SymbolSourceReader(SourceSnapshotProvider sourceSnapshot) {
        this.snapshot = null;
        this.sourceSnapshot = sourceSnapshot;
    }

    public ToolResult execute(String input, AgentContext context) {
        String symbolId = GraphToolSupport.symbolIdOnly(input);
        if (symbolId.isBlank()) {
            return ToolResult.error("缺少 symbol_id");
        }
        try {
            ProjectSnapshot value = sourceSnapshot != null
                    ? sourceSnapshot.loadSource(input)
                    : snapshot.load("read_symbol", input);
            symbolId = GraphToolSupport.canonicalSymbol(value, symbolId);
            Optional<GraphNode> node = value.graph().node(symbolId);
            if (node.isEmpty()) {
                return ToolResult.error("symbol_not_found: " + symbolId);
            }
            GraphNode symbol = node.orElseThrow();
            if (symbol.kind() == GraphNodeKind.FILE) {
                return ToolResult.error(
                        "unsupported_symbol_kind: FILE;请改查该文件中的具体类型或成员");
            }
            String source = value.sources().get(symbol.file());
            if (source == null) {
                return ToolResult.error("source_not_found: " + symbol.file());
            }

            JsonNode request = GraphToolSupport.JSON.readTree(input == null ? "" : input);
            SourceRange fullRange = sourceRange(value, symbol);
            int requestedStart = request.path("start_line").asInt(0);
            int requestedEnd = request.path("end_line").asInt(0);
            int cursor = request.path("cursor").asInt(0);
            if (requestedStart <= 0 && cursor > 0) {
                requestedStart = cursor;
            }
            // 分页游标超过声明末尾时返回结束标记，表示源码已读取完毕。
            if (cursor > 0 && cursor > fullRange.endLine()) {
                StringBuilder terminal = new StringBuilder();
                terminal.append("symbol_id: ").append(symbol.id()).append('\n');
                terminal.append("kind: ").append(symbol.kind().name()).append('\n');
                terminal.append("file: ").append(symbol.file()).append('\n');
                terminal.append("end_of_symbol: true\n");
                return ToolResult.ok(terminal.toString());
            }
            boolean ranged = requestedStart > 0 || requestedEnd > 0 || cursor > 0;
            SourceRange range = fullRange;
            if (ranged) {
                // 请求行范围位于声明之前时，返回已知符号的首个有界源码片段。
                // 读取范围始终限制在符号声明内，不退化为任意文件读取。
                if (requestedEnd > 0 && requestedEnd < fullRange.startLine()
                        && requestedStart <= fullRange.startLine()) {
                    int end = Math.min(
                            fullRange.endLine(),
                            fullRange.startLine() + MAX_SOURCE_LINES - 1);
                    range = SourceRange.fromLines(fullRange.startLine(), end);
                } else {
                    int start = requestedStart > 0
                            ? requestedStart : fullRange.startLine();
                    int end = requestedEnd > 0
                            ? requestedEnd : fullRange.endLine();
                    start = Math.max(fullRange.startLine(), start);
                    end = Math.min(fullRange.endLine(), end);
                    if (end < start) {
                        return ToolResult.error("invalid_source_range: " + symbolId);
                    }
                    range = SourceRange.fromLines(start, end);
                }
            }
            if (range.endLine() < range.startLine()) {
                return ToolResult.error("symbol_range_unavailable: " + symbolId);
            }
            // 符号内容超过单页限制时返回首个有效源码页。
            if (range.endLine() - range.startLine() + 1 > MAX_SOURCE_LINES) {
                range = SourceRange.fromLines(range.startLine(), range.startLine() + MAX_SOURCE_LINES - 1);
            }
            String fragment = sliceSource(source, range);
            while (fragment.getBytes(StandardCharsets.UTF_8).length > MAX_SOURCE_BYTES
                    && range.endLine() > range.startLine()) {
                range = SourceRange.fromLines(range.startLine(), range.endLine() - 1);
                fragment = sliceSource(source, range);
            }
            int bytes = fragment.getBytes(StandardCharsets.UTF_8).length;
            int lines = range.endLine() - range.startLine() + 1;
            if (!ranged && (bytes > MAX_SOURCE_BYTES || lines > MAX_SOURCE_LINES)) {
                return ToolResult.error(
                        "symbol_too_large: " + symbolId + " (" + bytes + " 字节, "
                                + lines + " 行);请改查该类型下的具体成员");
            }
            if (ranged && (bytes > MAX_SOURCE_BYTES || lines > MAX_SOURCE_LINES)) {
                return ToolResult.error(
                        "source_range_too_large: " + symbolId + " (" + bytes + " 字节, "
                                + lines + " 行);请缩小 start_line/end_line");
            }

            StringBuilder result = new StringBuilder();
            result.append("symbol_id: ").append(symbol.id()).append('\n');
            result.append("kind: ").append(symbol.kind().name()).append('\n');
            result.append("file: ").append(symbol.file()).append('\n');
            result.append("lines: ").append(range.startLine()).append('-')
                    .append(range.endLine()).append('\n');
            if (symbol.ownerId().startsWith("java:")) {
                result.append("owner_id: ").append(symbol.ownerId()).append('\n');
            }
            result.append("truncated: ").append((range.startLine() > fullRange.startLine()
                    || range.endLine() < fullRange.endLine())).append('\n');
            if (range.endLine() < fullRange.endLine()) {
                result.append("next_cursor: ").append(range.endLine() + 1).append('\n');
            }
            String membersOwner = symbol.kind() == GraphNodeKind.TYPE ? symbol.id() : symbol.ownerId();
            if (membersOwner.startsWith("java:")) {
                int pageStart = symbol.kind() == GraphNodeKind.TYPE ? range.startLine() : 0;
                var members = value.graph().symbolsInFile(symbol.file()).stream()
                        .filter(member -> member.ownerId().equals(membersOwner))
                        .filter(member -> member.startLine() >= pageStart)
                        .filter(member -> member.kind() == GraphNodeKind.FIELD
                                || member.kind() == GraphNodeKind.METHOD
                                || member.kind() == GraphNodeKind.CONSTRUCTOR
                                || member.kind() == GraphNodeKind.TYPE)
                        .sorted(Comparator.comparingInt(GraphNode::startLine).thenComparing(GraphNode::id))
                        .toList();
                result.append("members_owner_id: ").append(membersOwner).append('\n');
                result.append("members: ").append(GraphToolSupport.JSON.writeValueAsString(
                        members.stream().limit(64).map(member -> Map.of(
                                "id", member.id(), "kind", member.kind().name(),
                                "start_line", member.startLine(), "end_line", member.endLine())).toList()))
                        .append('\n');
                result.append("members_truncated: ").append(members.size() > 64).append('\n');
            }
            result.append('\n');
            result.append(fragment);
            return ToolResult.ok(result.toString());
        } catch (Exception exception) {
            return ToolResult.error("graph_unavailable: " + exception.getMessage());
        }
    }

    private static SourceRange sourceRange(ProjectSnapshot snapshot, GraphNode symbol) {
        CompilationUnit unit = snapshot.astUnits().get(symbol.file());
        if (unit == null || symbol.kind() == GraphNodeKind.FRAMEWORK_ENTRYPOINT) {
            return SourceRange.fromLines(symbol.startLine(), symbol.endLine());
        }

        Node declaration = findDeclaration(snapshot, unit, symbol);
        if (declaration == null) {
            return SourceRange.fromLines(symbol.startLine(), symbol.endLine());
        }
        Position begin = declaration.getBegin().orElse(null);
        Position finish = declaration.getEnd().orElse(null);
        if (begin == null || finish == null) {
            return SourceRange.fromLines(symbol.startLine(), symbol.endLine());
        }
        int start = begin.line;
        int end = finish.line;
        int startOffset = offset(snapshot.sources().get(symbol.file()), begin);
        int endOffset = offset(snapshot.sources().get(symbol.file()), finish) + 1;
        if (declaration instanceof NodeWithAnnotations<?> annotated) {
            for (Node annotation : annotated.getAnnotations()) {
                Position annotationBegin = annotation.getBegin().orElse(null);
                if (annotationBegin != null) {
                    start = Math.min(start, annotationBegin.line);
                    startOffset = Math.min(startOffset,
                            offset(snapshot.sources().get(symbol.file()), annotationBegin));
                }
            }
        }
        return new SourceRange(start, end, startOffset, endOffset);
    }

    private static Node findDeclaration(
            ProjectSnapshot snapshot,
            CompilationUnit unit,
            GraphNode symbol
    ) {
        return switch (symbol.kind()) {
            case METHOD -> findCallable(
                    snapshot, unit.findAll(MethodDeclaration.class), symbol);
            case CONSTRUCTOR -> findCallable(
                    snapshot, unit.findAll(ConstructorDeclaration.class), symbol);
            case TYPE -> findType(unit, symbol);
            case FIELD -> findField(snapshot, unit, symbol);
            default -> null;
        };
    }

    private static Node findCallable(
            ProjectSnapshot snapshot,
            List<? extends Node> declarations,
            GraphNode symbol
    ) {
        Node exact = declarations.stream()
                .filter(node -> belongsToOwner(snapshot, node, symbol))
                .filter(node -> hasRange(node, symbol) && callableMatches(node, symbol))
                .findFirst()
                .orElse(null);
        if (exact != null) {
            return exact;
        }
        List<? extends Node> sameRange = declarations.stream()
                .filter(node -> belongsToOwner(snapshot, node, symbol))
                .filter(node -> hasRange(node, symbol))
                .toList();
        if (sameRange.size() == 1) {
            return sameRange.get(0);
        }
        return declarations.stream()
                .filter(node -> belongsToOwner(snapshot, node, symbol))
                .filter(node -> containsLine(node, symbol.startLine()))
                .filter(node -> callableMatches(node, symbol))
                .min(Comparator.comparingInt(SymbolSourceReader::span))
                .orElse(null);
    }

    private static boolean callableMatches(Node node, GraphNode symbol) {
        String id = symbol.id();
        if (symbol.kind() == GraphNodeKind.METHOD && node instanceof MethodDeclaration method) {
            int separator = id.lastIndexOf('#');
            String expected = separator >= 0 ? id.substring(separator + 1) : id;
            return normalizeSignature(method.getSignature().asString())
                    .equals(normalizeSignature(expected));
        }
        if (symbol.kind() == GraphNodeKind.CONSTRUCTOR
                && node instanceof ConstructorDeclaration constructor) {
            int separator = id.indexOf("#<init>");
            String expected = separator >= 0
                    ? id.substring(separator + "#<init>".length()) : id;
            return normalizeSignature(constructor.getSignature().asString())
                    .equals(normalizeSignature(expected));
        }
        return true;
    }

    private static String normalizeSignature(String value) {
        return value.replaceAll("\\s+", "");
    }

    private static Node findType(CompilationUnit unit, GraphNode symbol) {
        TypeDeclaration<?> exact = unit.findAll(TypeDeclaration.class).stream()
                .filter(type -> hasRange(type, symbol))
                .findFirst()
                .orElse(null);
        if (exact != null) {
            return exact;
        }
        return unit.findAll(TypeDeclaration.class).stream()
                .filter(type -> containsLine(type, symbol.startLine()))
                .min(Comparator.comparingInt(SymbolSourceReader::span))
                .orElse(null);
    }

    private static Node findField(
            ProjectSnapshot snapshot,
            CompilationUnit unit,
            GraphNode symbol
    ) {
        String id = symbol.id();
        int separator = id.lastIndexOf('#');
        String fieldName = separator >= 0 ? id.substring(separator + 1) : id;
        Optional<FieldDeclaration> exact = unit.findAll(FieldDeclaration.class).stream()
                .filter(field -> belongsToOwner(snapshot, field, symbol))
                .filter(field -> field.getVariables().stream()
                        .anyMatch(variable -> variable.getNameAsString().equals(fieldName)))
                .filter(field -> containsLine(field, symbol.startLine()))
                .findFirst();
        return exact.orElseGet(() -> unit.findAll(FieldDeclaration.class).stream()
                .filter(field -> belongsToOwner(snapshot, field, symbol))
                .filter(field -> field.getVariables().stream()
                        .anyMatch(variable -> variable.getNameAsString().equals(fieldName)))
                .min(Comparator.comparingInt(field -> distance(field, symbol.startLine())))
                .orElse(null));
    }

    private static boolean belongsToOwner(
            ProjectSnapshot snapshot,
            Node declaration,
            GraphNode symbol
    ) {
        // 成员声明必须与图谱中所属类型及 AST 范围匹配。
        // 缺少所属类型或声明范围时拒绝读取，防止返回无关声明。
        if (symbol.ownerId().isBlank()) {
            return false;
        }
        GraphNode owner = snapshot.graph().node(symbol.ownerId()).orElse(null);
        TypeDeclaration<?> containingType = declaration
                .findAncestor(TypeDeclaration.class)
                .orElse(null);
        return owner != null && containingType != null
                && owner.file().equals(symbol.file()) && hasRange(containingType, owner);
    }

    private static boolean hasRange(Node node, GraphNode symbol) {
        int start = node.getBegin().map(position -> position.line).orElse(-1);
        int end = node.getEnd().map(position -> position.line).orElse(-1);
        return start == symbol.startLine() && end == symbol.endLine();
    }

    private static boolean containsLine(Node node, int line) {
        int start = node.getBegin().map(position -> position.line).orElse(Integer.MAX_VALUE);
        int end = node.getEnd().map(position -> position.line).orElse(Integer.MIN_VALUE);
        return start <= line && line <= end;
    }

    private static int span(Node node) {
        int start = node.getBegin().map(position -> position.line).orElse(0);
        int end = node.getEnd().map(position -> position.line).orElse(start);
        return end - start;
    }

    private static int distance(Node node, int line) {
        int start = node.getBegin().map(position -> position.line).orElse(line);
        int end = node.getEnd().map(position -> position.line).orElse(line);
        return line < start ? start - line : line > end ? line - end : 0;
    }

    /** 按 AST 的精确位置截取源码；无 AST 位置时退化为完整行范围。 */
    private static String sliceSource(String source, SourceRange range) {
        if (range.startOffset() >= 0 && range.endOffset() >= range.startOffset()) {
            return source.substring(
                    Math.min(range.startOffset(), source.length()),
                    Math.min(range.endOffset(), source.length()));
        }
        int start = lineStartOffset(source, range.startLine());
        int end = range.endLine() >= lineCount(source)
                ? source.length()
                : lineStartOffset(source, range.endLine() + 1);
        return source.substring(Math.min(start, source.length()), Math.min(end, source.length()));
    }

    private static int offset(String source, Position position) {
        return Math.min(
                lineStartOffset(source, position.line) + Math.max(0, position.column - 1),
                source.length());
    }

    private static int lineStartOffset(String source, int line) {
        if (line <= 1) {
            return 0;
        }
        int currentLine = 1;
        for (int index = 0; index < source.length(); index++) {
            if (source.charAt(index) == '\n' && ++currentLine == line) {
                return index + 1;
            }
        }
        return source.length();
    }

    private static int lineCount(String source) {
        int count = 1;
        for (int index = 0; index < source.length(); index++) {
            if (source.charAt(index) == '\n') {
                count++;
            }
        }
        return count;
    }

    private record SourceRange(int startLine, int endLine, int startOffset, int endOffset) {
        private static SourceRange fromLines(int startLine, int endLine) {
            return new SourceRange(startLine, endLine, -1, -1);
        }
    }
}
