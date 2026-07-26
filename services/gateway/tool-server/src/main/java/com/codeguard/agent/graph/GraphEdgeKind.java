package com.codeguard.agent.graph;

/** 项目语义图中可查询的事实关系。 */
public enum GraphEdgeKind {
    DECLARES,
    CALLS,
    READS_FIELD,
    WRITES_FIELD,
    REFERENCES_TYPE,
    EXTENDS,
    IMPLEMENTS,
    OVERRIDES,
    ANNOTATED_WITH,
    INJECTS,
    EXPOSES_ROUTE,
    LISTENS_TO_EVENT,
    SCHEDULED_BY
}
