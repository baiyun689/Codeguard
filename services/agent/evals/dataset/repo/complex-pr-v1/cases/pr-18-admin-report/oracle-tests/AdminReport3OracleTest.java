package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-18-admin-report/renderReportRow.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class AdminReport3OracleTest {
    @Test
    void renderReportRow_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-18-admin-report", "renderReportRow");
        assertEquals("管理员打开报表时执行公式", result.observedFailure());
    }
}
