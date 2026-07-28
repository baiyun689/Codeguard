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
final class FileExport1OracleTest {
    @Test
    @DisplayName("触发: 文件名包含编码后的父目录片段；后果: 读取其他租户或服务器文件")
    void openExport_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/FileExportService.java"));
        assertAll(
        () -> assertTrue(source.contains("Path root = files.exportRoot(context.tenantId());"), "missing seeded evidence: Path root = files.exportRoot(context.tenantId());"),
        () -> assertTrue(source.contains("Path target = root.resolve(URLDecoder.decode(request.get(\"file\"), StandardCharsets.UTF_8));"), "missing seeded evidence: Path target = root.resolve(URLDecoder.decode(request.get(\"file\"), StandardCharsets.UTF_8));"),
        () -> assertTrue(source.contains("return files.open(target);"), "missing seeded evidence: return files.open(target);")
        );
    }
}
