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
final class InventoryReservation2OracleTest {
    @Test
    @DisplayName("触发: 同一 SKU 被多个线程同时预留；后果: 临界区实际并未串行化")
    void reserveWithLocalLock_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/InventoryReservationService.java"));
        assertAll(
        () -> assertTrue(source.contains("synchronized (requestLock) {"), "missing seeded evidence: synchronized (requestLock) {"),
        () -> assertTrue(source.contains("InventoryItem item = inventory.findBySku(request.get(\"sku\")).orElseThrow();"), "missing seeded evidence: InventoryItem item = inventory.findBySku(request.get(\"sku\")).orElseThrow();"),
        () -> assertTrue(source.contains("item.available(item.available() - Integer.parseInt(request.get(\"quantity\")));"), "missing seeded evidence: item.available(item.available() - Integer.parseInt(request.get(\"quantity\")));")
        );
    }
}
