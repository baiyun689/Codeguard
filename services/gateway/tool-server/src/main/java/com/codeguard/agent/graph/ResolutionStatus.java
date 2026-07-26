package com.codeguard.agent.graph;

/** 静态事实的解析可靠性；未知绝不能被解释为不存在。 */
public enum ResolutionStatus {
    RESOLVED,
    AMBIGUOUS,
    UNRESOLVED
}
