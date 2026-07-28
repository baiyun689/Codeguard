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
final class InventoryReservation1OracleTest {
    @Test
    @DisplayName("触发: 两个请求同时预留最后一批库存；后果: 可用库存变为负数并产生无法履约订单")
    void reserveStock_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/InventoryReservationService.java"));
        assertAll(
        () -> assertTrue(source.contains("InventoryItem item = inventory.findBySku(request.get(\"sku\")).orElseThrow();"), "missing seeded evidence: InventoryItem item = inventory.findBySku(request.get(\"sku\")).orElseThrow();"),
        () -> assertTrue(source.contains("int quantity = Integer.parseInt(request.get(\"quantity\"));"), "missing seeded evidence: int quantity = Integer.parseInt(request.get(\"quantity\"));"),
        () -> assertTrue(source.contains("if (item.available() < quantity) throw new IllegalStateException(\"insufficient\");"), "missing seeded evidence: if (item.available() < quantity) throw new IllegalStateException(\"insufficient\");")

        );
    }
}
