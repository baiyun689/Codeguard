package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-13-bulk-import/renderImportError.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class BulkImport3OracleTest {
    @Test
    void renderImportError_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-13-bulk-import", "renderImportError");
        assertEquals("运营人员打开文件时执行公式", result.observedFailure());
    }
}
