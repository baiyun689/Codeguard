package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

/**
 * Evaluation-only oracle for pr-13-bulk-import/resolveArchiveEntry.
 * Install this source in the isolated oracle harness; it is intentionally
 * excluded from the reviewed repository snapshot.
 */
final class BulkImport1OracleTest {
    @Test
    void resolveArchiveEntry_preserves_the_business_invariant() {
        OracleResult result = TradeFlowOracleHarness.run(
                "pr-13-bulk-import", "resolveArchiveEntry");
        assertEquals("覆盖应用可写的任意文件", result.observedFailure());
    }
}
