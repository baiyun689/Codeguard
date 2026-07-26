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
import com.github.javaparser.ast.expr.AssignExpr;
import com.github.javaparser.ast.expr.FieldAccessExpr;
import com.github.javaparser.ast.expr.NameExpr;
import com.github.javaparser.ast.expr.UnaryExpr;
import com.github.javaparser.ast.expr.NormalAnnotationExpr;
import com.github.javaparser.ast.expr.SingleMemberAnnotationExpr;
import com.github.javaparser.ast.nodeTypes.NodeWithAnnotations;
import com.github.javaparser.ast.type.ClassOrInterfaceType;
import com.github.javaparser.resolution.declarations.ResolvedMethodDeclaration;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.CombinedTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.JavaParserTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.ReflectionTypeSolver;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Stream;

final class ProjectSnapshotBuilder {
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
        CombinedTypeSolver solvers = new CombinedTypeSolver(new ReflectionTypeSolver(false));
        discoverSourceRoots(root, javaFiles).forEach(path -> solvers.add(new JavaParserTypeSolver(path)));
        JavaParser parser = new JavaParser(new ParserConfiguration()
                .setStoreTokens(true)
                .setAttributeComments(true)
                .setSymbolResolver(new JavaSymbolSolver(solvers)));

        Map<String, String> sources = new LinkedHashMap<>();
        Map<String, CompilationUnit> units = new LinkedHashMap<>();
        for (Path file : javaFiles) {
            String relative = normalize(root.relativize(file));
            try {
                String source = Files.readString(file);
                sources.put(relative, source);
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
                    file, "", List.of()));
            for (TypeDeclaration<?> type : unit.findAll(TypeDeclaration.class)) {
                String typeId = "java:" + qualifiedTypeName(type, unit, file);
                symbolIds.put(type, typeId);
                nodes.add(node(typeId, GraphNodeKind.TYPE, file, type,
                        type.getNameAsString(), fileId, annotations(type)));
                edges.add(edge(fileId, typeId, GraphEdgeKind.DECLARES, file, type,
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
                if (method.getAnnotationByName("Override").isPresent()) {
                    addOverrideEdges(nodes, edges, method, id, file);
                }
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
            MethodDeclaration method,
            String methodId,
            String file
    ) {
        for (AnnotationExpr annotation : method.getAnnotations()) {
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
        return new GraphNode(id, kind, file, start, end, signature, owner, annotations);
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
                status, extractor);
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
        return "java:" + method.declaringType().getQualifiedName()
                + "#" + method.getSignature();
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
}
