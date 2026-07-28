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
final class FileExport3OracleTest {
    @Test
    @DisplayName("触发: 检查后目标被符号链接或并发替换；后果: 打开与已授权文件不同的对象")
    void openVerifiedExport_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/FileExportService.java"));
        assertAll(
        () -> assertTrue(source.contains("Path target = files.exportRoot(context.tenantId()).resolve(request.get(\"file\")).normalize();"), "missing seeded evidence: Path target = files.exportRoot(context.tenantId()).resolve(request.get(\"file\")).normalize();"),
        () -> assertTrue(source.contains("if (!Files.isRegularFile(target)) throw new IllegalArgumentException(\"missing\");"), "missing seeded evidence: if (!Files.isRegularFile(target)) throw new IllegalArgumentException(\"missing\");"),
        () -> assertTrue(source.contains("audit.record(context.tenantId(), \"EXPORT\", target.toString());"), "missing seeded evidence: audit.record(context.tenantId(), \"EXPORT\", target.toString());")

        );
    }
}
