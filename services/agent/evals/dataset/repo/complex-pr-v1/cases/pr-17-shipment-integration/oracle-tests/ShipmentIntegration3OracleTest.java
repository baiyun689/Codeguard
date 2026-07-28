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
final class ShipmentIntegration3OracleTest {
    @Test
    @DisplayName("触发: 物流端延迟或失联；后果: 请求线程长时间阻塞并级联耗尽")
    void createShipmentWithTimeout_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/ShipmentIntegrationService.java"));
        assertAll(
        () -> assertTrue(source.contains("long timeoutSeconds = Long.parseLong(request.get(\"timeoutSeconds\"));"), "missing seeded evidence: long timeoutSeconds = Long.parseLong(request.get(\"timeoutSeconds\"));"),
        () -> assertTrue(source.contains("return http.post(URI.create(request.get(\"carrierUrl\")), Map.of(\"orderId\", request.get(\"orderId\")),"), "missing seeded evidence: return http.post(URI.create(request.get(\"carrierUrl\")), Map.of(\"orderId\", request.get(\"orderId\")),"),
        () -> assertTrue(source.contains("Duration.ofMillis(timeoutSeconds), request.get(\"requestId\"));"), "missing seeded evidence: Duration.ofMillis(timeoutSeconds), request.get(\"requestId\"));")

        );
    }
}
