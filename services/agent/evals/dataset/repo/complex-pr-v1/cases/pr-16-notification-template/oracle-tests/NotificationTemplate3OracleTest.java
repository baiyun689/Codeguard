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
final class NotificationTemplate3OracleTest {
    @Test
    @DisplayName("触发: 发送成功但响应丢失后重试；后果: 用户收到重复邮件或短信")
    void sendNotification_seed_is_present() throws Exception {
        Path repo = Path.of(System.getProperty("tradeflow.repo"));
        String source = Files.readString(repo.resolve(
                "tradeflow-application/src/main/java/com/tradeflow/application/feature/NotificationTemplateService.java"));
        assertAll(
        () -> assertTrue(source.contains("events.publish(\"notification.send\", attemptId, request.get(\"message\"));"), "missing seeded evidence: events.publish(\"notification.send\", attemptId, request.get(\"message\"));")

        );
    }
}
