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
final class LoginRateLimit2OracleTest {
    @Test
    @DisplayName("触发: 并发登录失败请求；后果: 多个请求同时低于阈值并绕过锁定")
    void recordLoginFailure_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/LoginRateLimitService.java"));
        assertAll(
        () -> assertTrue(source.contains("String key = \"login:\" + request.get(\"username\");"), "missing seeded evidence: String key = \"login:\" + request.get(\"username\");"),
        () -> assertTrue(source.contains("long current = cache.get(key).map(Long::parseLong).orElse(0L);"), "missing seeded evidence: long current = cache.get(key).map(Long::parseLong).orElse(0L);"),
        () -> assertTrue(source.contains("if (current >= 5) throw new SecurityException(\"locked\");"), "missing seeded evidence: if (current >= 5) throw new SecurityException(\"locked\");")
        );
    }
}
