package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-17-shipment-integration/createShipmentWithTimeout.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class ShipmentIntegration3OracleTest {
    @Test
    void createShipmentWithTimeout_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-17-shipment-integration", "createShipmentWithTimeout");
        assertEquals("请求线程长时间阻塞并级联耗尽", result.observedFailure());
    }
}
