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
final class JwtKeyRotation1OracleTest {
    @Test
    @DisplayName("触发: 攻击者提交自定义 kid；后果: 服务访问内网或云元数据地址")
    void loadSigningKey_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/JwtKeyRotationService.java"));
        assertAll(
        () -> assertTrue(source.contains("URI keyUri = URI.create(request.get(\"issuer\") + \"/keys/\" + request.get(\"kid\"));"), "missing seeded evidence: URI keyUri = URI.create(request.get(\"issuer\") + \"/keys/\" + request.get(\"kid\"));"),
        () -> assertTrue(source.contains("return http.get(keyUri, Duration.ofSeconds(2));"), "missing seeded evidence: return http.get(keyUri, Duration.ofSeconds(2));")

        );
    }
}
