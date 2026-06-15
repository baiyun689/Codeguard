package com.codeguard.agent.repomap;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.ConstructorDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.VariableDeclarator;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.ObjectCreationExpr;
import com.github.javaparser.ast.type.ClassOrInterfaceType;

import java.util.ArrayList;
import java.util.List;

/**
 * 用 JavaParser 从单个 Java 源文件抽取符号 tag(def/ref)。
 * <p>
 * 确定性纯函数:同一份源码必产出同一组 tag。无法解析的源码返回空列表(由上层跳过并记录),
 * 不抛异常 —— 审查仓库里混入语法错误/非标准文件是常态,不能让一处坏文件拖垮整次建图。
 * <p>
 * ref 按**简单名**抽取(方法调用名、被 new/引用的类型名),不做 SymbolSolver 全限定解析
 * (见 design.md D3):repo map 只产出"导航候选",精确性由审查员后续 get_file_content 细读兜底,
 * 换取免去 classpath 配置的复杂度。
 */
public final class TagExtractor {

    /** 解析源码并抽取 tag;解析失败返回空列表。 */
    public List<Tag> extract(String relFile, String source) {
        List<Tag> tags = new ArrayList<>();
        CompilationUnit cu;
        try {
            ParseResult<CompilationUnit> result = new JavaParser().parse(source);
            if (!result.isSuccessful() || result.getResult().isEmpty()) {
                return tags;
            }
            cu = result.getResult().get();
        } catch (Exception e) {
            // 解析器内部异常也视为"不可解析",跳过。
            return tags;
        }

        // --- DEF:类/接口、方法、构造器、字段 ---
        cu.findAll(ClassOrInterfaceDeclaration.class).forEach(decl -> {
            String kw = decl.isInterface() ? "interface " : "class ";
            tags.add(Tag.def(relFile, decl.getNameAsString(), lineOf(decl), kw + decl.getNameAsString()));
        });
        cu.findAll(MethodDeclaration.class).forEach(decl ->
                tags.add(Tag.def(relFile, decl.getNameAsString(), lineOf(decl),
                        // 签名:不含修饰符/throws,含参数名 —— 给审查员看"怎么调"足够。
                        decl.getDeclarationAsString(false, false, true))));
        cu.findAll(ConstructorDeclaration.class).forEach(decl ->
                tags.add(Tag.def(relFile, decl.getNameAsString(), lineOf(decl),
                        decl.getDeclarationAsString(false, false))));
        cu.findAll(FieldDeclaration.class).forEach(field -> {
            String type = field.getElementType().asString();
            for (VariableDeclarator var : field.getVariables()) {
                tags.add(Tag.def(relFile, var.getNameAsString(), lineOf(var),
                        type + " " + var.getNameAsString()));
            }
        });

        // --- REF:方法调用、new、类型引用 ---
        cu.findAll(MethodCallExpr.class).forEach(call ->
                tags.add(Tag.ref(relFile, call.getNameAsString(), lineOf(call))));
        cu.findAll(ObjectCreationExpr.class).forEach(neu ->
                tags.add(Tag.ref(relFile, neu.getType().getNameAsString(), lineOf(neu))));
        cu.findAll(ClassOrInterfaceType.class).forEach(type ->
                tags.add(Tag.ref(relFile, type.getNameAsString(), lineOf(type))));

        return tags;
    }

    private static int lineOf(com.github.javaparser.ast.Node node) {
        return node.getBegin().map(p -> p.line).orElse(-1);
    }
}
