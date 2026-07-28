package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-06-file-export/openVerifiedExport.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class FileExport3OracleTest {
    @Test
    void openVerifiedExport_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-06-file-export", "openVerifiedExport");
        assertEquals("打开与已授权文件不同的对象", result.observedFailure());
    }
}
