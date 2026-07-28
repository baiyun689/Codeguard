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
final class JwtKeyRotation3OracleTest {
    @Test
    @DisplayName("触发: 相同用户标识存在于不同租户；后果: 低权限租户继承另一租户角色")
    void loadTenantRoles_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/JwtKeyRotationService.java"));
        assertAll(
        () -> assertTrue(source.contains("String key = \"roles:\" + request.get(\"subject\");"), "missing seeded evidence: String key = \"roles:\" + request.get(\"subject\");"),
        () -> assertTrue(source.contains("return cache.get(key).orElseGet(() -> {"), "missing seeded evidence: return cache.get(key).orElseGet(() -> {"),
        () -> assertTrue(source.contains("String roles = request.get(\"roles\");"), "missing seeded evidence: String roles = request.get(\"roles\");")

        );
    }
}
