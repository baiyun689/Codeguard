package com.codeguard.agent.repomap;

/**
 * 源码中的一个符号标记(tag)—— repo map 的最小单元。
 * <p>
 * 借鉴 aider repo map 的 Tag 概念(rel_fname/name/kind/line),抽取栈换成 JavaParser。
 * 分两类:
 * <ul>
 *   <li>{@link Kind#DEF} 定义(类/接口/方法/构造器/字段)—— 携带 {@code signature}(签名级文本),
 *       渲染时只输出签名、不输出实现体;</li>
 *   <li>{@link Kind#REF} 引用(方法调用/类型引用)—— 只需符号名用于建图连边,{@code signature} 为 {@code null}。</li>
 * </ul>
 *
 * @param relFile   相对仓库根的正斜杠路径
 * @param name      符号名(简单名,按名建图,不做全限定解析,见 design.md D3)
 * @param kind      DEF 或 REF
 * @param line      源码行号(1 基);REF 用调用点行号
 * @param signature DEF 的签名级文本(如 {@code int add(int a, int b)});REF 为 {@code null}
 */
public record Tag(String relFile, String name, Kind kind, int line, String signature) {

    public enum Kind {DEF, REF}

    public static Tag def(String relFile, String name, int line, String signature) {
        return new Tag(relFile, name, Kind.DEF, line, signature);
    }

    public static Tag ref(String relFile, String name, int line) {
        return new Tag(relFile, name, Kind.REF, line, null);
    }
}
