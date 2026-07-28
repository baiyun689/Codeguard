package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-18-admin-report/renderFullReport.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class AdminReport2OracleTest {
    @Test
    void renderFullReport_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-18-admin-report", "renderFullReport");
        assertEquals("堆内存耗尽并影响在线请求", result.observedFailure());
    }
}
