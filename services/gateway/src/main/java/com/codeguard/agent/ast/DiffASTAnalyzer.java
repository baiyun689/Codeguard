package com.codeguard.agent.ast;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ast.AccessSpecifier;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.Modifier;
import com.github.javaparser.ast.NodeList;
import com.github.javaparser.ast.body.*;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.stmt.*;
import com.github.javaparser.ast.type.ClassOrInterfaceType;

import java.util.ArrayList;
import java.util.List;

/**
 * 用 JavaParser 从单个 Java 源文件抽取完整的 AST 结构信息。
 * <p>
 * 纯函数——无状态、无副作用。解析失败返回 {@code parseSucceeded=false}，不抛异常。
 * 独立于 repomap Tag 体系——本类输出完整级 AST（可见性/注解/控制流/调用边）。
 */
public final class DiffASTAnalyzer {

    private DiffASTAnalyzer() {}

    /**
     * 解析单个 Java 源文件，返回结构化 AST 信息。
     */
    public static DiffASTResult analyze(String filePath, String source) {
        if (source == null || source.isBlank()) {
            return new DiffASTResult(filePath, false, List.of(), List.of(), List.of(), List.of());
        }
        CompilationUnit cu;
        try {
            ParseResult<CompilationUnit> result = new JavaParser().parse(source);
            if (!result.isSuccessful() || result.getResult().isEmpty()) {
                return new DiffASTResult(filePath, false, List.of(), List.of(), List.of(), List.of());
            }
            cu = result.getResult().get();
        } catch (Exception e) {
            return new DiffASTResult(filePath, false, List.of(), List.of(), List.of(), List.of());
        }

        List<DiffASTResult.ClassDef> classes = extractClasses(cu);
        List<DiffASTResult.MethodDef> methods = extractMethods(cu);
        List<DiffASTResult.CFNode> controlFlow = extractControlFlow(cu);
        List<DiffASTResult.CallEdgeDef> callEdges = extractCallEdges(cu);

        return new DiffASTResult(filePath, true, classes, methods, controlFlow, callEdges);
    }

    private static List<DiffASTResult.ClassDef> extractClasses(CompilationUnit cu) {
        List<DiffASTResult.ClassDef> result = new ArrayList<>();
        cu.findAll(ClassOrInterfaceDeclaration.class).forEach(decl -> {
            String type = decl.isInterface() ? "interface" : "class";
            String superClass = "";
            NodeList<ClassOrInterfaceType> extended = decl.getExtendedTypes();
            if (extended != null && extended.isNonEmpty()) {
                superClass = extended.get(0).getNameAsString();
            }
            List<String> interfaces = new ArrayList<>();
            NodeList<ClassOrInterfaceType> implemented = decl.getImplementedTypes();
            if (implemented != null) {
                implemented.forEach(t -> interfaces.add(t.getNameAsString()));
            }
            List<String> fields = new ArrayList<>();
            decl.getFields().forEach(f -> {
                String fieldType = f.getElementType().asString();
                f.getVariables().forEach(v -> fields.add(fieldType + " " + v.getNameAsString()));
            });
            int start = decl.getBegin().map(p -> p.line).orElse(-1);
            int end = decl.getEnd().map(p -> p.line).orElse(-1);
            result.add(new DiffASTResult.ClassDef(
                    decl.getNameAsString(), type, superClass, interfaces, fields, start, end));
        });
        cu.findAll(com.github.javaparser.ast.body.EnumDeclaration.class).forEach(decl -> {
            List<String> interfaces = new ArrayList<>();
            NodeList<ClassOrInterfaceType> implemented = decl.getImplementedTypes();
            if (implemented != null) {
                implemented.forEach(t -> interfaces.add(t.getNameAsString()));
            }
            int start = decl.getBegin().map(p -> p.line).orElse(-1);
            int end = decl.getEnd().map(p -> p.line).orElse(-1);
            result.add(new DiffASTResult.ClassDef(
                    decl.getNameAsString(), "enum", "", interfaces, List.of(), start, end));
        });
        return result;
    }

    private static List<DiffASTResult.MethodDef> extractMethods(CompilationUnit cu) {
        List<DiffASTResult.MethodDef> result = new ArrayList<>();
        cu.findAll(MethodDeclaration.class).forEach(decl ->
                result.add(buildMethodDef(decl.getNameAsString(), decl.getType().asString(),
                        decl.getParameters(), decl.getAccessSpecifier(),
                        decl.getModifiers(),
                        decl.getAnnotations(), decl.getBegin().map(p -> p.line).orElse(-1),
                        decl.getEnd().map(p -> p.line).orElse(-1))));
        cu.findAll(ConstructorDeclaration.class).forEach(decl ->
                result.add(buildMethodDef(decl.getNameAsString(), "",
                        decl.getParameters(), decl.getAccessSpecifier(),
                        decl.getModifiers(),
                        decl.getAnnotations(), decl.getBegin().map(p -> p.line).orElse(-1),
                        decl.getEnd().map(p -> p.line).orElse(-1))));
        return result;
    }

