package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-17-shipment-integration/createShipment.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class ShipmentIntegration1OracleTest {
    @Test
    void createShipment_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-17-shipment-integration", "createShipment");
        assertEquals("访问内网和云元数据", result.observedFailure());
    }
}
