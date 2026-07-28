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
final class ShipmentIntegration1OracleTest {
    @Test
    @DisplayName("触发: 用户控制 scheme 或 host；后果: 访问内网和云元数据")
    void createShipment_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/ShipmentIntegrationService.java"));
        assertAll(
        () -> assertTrue(source.contains("URI callback = URI.create(request.get(\"callbackUrl\"));"), "missing seeded evidence: URI callback = URI.create(request.get(\"callbackUrl\"));"),
        () -> assertTrue(source.contains("return http.post(callback, Map.of(\"orderId\", request.get(\"orderId\")),"), "missing seeded evidence: return http.post(callback, Map.of(\"orderId\", request.get(\"orderId\")),"),
        () -> assertTrue(source.contains("Duration.ofSeconds(3), request.get(\"requestId\"));"), "missing seeded evidence: Duration.ofSeconds(3), request.get(\"requestId\"));")

        );
    }
}
