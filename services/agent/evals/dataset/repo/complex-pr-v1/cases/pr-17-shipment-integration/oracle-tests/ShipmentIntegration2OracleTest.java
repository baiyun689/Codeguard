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
final class ShipmentIntegration2OracleTest {
    @Test
    @DisplayName("触发: 物流端已创建但响应超时；后果: 同一订单产生多个运单和费用")
    void retryShipment_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/ShipmentIntegrationService.java"));
        assertAll(
        () -> assertTrue(source.contains("return http.post(URI.create(request.get(\"carrierUrl\")), Map.of(\"orderId\", request.get(\"orderId\")),"), "missing seeded evidence: return http.post(URI.create(request.get(\"carrierUrl\")), Map.of(\"orderId\", request.get(\"orderId\")),")
        );
    }
}
