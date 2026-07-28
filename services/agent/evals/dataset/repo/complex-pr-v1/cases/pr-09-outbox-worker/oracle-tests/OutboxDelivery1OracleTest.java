package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertAll;
import static org.junit.jupiter.api.Assertions.assertTrue;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * Evaluator-only static contract oracle. It is excluded from the
 * reviewed project snapshot.
 */
final class OutboxDelivery1OracleTest {
    @Test
    @DisplayName("触发: 两个 worker 同时轮询；后果: 相同业务事件重复投递")
    void deliverReadyEvents_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/OutboxDeliveryService.java"));
        assertAll(
        () -> assertTrue(source.contains("List<OutboxEvent> ready = outbox.findReady(Instant.now(), 100);"), "missing seeded evidence: List<OutboxEvent> ready = outbox.findReady(Instant.now(), 100);"),
        () -> assertTrue(source.contains("ready.forEach(event -> events.publish(\"outbox\", event.aggregateId(), event.payload()));"), "missing seeded evidence: ready.forEach(event -> events.publish(\"outbox\", event.aggregateId(), event.payload()));")

        );
    }
}
