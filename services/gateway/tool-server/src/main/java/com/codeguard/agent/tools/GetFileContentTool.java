package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.core.AgentTool;
import com.codeguard.agent.core.ToolResult;
import com.codeguard.agent.graph.GraphNode;
import com.codeguard.agent.graph.GraphNodeKind;
import com.codeguard.agent.graph.ProjectSnapshot;
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
import java.util.Optional;
import java.util.concurrent.CompletableFuture;

/**
 * 读取已由项目图解析出的 symbol 源码片段。
 *
 * <p>该工具故意不接受文件路径。调用方必须先通过 SymbolResolution 或图谱工具
 * 获得稳定 {@code symbol_id}，Gateway 再从同一份 ProjectSnapshot 解析声明范围。
 * 这样既避免 LLM 猜测路径，也避免把整个源码文件重复放入 ReAct 上下文。</p>
 */
public final class GetFileContentTool implements AgentTool {

    /** 单次源码片段的硬限制，防止类型或异常长方法重新膨胀上下文。 */
    private static final int MAX_SOURCE_BYTES = 16_384;
    private static final int MAX_SOURCE_LINES = 240;

    private final CompletableFuture<ProjectSnapshot> snapshot;

    public GetFileContentTool(CompletableFuture<ProjectSnapshot> snapshot) {
        this.snapshot = snapshot;
    }

    @Override
    public String name() {
        return "get_file_content";
    }

    @Override
    public String description() {
        return "按已解析的 symbol_id 读取对应源码片段。METHOD/CONSTRUCTOR 返回注解、"
                + "修饰符、签名和方法体；TYPE 返回类/接口/枚举定义；FIELD 返回完整字段声明；"
                + "FRAMEWORK_ENTRYPOINT 返回对应注解。只接受 SymbolResolution 或图谱结果中的 symbol_id，"
                + "不接受文件路径；片段有大小限制，过大时应改查具体成员。";
    }

    @Override
    public ToolResult execute(String input, AgentContext context) {
        String symbolId = GraphToolSupport.symbolIdOnly(input);
        if (symbolId.isBlank()) {
            return ToolResult.error("缺少 symbol_id");
        }
        try {
            ProjectSnapshot value = GraphToolSupport.await(snapshot);
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

            SourceRange range = sourceRange(value, symbol);
            if (range.endLine() < range.startLine()) {
                return ToolResult.error("symbol_range_unavailable: " + symbolId);
            }
            String fragment = sliceSource(source, range);
            int bytes = fragment.getBytes(StandardCharsets.UTF_8).length;
            int lines = range.endLine() - range.startLine() + 1;
            if (bytes > MAX_SOURCE_BYTES || lines > MAX_SOURCE_LINES) {
                return ToolResult.error(
                        "symbol_too_large: " + symbolId + " (" + bytes + " 字节, "
                                + lines + " 行);请改查该类型下的具体成员");
            }

            StringBuilder result = new StringBuilder();
            result.append("symbol_id: ").append(symbol.id()).append('\n');
            result.append("kind: ").append(symbol.kind().name()).append('\n');
            result.append("file: ").append(symbol.file()).append('\n');
            result.append("lines: ").append(range.startLine()).append('-')
                    .append(range.endLine()).append('\n');
            if (!symbol.ownerId().isBlank()) {
                result.append("owner_id: ").append(symbol.ownerId()).append('\n');
            }
            result.append("truncated: false\n\n");
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
                .min(Comparator.comparingInt(GetFileContentTool::span))
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
                .min(Comparator.comparingInt(GetFileContentTool::span))
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
        // Member declarations must be proven to belong to the graph symbol's
        // owner.  Do not fail open when the owner node or AST range is absent:
        // returning an unrelated declaration would make this source tool
        // contradict the graph ground truth.
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
