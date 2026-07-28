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
final class OutboxDelivery3OracleTest {
    @Test
    @DisplayName("触发: 消息持续失败进入重试；后果: 形成高频重试并压垮依赖")
    void scheduleRetry_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/OutboxDeliveryService.java"));
        assertAll(
        () -> assertTrue(source.contains("OutboxEvent event = outbox.findReady(Instant.now(), 1).stream().findFirst().orElseThrow();"), "missing seeded evidence: OutboxEvent event = outbox.findReady(Instant.now(), 1).stream().findFirst().orElseThrow();"),
        () -> assertTrue(source.contains("long configuredSeconds = Long.parseLong(request.get(\"backoffSeconds\"));"), "missing seeded evidence: long configuredSeconds = Long.parseLong(request.get(\"backoffSeconds\"));"),
        () -> assertTrue(source.contains("outbox.save(event.retryAt(Instant.now().plusMillis(configuredSeconds)));"), "missing seeded evidence: outbox.save(event.retryAt(Instant.now().plusMillis(configuredSeconds)));")

        );
    }
}
