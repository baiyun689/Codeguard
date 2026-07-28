package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-04-inventory-reservation/releaseExpiredReservation.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class InventoryReservation3OracleTest {
    @Test
    void releaseExpiredReservation_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-04-inventory-reservation", "releaseExpiredReservation");
        assertEquals("库存被多次增加", result.observedFailure());
    }
}
