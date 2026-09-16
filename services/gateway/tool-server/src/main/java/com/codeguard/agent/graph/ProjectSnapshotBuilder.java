package com.codeguard.agent.graph;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.body.CallableDeclaration;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.ConstructorDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.TypeDeclaration;
import com.github.javaparser.ast.expr.AnnotationExpr;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.ObjectCreationExpr;
import com.github.javaparser.ast.expr.AssignExpr;
import com.github.javaparser.ast.expr.FieldAccessExpr;
import com.github.javaparser.ast.expr.NameExpr;
import com.github.javaparser.ast.expr.UnaryExpr;
import com.github.javaparser.ast.expr.NormalAnnotationExpr;
import com.github.javaparser.ast.expr.SingleMemberAnnotationExpr;
import com.github.javaparser.ast.nodeTypes.NodeWithAnnotations;
import com.github.javaparser.ast.type.ClassOrInterfaceType;
import com.github.javaparser.resolution.TypeSolver;
import com.github.javaparser.resolution.declarations.ResolvedMethodDeclaration;
import com.github.javaparser.resolution.declarations.ResolvedReferenceTypeDeclaration;
import com.github.javaparser.resolution.model.SymbolReference;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.CombinedTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.JavaParserTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.ReflectionTypeSolver;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Supplier;
import java.util.stream.Stream;

final class ProjectSnapshotBuilder {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final Set<String> EXCLUDED_SEGMENTS =
            Set.of(".git", "target", "build", ".gradle", ".idea", "node_modules");
    private static final Set<String> ROUTE_ANNOTATIONS = Set.of(
            "RequestMapping", "GetMapping", "PostMapping", "PutMapping",
            "DeleteMapping", "PatchMapping");
    private static final Set<String> EVENT_ANNOTATIONS =
            Set.of("EventListener", "TransactionalEventListener", "KafkaListener", "RabbitListener");
    private static final Set<String> SCHEDULE_ANNOTATIONS = Set.of("Scheduled");
    private static final Set<String> INJECT_ANNOTATIONS =
            Set.of("Autowired", "Inject", "Resource");

    private ProjectSnapshotBuilder() {}

    static ProjectSnapshot build(ProjectKey key) {
        Path root = key.repoRoot();
        List<String> diagnostics = new ArrayList<>();
        List<Path> javaFiles = scanJavaFiles(root, diagnostics);

        // 第一遍:无符号解析 parse,收集项目内全部类型的 FQCN 集合(快速失败层用)。
        // 与第二遍重复 parse 的代价(~1s)远小于让第三方类型走失败路径的代价——
        // JavaParserTypeSolver 解析未命中时会 parseDirectory 递归重扫整个 source root(见 ProjectAwareTypeSolver)。
        Map<String, String> sources = new LinkedHashMap<>();
        Set<String> projectTypes = new LinkedHashSet<>();
        JavaParser plainParser = new JavaParser(new ParserConfiguration()
                .setLanguageLevel(ParserConfiguration.LanguageLevel.BLEEDING_EDGE)
                .setStoreTokens(true)
                .setAttributeComments(true));
        for (Path file : javaFiles) {
            String relative = normalize(root.relativize(file));
            try {
                String source = Files.readString(file);
                sources.put(relative, source);
                ParseResult<CompilationUnit> parsed = plainParser.parse(source);
                if (parsed.isSuccessful() && parsed.getResult().isPresent()) {
                    collectTypeNames(parsed.getResult().orElseThrow(), projectTypes);
                }
            } catch (Exception exception) {
                diagnostics.add(relative + ": " + exception.getMessage());
            }
        }

        // 第二遍:带 resolver parse。第三方类型(非 JDK、非项目内)由 ProjectAwareTypeSolver
        // 直接快速失败,不进入 solver 链——否则每次失败都会触发 parseDirectory 全目录重扫。
        CombinedTypeSolver delegate = new CombinedTypeSolver(new ReflectionTypeSolver(false));
        discoverSourceRoots(root, javaFiles).forEach(path -> delegate.add(new JavaParserTypeSolver(path)));
        JavaParser parser = new JavaParser(new ParserConfiguration()
                .setLanguageLevel(ParserConfiguration.LanguageLevel.BLEEDING_EDGE)
                .setStoreTokens(true)
                .setAttributeComments(true)
                .setSymbolResolver(new JavaSymbolSolver(new ProjectAwareTypeSolver(projectTypes, delegate))));

        Map<String, CompilationUnit> units = new LinkedHashMap<>();
        for (Path file : javaFiles) {
            String relative = normalize(root.relativize(file));
            String source = sources.get(relative);
            if (source == null) {
                continue;  // 第一遍已记录解析/读取失败
            }
            try {
                ParseResult<CompilationUnit> parsed = parser.parse(source);
                if (parsed.isSuccessful() && parsed.getResult().isPresent()) {
                    units.put(relative, parsed.getResult().orElseThrow());
                } else {
                    diagnostics.add(relative + ": " + parsed.getProblems());
                }
            } catch (Exception exception) {
                diagnostics.add(relative + ": " + exception.getMessage());
            }
        }
        ProjectCodeGraph graph = extractGraph(units);
        return new ProjectSnapshot(key, sources, units, graph, diagnostics);
    }

    /**
     * 构建懒查询使用的项目轻量索引。
     *
     * 读取源码并解析声明 AST，不执行符号求解；语义关系由查询阶段按需扩展。
     */
    static ProjectSnapshot buildIndex(ProjectKey key) {
        Path root = key.repoRoot();
        List<String> diagnostics = new ArrayList<>();
        List<Path> javaFiles = scanJavaFiles(root, diagnostics);
        Map<String, String> sources = new LinkedHashMap<>();
        Map<String, CompilationUnit> units = new LinkedHashMap<>();
        JavaParser parser = new JavaParser(new ParserConfiguration()
                .setLanguageLevel(ParserConfiguration.LanguageLevel.BLEEDING_EDGE)
                .setStoreTokens(true)
                .setAttributeComments(true));
        for (Path file : javaFiles) {
            String relative = normalize(root.relativize(file));
            try {
                String source = Files.readString(file);
                ParseResult<CompilationUnit> parsed = parser.parse(source);
                sources.put(relative, source);
                if (parsed.isSuccessful() && parsed.getResult().isPresent()) {
                    units.put(relative, parsed.getResult().orElseThrow());
                } else {
                    diagnostics.add(relative + ": " + parsed.getProblems());
                }
            } catch (Exception exception) {
                diagnostics.add(relative + ": " + exception.getMessage());
            }
        }
        return new ProjectSnapshot(key, sources, units, extractIndexGraph(units), diagnostics);
    }

    /**
     * 对一个工具查询按需解析关系。返回新的不可变快照，原始轻量索引不会被修改，因而
     * 同一个 revision 的并发 reviewer 不会互相污染。源码读取直接复用索引；
     * 变更上下文只解析请求文件中变更行的直接关系，不遍历调用图。
     */
    static ProjectSnapshot expand(ProjectSnapshot index, String toolName, String input)
            throws Exception {
        return expand(index, toolName, input, new ProjectSemanticCache(256, java.time.Duration.ofMinutes(5)));
    }

