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
final class OutboxDelivery2OracleTest {
    @Test
    @DisplayName("触发: 发送超时或 broker 拒绝；后果: 事件被永久标记完成但实际未送达")
    void deliverOneEvent_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/OutboxDeliveryService.java"));
        assertAll(
        () -> assertTrue(source.contains("OutboxEvent event = outbox.findReady(Instant.now(), 1).stream().findFirst().orElseThrow();"), "missing seeded evidence: OutboxEvent event = outbox.findReady(Instant.now(), 1).stream().findFirst().orElseThrow();"),
        () -> assertTrue(source.contains("outbox.save(event.sent());"), "missing seeded evidence: outbox.save(event.sent());"),
        () -> assertTrue(source.contains("events.publish(\"outbox\", event.aggregateId(), event.payload());"), "missing seeded evidence: events.publish(\"outbox\", event.aggregateId(), event.payload());")
        );
    }
}
