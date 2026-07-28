package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-06-file-export/openOwnedExport.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class FileExport2OracleTest {
    @Test
    void openOwnedExport_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-06-file-export", "openOwnedExport");
        assertEquals("下载他人的报表", result.observedFailure());
    }
}