    static ProjectSnapshot expand(
            ProjectSnapshot index,
            String toolName,
            String input,
            ProjectSemanticCache semanticCache
    ) throws Exception {
        if (toolName.equals("read_symbol")) {
            return index;
        }
        if (toolName.equals("resolve_change_context")) {
            return expandChangedReferences(index, input, semanticCache);
        }
        String subject = canonicalNodeId(index, subjectId(input));
        if (subject.isBlank()) {
            return index;
        }
        int maxDepth = toolName.equals("inspect_structure") ? 1 : maxDepth(input);
        Supplier<JavaParser> parserFactory = semanticCache.parserFactory(
                semanticParserFactory(index));
        Set<String> frontier = new LinkedHashSet<>(Set.of(subject));
        GraphNode subjectNode = index.graph().node(subject).orElse(null);
        boolean securityPath = toolName.equals("inspect_path")
                && pathKind(input).equals("security");
        if (securityPath && subjectNode != null && subjectNode.kind() == GraphNodeKind.TYPE) {
            index.graph().symbolsInFile(subjectNode.file()).stream()
                    .filter(node -> subject.equals(node.ownerId()))
                    .filter(node -> node.kind() == GraphNodeKind.METHOD
                            || node.kind() == GraphNodeKind.CONSTRUCTOR)
                    .map(GraphNode::id)
                    .forEach(frontier::add);
        }
        Set<String> visited = new LinkedHashSet<>();
        Set<String> resolvedFiles = new LinkedHashSet<>();
        List<GraphEdge> discovered = new ArrayList<>();
        String relation = toolName.equals("query_relations")
                ? JSON.readTree(input).path("relation").asText("") : "";
        boolean reverse = toolName.equals("inspect_change_impact")
                || Set.of("callers", "field_readers", "field_writers", "implementations",
                        "children", "type_users", "entrypoints")
                        .contains(relation);
        int fileLimit = reverse ? 64 : 24;
        for (int depth = 0; depth < maxDepth && !frontier.isEmpty(); depth++) {
            ensureNotInterrupted();
            Set<String> next = new LinkedHashSet<>();
            for (String current : frontier) {
                ensureNotInterrupted();
                if (!visited.add(current)) {
                    continue;
                }
                List<GraphEdge> edges = reverse
                        ? resolveIncoming(
                                index, current, semanticCache, parserFactory, fileLimit, false,
                                relation)
                        : resolveOutgoing(
                                index, current, semanticCache, parserFactory, fileLimit, resolvedFiles);
                if (relation.equals("overrides")) {
                    edges = new ArrayList<>(edges);
                    edges.addAll(resolveIncomingOverrides(index, current, semanticCache, parserFactory, fileLimit));
                }
                discovered.addAll(edges);
                edges.stream()
                        .filter(edge -> edge.resolution() == ResolutionStatus.RESOLVED)
                        .map(edge -> relation.equals("overrides")
                                ? (edge.sourceId().equals(current) ? edge.targetId() : edge.sourceId())
                                : reverse ? edge.sourceId() : edge.targetId())
                        .map(id -> canonicalNodeId(index, id))
                        .filter(id -> index.graph().node(id).isPresent())
                        .forEach(next::add);
            }
            frontier = next;
        }
        if (toolName.equals("inspect_structure")
                || (securityPath && subjectNode != null
                && (subjectNode.kind() == GraphNodeKind.FIELD
                || subjectNode.kind() == GraphNodeKind.TYPE))) {
            discovered.addAll(resolveIncoming(
                    index, subject, semanticCache, parserFactory, fileLimit,
                    toolName.equals("inspect_structure"), relation));
        }
        return withEdges(index, discovered);
    }

    private static ProjectSnapshot expandChangedReferences(
            ProjectSnapshot index, String input, ProjectSemanticCache cache
    ) throws Exception {
        Map<String, Set<Integer>> changed = new LinkedHashMap<>();
        for (var change : JSON.readTree(input).path("changes")) {
            String file = change.path("file").asText("").replace('\\', '/');
            if (!index.sources().containsKey(file)) continue;
            Set<Integer> lines = changed.computeIfAbsent(file, ignored -> new LinkedHashSet<>());
            for (var line : change.path("lines")) {
                if (line.canConvertToInt() && line.asInt() > 0) lines.add(line.asInt());
            }
        }
        if (changed.isEmpty()) return index;
        Supplier<JavaParser> factory = cache.parserFactory(semanticParserFactory(index));
        List<GraphEdge> direct = new ArrayList<>();
        List<String> diagnostics = new ArrayList<>(index.diagnostics());
        int files = 0;
        for (var entry : changed.entrySet()) {
            ensureNotInterrupted();
            if (entry.getValue().isEmpty()) continue;
            if (files++ >= 24) {
                diagnostics.add(entry.getKey() + ": changed_reference_file_limit");
                continue;
            }
            try {
                cache.fileEdges(entry.getKey(), () -> resolveFileEdges(index, entry.getKey(), factory))
                        .stream()
                        .filter(edge -> entry.getKey().equals(edge.file()) && entry.getValue().contains(edge.line()))
                        .forEach(direct::add);
            } catch (InterruptedException interrupted) {
                throw interrupted;
            } catch (Exception exception) {
                diagnostics.add(entry.getKey() + ": changed_reference_resolution_failed");
            }
        }
        ProjectSnapshot expanded = withEdges(index, direct);
        return new ProjectSnapshot(index.key(), index.sources(), index.astUnits(), expanded.graph(), diagnostics);
    }

    private static ProjectSnapshot withEdges(
            ProjectSnapshot index,
            Collection<GraphEdge> additionalEdges
    ) {
        Map<String, GraphEdge> unique = new LinkedHashMap<>();
        for (GraphEdge edge : index.graph().edges()) {
            unique.put(edgeKey(edge), edge);
        }
        for (GraphEdge edge : additionalEdges) {
            GraphEdge canonical = canonicalEdge(index, edge);
            unique.putIfAbsent(edgeKey(canonical), canonical);
        }
        return new ProjectSnapshot(
                index.key(), index.sources(), index.astUnits(),
                new ProjectCodeGraph(index.graph().nodes(), unique.values()), index.diagnostics());
    }

    private static String edgeKey(GraphEdge edge) {
        return edge.sourceId() + "|" + edge.targetId() + "|" + edge.kind()
                + "|" + edge.file() + "|" + edge.line();
    }

    /**
     * 统一轻量索引与语义解析产生的方法签名格式。
     *
     * 只消除参数列表中的空白差异，并使用轻量索引中的符号标识作为图谱端点。
     */
    private static String canonicalNodeId(ProjectSnapshot index, String requested) {
        if (requested == null || requested.isBlank()) {
            return requested == null ? "" : requested;
        }
        if (index.graph().node(requested).isPresent()) {
            return requested;
        }
        String compact = comparableSymbolId(requested);
        return index.graph().nodes().stream()
                .filter(node -> comparableSymbolId(node.id()).equals(compact))
                .map(GraphNode::id)
                .findFirst()
                .orElse(requested);
    }

