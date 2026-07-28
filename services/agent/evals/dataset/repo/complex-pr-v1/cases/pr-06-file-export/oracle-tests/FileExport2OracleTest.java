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
final class FileExport2OracleTest {
    @Test
    @DisplayName("触发: 调用方把 owner 参数替换成目标用户；后果: 下载他人的报表")
    void openOwnedExport_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/FileExportService.java"));
        assertAll(
        () -> assertTrue(source.contains("Path root = files.exportRoot(request.get(\"ownerTenant\"));"), "missing seeded evidence: Path root = files.exportRoot(request.get(\"ownerTenant\"));"),
        () -> assertTrue(source.contains("return files.open(root.resolve(request.get(\"file\")));"), "missing seeded evidence: return files.open(root.resolve(request.get(\"file\")));")
        );
    }
}
