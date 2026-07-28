package com.tradeflow.domain;

import java.time.Instant;

public record OutboxEvent(
        String id, String aggregateId, long version, String payload,
        String state, int attempts, Instant availableAt) {
    public OutboxEvent sent() {
        return new OutboxEvent(id, aggregateId, version, payload, "SENT", attempts, availableAt);
    }
    public OutboxEvent retryAt(Instant next) {
        return new OutboxEvent(id, aggregateId, version, payload, "PENDING", attempts + 1, next);
    }
}