    private static GraphEdge canonicalEdge(ProjectSnapshot index, GraphEdge edge) {
        String source = canonicalNodeId(index, edge.sourceId());
        String target = canonicalNodeId(index, edge.targetId());
        if (source.equals(edge.sourceId()) && target.equals(edge.targetId())) {
            return edge;
        }
        return new GraphEdge(source, target, edge.kind(), edge.file(), edge.line(),
                edge.sourceSet(), edge.resolution(), edge.extractor());
    }

    private static String comparableSymbolId(String value) {
        if (!value.startsWith("java:")) {
            return value;
        }
        // 包名用于区分重载，不得删除包名后猜测符号身份。
        return value.replaceAll("\\s+", "");
    }

    private static List<Path> scanJavaFiles(Path root, List<String> diagnostics) {
        try (Stream<Path> stream = Files.walk(root)) {
            return stream.filter(path -> {
                        if (Files.isSymbolicLink(path)) {
                            diagnostics.add("symlink_rejected: " + normalize(root.relativize(path)));
                            return false;
                        }
                        return Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS);
                    })
                    .filter(path -> path.getFileName().toString().endsWith(".java"))
                    .filter(path -> !hasExcludedSegment(root.relativize(path)))
                    .sorted(Comparator.comparing(Path::toString))
                    .toList();
        } catch (IOException exception) {
            diagnostics.add("scan_failed: " + exception.getMessage());
            return List.of();
        }
    }

    private static boolean hasExcludedSegment(Path relative) {
        for (Path segment : relative) {
            if (EXCLUDED_SEGMENTS.contains(segment.toString())) {
                return true;
            }
        }
        return false;
    }

    private static Set<Path> discoverSourceRoots(Path root, List<Path> files) {
        Set<Path> roots = new LinkedHashSet<>();
        for (Path file : files) {
            Path parent = file.getParent();
            while (parent != null && parent.startsWith(root)) {
                String normalized = normalize(root.relativize(parent));
                if (normalized.endsWith("src/main/java") || normalized.endsWith("src/test/java")) {
                    roots.add(parent);
                    break;
                }
                parent = parent.getParent();
            }
        }
        if (roots.isEmpty()) {
            roots.add(root);
        }
        return roots;
    }

    private static ProjectCodeGraph extractGraph(Map<String, CompilationUnit> units) {
        List<GraphNode> nodes = new ArrayList<>();
        List<GraphEdge> edges = new ArrayList<>();
        Map<Node, String> symbolIds = new LinkedHashMap<>();

        units.forEach((file, unit) -> {
            String fileId = "file:" + file;
            nodes.add(new GraphNode(fileId, GraphNodeKind.FILE, file, 1,
                    Math.max(1, unit.getEnd().map(position -> position.line).orElse(1)),
                    file, "", SourceSet.fromPath(file), List.of()));
            for (TypeDeclaration<?> type : unit.findAll(TypeDeclaration.class)) {
                String typeId = "java:" + qualifiedTypeName(type, unit, file);
                symbolIds.put(type, typeId);
                nodes.add(node(typeId, GraphNodeKind.TYPE, file, type,
                        type.getNameAsString(), ownerId(type, symbolIds, file), annotations(type)));
                edges.add(edge(ownerId(type, symbolIds, file), typeId, GraphEdgeKind.DECLARES, file, type,
                        ResolutionStatus.RESOLVED, "java-ast"));
            }
        });

        units.forEach((file, unit) -> {
            for (MethodDeclaration method : unit.findAll(MethodDeclaration.class)) {
                String owner = ownerId(method, symbolIds, file);
                String id = methodId(method, owner);
                symbolIds.put(method, id);
                nodes.add(node(id, GraphNodeKind.METHOD, file, method,
                        method.getDeclarationAsString(false, false, true), owner,
                        annotations(method)));
                edges.add(edge(owner, id, GraphEdgeKind.DECLARES, file, method,
                        ResolutionStatus.RESOLVED, "java-ast"));
                addAnnotationEdges(edges, id, method, file);
                addFrameworkNodes(nodes, edges, method, id, file);
            }
            for (ConstructorDeclaration constructor : unit.findAll(ConstructorDeclaration.class)) {
                String owner = ownerId(constructor, symbolIds, file);
                String id = owner + "#<init>" + constructor.getSignature().asString();
                symbolIds.put(constructor, id);
                nodes.add(node(id, GraphNodeKind.CONSTRUCTOR, file, constructor,
                        constructor.getDeclarationAsString(false, false, true), owner,
                        annotations(constructor)));
                edges.add(edge(owner, id, GraphEdgeKind.DECLARES, file, constructor,
                        ResolutionStatus.RESOLVED, "java-ast"));
                addAnnotationEdges(edges, id, constructor, file);
                addFrameworkNodes(nodes, edges, constructor, id, file);
            }
            for (FieldDeclaration field : unit.findAll(FieldDeclaration.class)) {
                String owner = ownerId(field, symbolIds, file);
                field.getVariables().forEach(variable -> {
                    String id = owner + "#" + variable.getNameAsString();
                    symbolIds.put(variable, id);
                    nodes.add(node(id, GraphNodeKind.FIELD, file, variable,
                            variable.getTypeAsString() + " " + variable.getNameAsString(),
                            owner, annotations(field)));
                    edges.add(edge(owner, id, GraphEdgeKind.DECLARES, file, variable,
                            ResolutionStatus.RESOLVED, "java-ast"));
                    addAnnotationEdges(edges, id, field, file);
                    for (AnnotationExpr annotation : field.getAnnotations()) {
                        if (INJECT_ANNOTATIONS.contains(annotation.getName().getIdentifier())) {
                            edges.add(edge(owner, id, GraphEdgeKind.INJECTS, file, annotation,
                                    ResolutionStatus.RESOLVED, "spring-annotations"));
                        }
                    }
                });
            }
        });

        // 全部方法节点建立后再解析重写关系，避免文件遍历顺序影响父方法定位。
        units.forEach((file, unit) -> {
            for (MethodDeclaration method : unit.findAll(MethodDeclaration.class)) {
                if (method.isStatic() || method.isPrivate()) {
                    continue;
                }
                String id = symbolIds.get(method);
                if (id != null) {
                    addOverrideEdges(nodes, edges, method, id, file);
                }
            }
        });

        units.forEach((file, unit) -> {
            for (MethodCallExpr call : unit.findAll(MethodCallExpr.class)) {
                String caller = enclosingCallableId(call, symbolIds);
                if (caller == null) {
                    continue;
                }
                String target;
                ResolutionStatus status;
                try {
                    ResolvedMethodDeclaration resolved = call.resolve();
                    if (ambiguousSourceOverload(resolved)) {
                        throw new IllegalStateException("ambiguous_source_overload");
                    }
                    target = resolvedMethodId(resolved);
                    status = ResolutionStatus.RESOLVED;
                } catch (Exception exception) {
                    target = "unresolved:method:" + call.getNameAsString()
                            + "/" + call.getArguments().size();
                    status = ResolutionStatus.UNRESOLVED;
                }
                edges.add(edge(caller, target, GraphEdgeKind.CALLS, file, call, status,
                        "java-symbol-solver"));
            }
            for (ObjectCreationExpr call : unit.findAll(ObjectCreationExpr.class)) {
                String caller = enclosingCallableId(call, symbolIds);
                if (caller != null) {
                    edges.add(constructorEdge(caller, call, file));
                }
            }
            for (ClassOrInterfaceDeclaration type : unit.findAll(ClassOrInterfaceDeclaration.class)) {
                String source = symbolIds.get(type);
                type.getExtendedTypes().forEach(parent ->
                        addTypeEdge(edges, source, parent, GraphEdgeKind.EXTENDS, file));
                type.getImplementedTypes().forEach(parent ->
                        addTypeEdge(edges, source, parent, GraphEdgeKind.IMPLEMENTS, file));
            }
            for (ClassOrInterfaceType type : unit.findAll(ClassOrInterfaceType.class)) {
                String source = enclosingCallableId(type, symbolIds);
                if (source == null) {
                    source = type.findAncestor(FieldDeclaration.class)
                            .flatMap(field -> field.getVariables().stream()
                                    .filter(variable -> variable.getType().findAll(ClassOrInterfaceType.class)
                                            .stream().anyMatch(candidate -> candidate == type))
                                    .map(symbolIds::get)
                                    .filter(java.util.Objects::nonNull)
                                    .findFirst())
                            .orElse(null);
                }
                if (source == null) {
                    source = type.findAncestor(TypeDeclaration.class)
                            .map(symbolIds::get).orElse("file:" + file);
                }
                addTypeEdge(edges, source, type, GraphEdgeKind.REFERENCES_TYPE, file);
            }
            for (NameExpr expression : unit.findAll(NameExpr.class)) {
                addFieldAccessEdge(edges, symbolIds, file, expression);
            }
            for (FieldAccessExpr expression : unit.findAll(FieldAccessExpr.class)) {
                addFieldAccessEdge(edges, symbolIds, file, expression);
            }
        });

        return new ProjectCodeGraph(nodes, edges);
    }

    /** 只生成声明、注解和框架入口节点；调用/字段/继承关系留给查询时解析。 */
    static ProjectCodeGraph extractIndexGraph(Map<String, CompilationUnit> units) {
        List<GraphNode> nodes = new ArrayList<>();
        List<GraphEdge> edges = new ArrayList<>();
        Map<Node, String> symbolIds = new LinkedHashMap<>();
        units.forEach((file, unit) -> {
            String fileId = "file:" + file;
            nodes.add(new GraphNode(fileId, GraphNodeKind.FILE, file, 1,
                    Math.max(1, unit.getEnd().map(position -> position.line).orElse(1)),
                    file, "", SourceSet.fromPath(file), List.of()));
            for (TypeDeclaration<?> type : unit.findAll(TypeDeclaration.class)) {
                String typeId = "java:" + qualifiedTypeNameWithoutResolve(type, unit, file);
                symbolIds.put(type, typeId);
                nodes.add(node(typeId, GraphNodeKind.TYPE, file, type,
                        type.getNameAsString(), ownerId(type, symbolIds, file), annotations(type)));
                edges.add(edge(ownerId(type, symbolIds, file), typeId, GraphEdgeKind.DECLARES, file, type,
                        ResolutionStatus.RESOLVED, "java-ast"));
            }
        });
        units.forEach((file, unit) -> {
            for (MethodDeclaration method : unit.findAll(MethodDeclaration.class)) {
                String owner = ownerId(method, symbolIds, file);
                String id = owner + "#" + method.getSignature().asString();
                symbolIds.put(method, id);
                nodes.add(node(id, GraphNodeKind.METHOD, file, method,
                        method.getDeclarationAsString(false, false, true), owner,
                        annotations(method)));
                edges.add(edge(owner, id, GraphEdgeKind.DECLARES, file, method,
                        ResolutionStatus.RESOLVED, "java-ast"));
                addAnnotationEdges(edges, id, method, file);
                addFrameworkNodes(nodes, edges, method, id, file);
            }
            for (ConstructorDeclaration constructor : unit.findAll(ConstructorDeclaration.class)) {
                String owner = ownerId(constructor, symbolIds, file);
                String id = owner + "#<init>" + constructor.getSignature().asString();
                symbolIds.put(constructor, id);
                nodes.add(node(id, GraphNodeKind.CONSTRUCTOR, file, constructor,
                        constructor.getDeclarationAsString(false, false, true), owner,
                        annotations(constructor)));
                edges.add(edge(owner, id, GraphEdgeKind.DECLARES, file, constructor,
                        ResolutionStatus.RESOLVED, "java-ast"));
                addAnnotationEdges(edges, id, constructor, file);
                addFrameworkNodes(nodes, edges, constructor, id, file);
            }
            for (FieldDeclaration field : unit.findAll(FieldDeclaration.class)) {
                String owner = ownerId(field, symbolIds, file);
                field.getVariables().forEach(variable -> {
                    String id = owner + "#" + variable.getNameAsString();
                    symbolIds.put(variable, id);
                    nodes.add(node(id, GraphNodeKind.FIELD, file, variable,
                            variable.getTypeAsString() + " " + variable.getNameAsString(),
                            owner, annotations(field)));
                    edges.add(edge(owner, id, GraphEdgeKind.DECLARES, file, variable,
                            ResolutionStatus.RESOLVED, "java-ast"));
                    addAnnotationEdges(edges, id, field, file);
                });
            }
        });
        return new ProjectCodeGraph(nodes, edges);
    }

    private static String qualifiedTypeNameWithoutResolve(
            TypeDeclaration<?> type,
            CompilationUnit unit,
            String file
    ) {
        List<String> names = new ArrayList<>();
        Node current = type;
        while (current instanceof TypeDeclaration<?> declaration) {
            names.add(0, declaration.getNameAsString());
            current = declaration.getParentNode().orElse(null);
        }
        String prefix = unit.getPackageDeclaration()
                .map(declaration -> declaration.getNameAsString() + ".")
                .orElse("");
        return prefix + (names.isEmpty() ? file : String.join(".", names));
    }

    private static String subjectId(String input) {
        if (input == null || input.isBlank()) {
            return "";
        }
        String value = input.trim();
        if (!value.startsWith("{")) {
            return value;
        }
        try {
            JsonNode root = JSON.readTree(value);
            return root.path("subject_symbol_id").asText(
                    root.path("symbol_id").asText(root.path("subject").asText(""))).trim();
        } catch (Exception ignored) {
            return "";
        }
    }

    private static int maxDepth(String input) {
        try {
            JsonNode root = JSON.readTree(input == null ? "" : input);
            int depth = root.path("depth").asInt(root.path("max_depth").asInt(3));
            return Math.max(1, Math.min(3, depth));
        } catch (Exception ignored) {
            return 3;
        }
    }

    private static Supplier<JavaParser> semanticParserFactory(ProjectSnapshot index) {
        CombinedTypeSolver delegate = new CombinedTypeSolver(new ReflectionTypeSolver(false));
        List<Path> files = index.sources().keySet().stream()
                .map(index.key().repoRoot()::resolve)
                .toList();
        discoverSourceRoots(index.key().repoRoot(), files)
                .forEach(path -> delegate.add(new JavaParserTypeSolver(path)));
        Set<String> projectTypes = new LinkedHashSet<>();
        index.astUnits().values().forEach(unit -> collectTypeNames(unit, projectTypes));
        return () -> new JavaParser(new ParserConfiguration()
                .setLanguageLevel(ParserConfiguration.LanguageLevel.BLEEDING_EDGE)
                .setStoreTokens(true)
                .setAttributeComments(true)
                .setSymbolResolver(new JavaSymbolSolver(
                        new ProjectAwareTypeSolver(projectTypes, delegate))));
    }

    private static List<GraphEdge> resolveOutgoing(
            ProjectSnapshot index,
            String symbol,
            ProjectSemanticCache semanticCache,
            Supplier<JavaParser> parserFactory,
            int fileLimit,
            Set<String> resolvedFiles
    ) throws Exception {
        GraphNode node = index.graph().node(symbol).orElse(null);
        if (node == null || node.file().isBlank()) {
            return List.of();
        }
        if (!resolvedFiles.contains(node.file()) && resolvedFiles.size() >= fileLimit) {
            return List.of();
        }
        ensureNotInterrupted();
        try {
            List<GraphEdge> edges = semanticCache.fileEdges(
                            node.file(),
                            () -> resolveFileEdges(index, node.file(), parserFactory))
                    .stream()
                    .filter(edge -> comparableSymbolId(edge.sourceId())
                            .equals(comparableSymbolId(symbol)))
                    .toList();
            resolvedFiles.add(node.file());
            return edges;
        } catch (InterruptedException interrupted) {
            throw interrupted;
        } catch (Exception ignored) {
            return List.of();
        }
    }

    private static List<GraphEdge> resolveIncoming(
            ProjectSnapshot index,
            String target,
            ProjectSemanticCache semanticCache,
            Supplier<JavaParser> parserFactory,
            int fileLimit,
            boolean structureOnly,
            String relation
    ) throws Exception {
        GraphNode targetNode = index.graph().node(target).orElse(null);
        if (targetNode == null) {
            return List.of();
        }
        String incomingKey = target + "|" + fileLimit + "|" + structureOnly + "|" + relation;
        return semanticCache.incomingEdges(incomingKey, () -> resolveIncomingUncached(
                index, target, targetNode, semanticCache, parserFactory, fileLimit,
                structureOnly, relation));
    }

    private static List<GraphEdge> resolveIncomingOverrides(
            ProjectSnapshot index, String target, ProjectSemanticCache cache,
            Supplier<JavaParser> parserFactory, int fileLimit
    ) throws Exception {
        String key = "OVERRIDE|" + methodName(target) + "|" + methodArity(target);
        return cache.incomingEdges("override:" + target + "|" + fileLimit, () -> {
            List<GraphEdge> result = new ArrayList<>();
            for (String file : cache.indexedCandidateFiles(index, key).stream().limit(fileLimit).toList()) {
                ensureNotInterrupted();
                cache.fileEdges(file, () -> resolveFileEdges(index, file, parserFactory)).stream()
                        .filter(edge -> edge.kind() == GraphEdgeKind.OVERRIDES)
                        .filter(edge -> comparableSymbolId(edge.targetId()).equals(comparableSymbolId(target)))
                        .forEach(result::add);
            }
            return result;
        });
    }

    private static List<GraphEdge> resolveIncomingUncached(
            ProjectSnapshot index,
            String target,
            GraphNode targetNode,
            ProjectSemanticCache semanticCache,
            Supplier<JavaParser> parserFactory,
            int fileLimit,
            boolean structureOnly,
            String relation
    ) throws Exception {
        String methodName = methodName(target);
        int arity = methodArity(target);
        String fieldName = fieldName(target);
        List<GraphEdge> result = new ArrayList<>();
        String candidateKey = candidateKey(
                targetNode, methodName, arity, fieldName, target, relation);
        List<String> candidateFiles = relation.equals("entrypoints")
                ? List.of(targetNode.file())
                : semanticCache.indexedCandidateFiles(index, candidateKey);
        int resolvedFiles = 0;
        for (String file : candidateFiles) {
            if (resolvedFiles >= fileLimit) {
                break;
            }
            ensureNotInterrupted();
            boolean candidate;
            CompilationUnit plain = index.astUnits().get(file);
            if (plain == null) {
                continue;
            }
            if (relation.equals("entrypoints")) {
                // 框架入口关系由目标方法的注解生成，只需解析目标文件，无需扫描整个仓库。
                candidate = targetNode.kind() == GraphNodeKind.METHOD
                        || targetNode.kind() == GraphNodeKind.CONSTRUCTOR;
            } else if (targetNode.kind() == GraphNodeKind.CONSTRUCTOR) {
                candidate = plain.findAll(ObjectCreationExpr.class).stream().anyMatch(call ->
                        call.getType().getNameAsString().equals(methodName.replace("<init>", ""))
                                && call.getArguments().size() == arity);
            } else if (targetNode.kind() == GraphNodeKind.METHOD) {
                candidate = plain.findAll(MethodCallExpr.class).stream().anyMatch(call ->
                        call.getNameAsString().equals(methodName)
                                && call.getArguments().size() == arity);
            } else if (targetNode.kind() == GraphNodeKind.FIELD) {
                candidate = plain.findAll(NameExpr.class).stream().anyMatch(
                        name -> name.getNameAsString().equals(fieldName))
                        || plain.findAll(FieldAccessExpr.class).stream().anyMatch(
                        field -> field.getNameAsString().equals(fieldName));
            } else if (targetNode.kind() == GraphNodeKind.TYPE) {
                candidate = relation.equals("type_users")
                        ? plain.findAll(ClassOrInterfaceType.class).stream()
                                .anyMatch(type -> simpleTypeName(type).equals(typeName(target)))
                        : plain.findAll(ClassOrInterfaceDeclaration.class).stream().anyMatch(
                                declaration -> declaration.getExtendedTypes().stream()
                                        .anyMatch(type -> simpleTypeName(type).equals(typeName(target)))
                                        || declaration.getImplementedTypes().stream()
                                        .anyMatch(type -> simpleTypeName(type).equals(typeName(target))));
            } else {
                candidate = false;
            }
            if (!candidate) {
                continue;
            }
            List<GraphEdge> edges;
            try {
                boolean targetMethod = targetNode.kind() == GraphNodeKind.METHOD
                        || targetNode.kind() == GraphNodeKind.CONSTRUCTOR;
                String edgeCacheKey = structureOnly && targetMethod
                        ? "target:" + comparableSymbolId(target) + "|" + file
                        : file;
                edges = semanticCache.fileEdges(
                        edgeCacheKey,
                        () -> structureOnly && targetMethod
                                ? resolveTargetMethodEdges(index, file, target, parserFactory)
                                : resolveFileEdges(index, file, parserFactory));
                resolvedFiles++;
            } catch (InterruptedException interrupted) {
                throw interrupted;
            } catch (Exception ignored) {
                // 语义解析失败的文件不占反向查询的有效文件预算，继续处理下一个候选。
                continue;
            }
            edges.stream()
                    .filter(edge -> comparableSymbolId(edge.targetId())
                            .equals(comparableSymbolId(target))
                            || (edge.resolution() != ResolutionStatus.RESOLVED
                                    && unresolvedMethodTarget(target).equals(edge.targetId())))
                    .forEach(result::add);
        }
        return result;
    }

    /**
     * 解析候选文件中是否存在指向目标符号的调用。
     *
     * 词法索引负责筛选候选文件，具体关系由 Symbol Solver 确认，
     * 不额外构建候选文件中所有无关的语义关系。
     */
    private static List<GraphEdge> resolveTargetMethodEdges(
            ProjectSnapshot index,
            String file,
            String target,
            Supplier<JavaParser> parserFactory
    ) {
        String source = index.sources().get(file);
        if (source == null) {
            return List.of();
        }
        CompilationUnit unit;
        try {
            unit = parserFactory.get().parse(source).getResult().orElse(null);
        } catch (Exception exception) {
            return List.of();
        }
        if (unit == null) {
            return List.of();
        }
        String methodName = methodName(target);
        int arity = methodArity(target);
        List<GraphEdge> result = new ArrayList<>();
        if (methodName.startsWith("<init>")) {
            for (ObjectCreationExpr call : unit.findAll(ObjectCreationExpr.class)) {
                if (!call.getType().getNameAsString().equals(methodName.replace("<init>", ""))
                        || call.getArguments().size() != arity) continue;
                String caller = enclosingCallableId(index.graph(), call, file);
                if (caller != null) result.add(constructorEdge(caller, call, file));
            }
            return result;
        }
        for (MethodCallExpr call : unit.findAll(MethodCallExpr.class)) {
            if (!call.getNameAsString().equals(methodName)
                    || call.getArguments().size() != arity) {
                continue;
            }
            String caller = enclosingCallableId(index.graph(), call, file);
            if (caller == null) {
                continue;
            }
            String targetId;
            ResolutionStatus status;
            try {
                ResolvedMethodDeclaration resolved = call.resolve();
                if (ambiguousSourceOverload(resolved)) {
                    throw new IllegalStateException("ambiguous_source_overload");
                }
                targetId = resolvedMethodId(resolved);
                status = ResolutionStatus.RESOLVED;
            } catch (Exception exception) {
                targetId = "unresolved:method:" + methodName + "/" + arity;
                status = ResolutionStatus.UNRESOLVED;
            }
            result.add(new GraphEdge(
                    caller,
                    targetId,
                    GraphEdgeKind.CALLS,
                    file,
                    call.getBegin().map(position -> position.line).orElse(1),
                    SourceSet.fromPath(file),
                    status,
                    "java-symbol-solver"));
        }
        return result;
    }

    private static String enclosingCallableId(ProjectCodeGraph graph, Node node, String file) {
        int line = node.getBegin().map(position -> position.line).orElse(-1);
        return graph.symbolsInFile(file).stream()
                .filter(candidate -> candidate.kind() == GraphNodeKind.METHOD
                        || candidate.kind() == GraphNodeKind.CONSTRUCTOR)
                .filter(candidate -> candidate.startLine() <= line && candidate.endLine() >= line)
                .min(Comparator.comparingInt(candidate ->
                        candidate.endLine() - candidate.startLine()))
                .map(GraphNode::id)
                .orElse(null);
    }

    private static String candidateKey(
            GraphNode targetNode,
            String methodName,
            int arity,
            String fieldName,
            String target,
            String relation
    ) {
        return switch (targetNode.kind()) {
            case METHOD -> relation.equals("entrypoints")
                    ? "ENTRYPOINT|" + methodName + "|" + arity
                    : "METHOD|" + methodName + "|" + arity;
            case CONSTRUCTOR -> "CONSTRUCTOR|" + methodName.replace("<init>", "") + "|" + arity;
            case FIELD -> "FIELD|" + fieldName;
            case TYPE -> relation.equals("type_users")
                    ? "TYPE_REF|" + typeName(target)
                    : "TYPE|" + typeName(target);
            default -> targetNode.kind() + "|" + target;
        };
    }

    private static GraphEdge constructorEdge(String caller, ObjectCreationExpr call, String file) {
        String target;
        ResolutionStatus status;
        try {
            var resolved = call.resolve();
            var ast = resolved.toAst();
            String signature = ast.isPresent() && ast.get() instanceof ConstructorDeclaration declaration
                    ? declaration.getSignature().asString() : resolved.getSignature();
            target = "java:" + resolved.declaringType().getQualifiedName() + "#<init>" + signature;
            status = ResolutionStatus.RESOLVED;
        } catch (Exception exception) {
            target = "unresolved:method:<init>" + call.getType().getNameAsString() + "/" + call.getArguments().size();
            status = ResolutionStatus.UNRESOLVED;
        }
        return edge(caller, target, GraphEdgeKind.CALLS, file, call, status, "java-symbol-solver");
    }

    private static List<GraphEdge> resolveFileEdges(
            ProjectSnapshot index,
            String file,
            Supplier<JavaParser> parserFactory
    ) {
        String source = index.sources().get(file);
        if (source == null) {
            return List.of();
        }
        CompilationUnit unit = parserFactory.get().parse(source).getResult().orElse(null);
        if (unit == null) {
            throw new IllegalStateException("semantic parse failed: " + file);
        }
        return resolveIndexedOverrideTargets(index, extractGraph(Map.of(file, unit)).edges());
    }

    /**
     * 使用项目轻量索引确认单文件懒解析中的重写目标。
     *
     * 目标与索引中的声明精确匹配时，将关系标记为已解析；不额外扫描目标文件。
     */
    private static List<GraphEdge> resolveIndexedOverrideTargets(
            ProjectSnapshot index,
            List<GraphEdge> edges
    ) {
        return edges.stream()
                .map(edge -> {
                    if (edge.kind() != GraphEdgeKind.OVERRIDES
                            || edge.resolution() == ResolutionStatus.RESOLVED) {
                        return edge;
                    }
                    String target = canonicalNodeId(index, edge.targetId());
                    if (index.graph().node(target).isEmpty()) {
                        return edge;
                    }
                    return new GraphEdge(
                            edge.sourceId(),
                            target,
                            edge.kind(),
                            edge.file(),
                            edge.line(),
                            edge.sourceSet(),
                            ResolutionStatus.RESOLVED,
                            edge.extractor());
                })
                .toList();
    }

    private static void ensureNotInterrupted() throws InterruptedException {
        if (Thread.currentThread().isInterrupted()) {
            throw new InterruptedException("lazy graph expansion interrupted");
        }
    }

    private static String methodName(String symbol) {
        int hash = symbol.lastIndexOf('#');
        int open = symbol.indexOf('(', hash + 1);
        return hash >= 0 && open > hash ? symbol.substring(hash + 1, open) : "";
    }

    private static String unresolvedMethodTarget(String symbol) {
        String name = methodName(symbol);
        int arity = methodArity(symbol);
        return name.isBlank() || arity < 0 ? "" : "unresolved:method:" + name + "/" + arity;
    }

    private static int methodArity(String symbol) {
        int open = symbol.indexOf('(');
        int close = symbol.lastIndexOf(')');
        if (open < 0 || close < open) {
            return -1;
        }
        String parameters = symbol.substring(open + 1, close).trim();
        if (parameters.isEmpty()) {
            return 0;
        }
        int depth = 0;
        int count = 1;
        for (int i = 0; i < parameters.length(); i++) {
            char c = parameters.charAt(i);
            if (c == '<') depth++;
            else if (c == '>') depth = Math.max(0, depth - 1);
            else if (c == ',' && depth == 0) count++;
        }
        return count;
    }

    private static String fieldName(String symbol) {
        int hash = symbol.lastIndexOf('#');
        return hash >= 0 ? symbol.substring(hash + 1) : symbol;
    }

    private static String pathKind(String input) {
        try {
            JsonNode root = JSON.readTree(input == null ? "" : input);
            return root.path("path_kind").asText("").trim();
        } catch (Exception ignored) {
            return "";
        }
    }

    private static String simpleTypeName(ClassOrInterfaceType type) {
        String value = type.getNameWithScope();
        int separator = value.lastIndexOf('.');
        return separator >= 0 ? value.substring(separator + 1) : value;
    }

    private static String typeName(String symbol) {
        int separator = symbol.lastIndexOf('.');
        return separator >= 0 ? symbol.substring(separator + 1) : symbol;
    }

    private static void addAnnotationEdges(
            List<GraphEdge> edges,
            String source,
            NodeWithAnnotations<?> declaration,
            String file
    ) {
        declaration.getAnnotations().forEach(annotation ->
                edges.add(edge(source,
                        "annotation:" + annotation.getNameAsString(),
                        GraphEdgeKind.ANNOTATED_WITH,
                        file,
                        annotation,
                        ResolutionStatus.RESOLVED,
                        "java-annotations")));
    }

    private static void addOverrideEdges(
            List<GraphNode> nodes,
            List<GraphEdge> edges,
            MethodDeclaration method,
            String methodId,
            String file
    ) {
        ClassOrInterfaceDeclaration owner = method
                .findAncestor(ClassOrInterfaceDeclaration.class)
                .orElse(null);
        if (owner == null) {
            return;
        }
        // Java 方法重写不要求声明 @Override，须解析实际继承关系。
        try {
            ResolvedMethodDeclaration implementation = method.resolve();
            boolean found = false;
            for (var ancestor : owner.resolve().getAllAncestors()) {
                if (ancestor.getTypeDeclaration().isEmpty()) {
                    continue;
                }
                for (var inherited : ancestor.getTypeDeclaration().orElseThrow().getDeclaredMethods()) {
                    String access = inherited.accessSpecifier().name();
                    if (inherited.isStatic() || access.equals("PRIVATE")
                            || (!access.equals("PUBLIC") && !access.equals("PROTECTED")
                            && !inherited.declaringType().getPackageName().equals(
                                    implementation.declaringType().getPackageName()))
                            || !inherited.getSignature().equals(implementation.getSignature())) {
                        continue;
                    }
                    String target = resolvedMethodId(inherited);
                    boolean indexed = nodes.stream().anyMatch(node -> node.id().equals(target));
                    edges.add(edge(methodId, target, GraphEdgeKind.OVERRIDES, file, method,
                            indexed ? ResolutionStatus.RESOLVED : ResolutionStatus.UNRESOLVED,
                            "java-override"));
                    found = true;
                }
            }
            if (found || method.getAnnotationByName("Override").isEmpty()) {
                return;
            }
        } catch (Exception ignored) {
            // 求解失败时，仅保留有显式注解依据的未解析关系，不推断无注解的方法重写。
            if (method.getAnnotationByName("Override").isEmpty()) {
                return;
            }
        }
        List<ClassOrInterfaceType> parents = new ArrayList<>();
        parents.addAll(owner.getExtendedTypes());
        parents.addAll(owner.getImplementedTypes());
        if (parents.isEmpty()) {
            edges.add(edge(methodId,
                    "unresolved:override:" + method.getSignature(),
                    GraphEdgeKind.OVERRIDES, file, method,
                    ResolutionStatus.UNRESOLVED, "java-override"));
            return;
        }
        for (ClassOrInterfaceType parent : parents) {
            String target;
            try {
                target = "java:" + parent.resolve().asReferenceType().getQualifiedName()
                        + "#" + method.getSignature().asString();
            } catch (Exception exception) {
                target = "unresolved:override:" + parent.getNameAsString()
                        + "#" + method.getSignature();
            }
            String targetId = target;
            boolean resolved = nodes.stream().anyMatch(node -> node.id().equals(targetId));
            edges.add(edge(methodId, targetId, GraphEdgeKind.OVERRIDES, file, method,
                    resolved ? ResolutionStatus.RESOLVED : ResolutionStatus.UNRESOLVED,
                    "java-override"));
        }
    }

    private static void addFieldAccessEdge(
            List<GraphEdge> edges,
            Map<Node, String> symbolIds,
            String file,
            Node expression
    ) {
        String source = enclosingCallableId(expression, symbolIds);
        if (source == null) {
            return;
        }
        String target;
        try {
            var resolved = expression instanceof NameExpr name
                    ? name.resolve()
                    : ((FieldAccessExpr) expression).resolve();
            if (!resolved.isField()) {
                return;
            }
            var field = resolved.asField();
            target = "java:" + field.declaringType().getQualifiedName()
                    + "#" + field.getName();
        } catch (Exception exception) {
            target = "unresolved-field:" + expression;
            GraphEdgeKind unresolvedKind = isWrite(expression)
                    ? GraphEdgeKind.WRITES_FIELD : GraphEdgeKind.READS_FIELD;
            edges.add(edge(source, target, unresolvedKind, file, expression,
                    ResolutionStatus.UNRESOLVED, "java-symbol-solver"));
            return;
        }
        GraphEdgeKind kind = isWrite(expression)
                ? GraphEdgeKind.WRITES_FIELD : GraphEdgeKind.READS_FIELD;
        edges.add(edge(source, target, kind, file, expression,
                ResolutionStatus.RESOLVED, "java-symbol-solver"));
    }

    private static boolean isWrite(Node expression) {
        Node parent = expression.getParentNode().orElse(null);
        if (parent instanceof AssignExpr assign && assign.getTarget() == expression) {
            return true;
        }
        return parent instanceof UnaryExpr unary
                && switch (unary.getOperator()) {
                    case PREFIX_INCREMENT, PREFIX_DECREMENT,
                            POSTFIX_INCREMENT, POSTFIX_DECREMENT -> true;
                    default -> false;
                };
    }

    private static void addTypeEdge(
            List<GraphEdge> edges,
            String source,
            ClassOrInterfaceType type,
            GraphEdgeKind kind,
            String file
    ) {
        String target;
        ResolutionStatus status;
        try {
            target = "java:" + type.resolve().asReferenceType().getQualifiedName();
            status = ResolutionStatus.RESOLVED;
        } catch (Exception exception) {
            target = "unresolved:type:" + type.getNameWithScope();
            status = ResolutionStatus.UNRESOLVED;
        }
        edges.add(edge(source, target, kind, file, type, status, "java-symbol-solver"));
    }

    private static void addFrameworkNodes(
            List<GraphNode> nodes,
            List<GraphEdge> edges,
            NodeWithAnnotations<?> declaration,
            String methodId,
            String file
    ) {
        for (AnnotationExpr annotation : declaration.getAnnotations()) {
            String name = annotation.getName().getIdentifier();
            GraphEdgeKind kind = null;
            if (ROUTE_ANNOTATIONS.contains(name)) {
                kind = GraphEdgeKind.EXPOSES_ROUTE;
            } else if (EVENT_ANNOTATIONS.contains(name)) {
                kind = GraphEdgeKind.LISTENS_TO_EVENT;
            } else if (SCHEDULE_ANNOTATIONS.contains(name)) {
                kind = GraphEdgeKind.SCHEDULED_BY;
            }
            if (kind == null) {
                continue;
            }
            String signature = "@" + name + annotationValue(annotation);
            String entryId = "framework:" + methodId + ":" + name;
            nodes.add(node(entryId, GraphNodeKind.FRAMEWORK_ENTRYPOINT, file, annotation,
                    signature, methodId, List.of(name)));
            edges.add(edge(entryId, methodId, kind, file, annotation,
                    ResolutionStatus.RESOLVED, "spring-annotations"));
        }
    }

    private static String annotationValue(AnnotationExpr annotation) {
        if (annotation instanceof SingleMemberAnnotationExpr single) {
            return "(" + single.getMemberValue() + ")";
        }
        if (annotation instanceof NormalAnnotationExpr normal) {
            return "(" + normal.getPairs() + ")";
        }
        return "";
    }

    private static GraphNode node(
            String id,
            GraphNodeKind kind,
            String file,
            Node source,
            String signature,
            String owner,
            List<String> annotations
    ) {
        int start = source.getBegin().map(position -> position.line).orElse(1);
        int end = source.getEnd().map(position -> position.line).orElse(start);
        return new GraphNode(
                id, kind, file, start, end, signature, owner,
                SourceSet.fromPath(file), annotations);
    }

    private static GraphEdge edge(
            String source,
            String target,
            GraphEdgeKind kind,
            String file,
            Node location,
            ResolutionStatus status,
            String extractor
    ) {
        return new GraphEdge(source, target, kind, file,
                location.getBegin().map(position -> position.line).orElse(1),
                SourceSet.fromPath(file), status, extractor);
    }

    private static String qualifiedTypeName(
            TypeDeclaration<?> type,
            CompilationUnit unit,
            String file
    ) {
        if (type instanceof ClassOrInterfaceDeclaration declaration) {
            try {
                return declaration.resolve().getQualifiedName();
            } catch (Exception ignored) {
                // 使用源码包名和嵌套类型名作为稳定降级。
            }
        }
        List<String> names = new ArrayList<>();
        Node current = type;
        while (current instanceof TypeDeclaration<?> declaration) {
            names.add(0, declaration.getNameAsString());
            current = declaration.getParentNode().orElse(null);
        }
        String prefix = unit.getPackageDeclaration()
                .map(declaration -> declaration.getNameAsString() + ".")
                .orElse("");
        return prefix + (names.isEmpty() ? file : String.join(".", names));
    }

    private static String ownerId(Node node, Map<Node, String> ids, String file) {
        return node.findAncestor(TypeDeclaration.class)
                .map(ids::get)
                .orElse("file:" + file);
    }

    private static String methodId(MethodDeclaration method, String owner) {
        try {
            return resolvedMethodId(method.resolve());
        } catch (Exception exception) {
            return owner + "#" + method.getSignature().asString();
        }
    }

    private static String resolvedMethodId(ResolvedMethodDeclaration method) {
        // 使用解析后的声明统一源码索引和语义边的符号身份。
        // 解析签名保留参数限定类型和泛型信息，以区分导入、重载和类型特化。
        var declaration = method.toAst();
        if (declaration.isPresent() && declaration.get() instanceof MethodDeclaration source) {
            return "java:" + method.declaringType().getQualifiedName()
                    + "#" + source.getSignature().asString();
        }
        return "java:" + method.declaringType().getQualifiedName()
                + "#" + method.getSignature();
    }

    private static boolean ambiguousSourceOverload(ResolvedMethodDeclaration method) {
        var ast = method.toAst();
        if (ast.isEmpty() || !(ast.get() instanceof MethodDeclaration source)) return false;
        var owner = source.findAncestor(TypeDeclaration.class);
        if (owner.isEmpty()) return false;
        // 参数简单名称相同但所属包不同的重载可能产生歧义。
        // 无法唯一确认目标时保留未解析调用，不将不确定选择发布为已解析关系。
        String simple = simpleSignature(source);
        TypeDeclaration<?> declaration = owner.get();
        return declaration.getMethodsByName(source.getNameAsString()).stream()
                .filter(other -> !other.getSignature().equals(source.getSignature()))
                .anyMatch(other -> simpleSignature(other).equals(simple));
    }

    private static String simpleSignature(MethodDeclaration method) {
        return method.getSignature().asString().replaceAll("\\s+", "")
                .replaceAll("[A-Za-z_$][\\w$]*\\.", "");
    }

    private static String enclosingCallableId(Node node, Map<Node, String> ids) {
        return node.findAncestor(CallableDeclaration.class).map(ids::get).orElse(null);
    }

    private static List<String> annotations(NodeWithAnnotations<?> node) {
        return node.getAnnotations().stream()
                .map(annotation -> annotation.getName().getIdentifier())
                .toList();
    }

    private static String normalize(Path path) {
        return path.toString().replace('\\', '/');
    }

    /** 收集某文件定义的全部类型 FQCN(含嵌套类型,用 $ 连接),供快速失败层匹配。 */
    private static void collectTypeNames(CompilationUnit unit, Set<String> projectTypes) {
        String prefix = unit.getPackageDeclaration()
                .map(declaration -> declaration.getNameAsString() + ".")
                .orElse("");
        for (TypeDeclaration<?> type : unit.findAll(TypeDeclaration.class)) {
            projectTypes.add(prefix + nestedTypeName(type));
        }
    }

    private static String nestedTypeName(TypeDeclaration<?> type) {
        StringBuilder name = new StringBuilder(type.getNameAsString());
        Node current = type.getParentNode().orElse(null);
        while (current instanceof TypeDeclaration<?> declaration) {
            name.insert(0, declaration.getNameAsString() + "$");
            current = declaration.getParentNode().orElse(null);
        }
        return name.toString();
    }

    /**
     * 限定解析范围的项目类型求解器。
     *
     * 处理 JDK 和项目内类型；对范围之外的第三方类型直接返回未解析结果，
     * 避免对不在源码索引中的类型反复扫描目录。
     */
    private static final class ProjectAwareTypeSolver implements TypeSolver {
        private final Set<String> projectTypes;
        private final TypeSolver delegate;
        private TypeSolver parent;

        ProjectAwareTypeSolver(Set<String> projectTypes, TypeSolver delegate) {
            this.projectTypes = projectTypes;
            this.delegate = delegate;
        }

        @Override
        public TypeSolver getParent() {
            return parent;
        }

        @Override
        public void setParent(TypeSolver parent) {
            this.parent = parent;
        }

        @Override
        public SymbolReference<ResolvedReferenceTypeDeclaration> tryToSolveType(String name) {
            if (isJdkType(name) || projectTypes.contains(name)) {
                return delegate.tryToSolveType(name);
            }
            return SymbolReference.unsolved(ResolvedReferenceTypeDeclaration.class);
        }

        private static boolean isJdkType(String name) {
            return name.startsWith("java.") || name.startsWith("javax.")
                    || name.startsWith("jdk.") || name.startsWith("sun.")
                    || name.startsWith("com.sun.");
        }
    }
}
