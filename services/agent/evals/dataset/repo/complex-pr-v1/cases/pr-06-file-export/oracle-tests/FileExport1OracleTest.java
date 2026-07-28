package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-06-file-export/openExport.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class FileExport1OracleTest {
    @Test
    void openExport_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-06-file-export", "openExport");
        assertEquals("读取其他租户或服务器文件", result.observedFailure());
    }
}