    private static DiffASTResult.MethodDef buildMethodDef(
            String name, String returnType,
            NodeList<Parameter> params,
            AccessSpecifier accessSpec,
            NodeList<Modifier> modifiers,
            NodeList<com.github.javaparser.ast.expr.AnnotationExpr> annotations,
            int start, int end) {
        List<String> paramTypes = new ArrayList<>();
        List<String> paramNames = new ArrayList<>();
        for (Parameter p : params) {
            paramTypes.add(p.getType().asString());
            paramNames.add(p.getNameAsString());
        }
        String visibility = switch (accessSpec) {
            case PUBLIC -> "public";
            case PRIVATE -> "private";
            case PROTECTED -> "protected";
            case NONE -> "package-private";
        };
        List<String> modList = new ArrayList<>();
        for (Modifier m : modifiers) {
            String kw = m.getKeyword().asString();
            if (!kw.equals(visibility)) {
                modList.add(kw);
            }
        }
        List<String> annList = new ArrayList<>();
        for (var ann : annotations) {
            annList.add("@" + ann.getNameAsString());
        }
        return new DiffASTResult.MethodDef(
                name, returnType, paramTypes, paramNames, visibility, modList, annList, start, end);
    }

    private static List<DiffASTResult.CFNode> extractControlFlow(CompilationUnit cu) {
        List<DiffASTResult.CFNode> result = new ArrayList<>();
        cu.findAll(IfStmt.class).forEach(n ->
                result.add(cfNode("IF", n, n.getCondition().toString())));
        cu.findAll(ForStmt.class).forEach(n ->
                result.add(cfNode("FOR", n, n.getCompare().map(Object::toString).orElse(""))));
        cu.findAll(ForEachStmt.class).forEach(n ->
                result.add(cfNode("FOR_EACH", n, n.getIterable().toString())));
        cu.findAll(WhileStmt.class).forEach(n ->
                result.add(cfNode("WHILE", n, n.getCondition().toString())));
        cu.findAll(DoStmt.class).forEach(n ->
                result.add(cfNode("DO_WHILE", n, n.getCondition().toString())));
        cu.findAll(TryStmt.class).forEach(n ->
                result.add(cfNode("TRY_CATCH", n, "")));
        cu.findAll(SwitchStmt.class).forEach(n ->
                result.add(cfNode("SWITCH", n, n.getSelector().toString())));
        cu.findAll(SynchronizedStmt.class).forEach(n ->
                result.add(cfNode("SYNCHRONIZED", n, n.getExpression().toString())));
        return result;
    }

    private static DiffASTResult.CFNode cfNode(String type, com.github.javaparser.ast.Node node, String condition) {
        int start = node.getBegin().map(p -> p.line).orElse(-1);
        int end = node.getEnd().map(p -> p.line).orElse(-1);
        String clipped = condition.length() > 60 ? condition.substring(0, 60) + "..." : condition;
        return new DiffASTResult.CFNode(type, start, end, clipped);
    }

    private static List<DiffASTResult.CallEdgeDef> extractCallEdges(CompilationUnit cu) {
        List<DiffASTResult.CallEdgeDef> result = new ArrayList<>();
        cu.findAll(MethodCallExpr.class).forEach(call -> {
            String caller = findEnclosingMethod(call);
            String callee = call.getNameAsString();
            String scope = call.getScope().map(Expression::toString).orElse("");
            if (caller.isEmpty()) return;
            int line = call.getBegin().map(p -> p.line).orElse(-1);
            result.add(new DiffASTResult.CallEdgeDef(caller, callee, scope, line));
        });
        return result;
    }

    private static String findEnclosingMethod(com.github.javaparser.ast.Node node) {
        com.github.javaparser.ast.Node current = node.getParentNode().orElse(null);
        while (current != null) {
            if (current instanceof MethodDeclaration m) return m.getNameAsString();
            if (current instanceof ConstructorDeclaration c) return c.getNameAsString();
            if (current instanceof CompilationUnit) break;
            current = current.getParentNode().orElse(null);
        }
        return "";
    }
}
