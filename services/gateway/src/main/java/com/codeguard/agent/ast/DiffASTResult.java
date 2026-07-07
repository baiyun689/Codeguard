package com.codeguard.agent.ast;

import java.util.List;

/**
 * 单个 Java 文件的 AST 分析结果。
 * <p>
 * 纯数据容器——不含任何解析逻辑。由 {@link DiffASTAnalyzer} 填充，
 * 供 {@link ASTContextFormatter} 消费。
 */
public record DiffASTResult(
        String filePath,
        boolean parseSucceeded,
        List<ClassDef> classes,
        List<MethodDef> methods,
        List<CFNode> controlFlowNodes,
        List<CallEdgeDef> callEdges) {

    /** 类/接口/枚举/record 的结构信息。 */
    public record ClassDef(
            String name,
            String type,        // "class" / "interface" / "enum" / "record"
            String superClass,  // extends 的父类名，无则为空串
            List<String> interfaces, // implements 的接口名列表
            List<String> fields,     // "type name" 格式的字段列表
            int startLine,
            int endLine) {}

    /** 方法/构造器的结构信息。 */
    public record MethodDef(
            String name,
            String returnType,
            List<String> paramTypes,
            List<String> paramNames,
            String visibility,      // "public" / "private" / "protected" / "package-private"
            List<String> modifiers, // static / final / synchronized / abstract
            List<String> annotations, // @Override / @Deprecated 等
            int startLine,
            int endLine) {}

    /** 控制流节点。 */
    public record CFNode(
            String type,        // IF / FOR / FOR_EACH / WHILE / DO_WHILE / TRY_CATCH / SWITCH / SYNCHRONIZED
            int startLine,
            int endLine,
            String condition) {}  // 条件文本，截断到 60 字符

    /** 调用边。 */
    public record CallEdgeDef(
            String callerMethod,  // 发起调用的方法名
            String calleeMethod,  // 被调用的方法名
            String calleeScope,   // 调用表达式的作用域（如 userService.save → "userService"），无为 ""
            int line) {}
}
