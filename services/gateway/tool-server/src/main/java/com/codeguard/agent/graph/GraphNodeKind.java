package com.codeguard.agent.graph;

/** 可被工具稳定引用的项目语义节点类型。 */
public enum GraphNodeKind {
    FILE,
    TYPE,
    METHOD,
    CONSTRUCTOR,
    FIELD,
    FRAMEWORK_ENTRYPOINT
}
