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
final class JwtKeyRotation2OracleTest {
    @Test
    @DisplayName("触发: 任意本应过期的 token；后果: 凭据有效期被错误延长或全部被拒绝")
    void isTokenActive_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/JwtKeyRotationService.java"));
        assertAll(
        () -> assertTrue(source.contains("long expiresAtSeconds = Long.parseLong(request.get(\"exp\"));"), "missing seeded evidence: long expiresAtSeconds = Long.parseLong(request.get(\"exp\"));")
        );
    }
}
