package com.tradeflow.oracle;

import static org.junit.jupiter.api.Assertions.assertAll;
import static org.junit.jupiter.api.Assertions.assertTrue;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * Evaluator-only static contract oracle. It is excluded from the
 * reviewed project snapshot.
 */
final class BulkImport2OracleTest {
    @Test
    @DisplayName("触发: 批次中间一行保存失败；后果: 调用方收到成功但数据仅部分导入")
    void importRows_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/BulkImportService.java"));
        assertAll(
        () -> assertTrue(source.contains("List<String> rows = Arrays.asList(request.get(\"rows\").split(\"\\\\|\"));"), "missing seeded evidence: List<String> rows = Arrays.asList(request.get(\"rows\").split(\"\\\\|\"));"),
        () -> assertTrue(source.contains("events.publish(\"catalog.import\", context.tenantId(), row);"), "missing seeded evidence: events.publish(\"catalog.import\", context.tenantId(), row);"),
        () -> assertTrue(source.contains("audit.record(context.tenantId(), \"IMPORT_ROW_FAILED\", row);"), "missing seeded evidence: audit.record(context.tenantId(), \"IMPORT_ROW_FAILED\", row);")

        );
    }
}
