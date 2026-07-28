package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-04-inventory-reservation/reserveWithLocalLock.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class InventoryReservation2OracleTest {
    @Test
    void reserveWithLocalLock_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-04-inventory-reservation", "reserveWithLocalLock");
        assertEquals("临界区实际并未串行化", result.observedFailure());
    }
}
