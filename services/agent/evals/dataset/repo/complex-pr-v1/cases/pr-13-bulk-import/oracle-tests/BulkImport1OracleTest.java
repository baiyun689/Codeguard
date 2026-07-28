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
final class BulkImport1OracleTest {
    @Test
    @DisplayName("触发: 上传包含 ../ 的压缩包；后果: 覆盖应用可写的任意文件")
    void resolveArchiveEntry_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/BulkImportService.java"));
        assertAll(
        () -> assertTrue(source.contains("Path root = files.exportRoot(context.tenantId()).resolve(\"imports\");"), "missing seeded evidence: Path root = files.exportRoot(context.tenantId()).resolve(\"imports\");"),
        () -> assertTrue(source.contains("Path target = root.resolve(request.get(\"entryName\"));"), "missing seeded evidence: Path target = root.resolve(request.get(\"entryName\"));")

        );
    }
}
