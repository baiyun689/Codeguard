package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-04-inventory-reservation/reserveStock.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class InventoryReservation1OracleTest {
    @Test
    void reserveStock_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-04-inventory-reservation", "reserveStock");
        assertEquals("可用库存变为负数并产生无法履约订单", result.observedFailure());
    }
}
