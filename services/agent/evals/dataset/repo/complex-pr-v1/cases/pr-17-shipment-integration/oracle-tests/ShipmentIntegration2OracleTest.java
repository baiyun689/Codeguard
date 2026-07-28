package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-17-shipment-integration/retryShipment.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class ShipmentIntegration2OracleTest {
    @Test
    void retryShipment_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-17-shipment-integration", "retryShipment");
        assertEquals("同一订单产生多个运单和费用", result.observedFailure());
    }
}
