package com.codeguard.toolserver;

import io.javalin.Javalin;
import io.javalin.testtools.JavalinTest;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class OperationalControllerTest {
    @Test
    void exposesLiveReadyAndPrometheusMetrics() {
        GatewayMetrics metrics = new GatewayMetrics();
        metrics.reviewSucceeded(0.25);
        metrics.reviewFailed("PROCESS_TIMEOUT", 0.5);
        metrics.retry("PROCESS_TIMEOUT");
        metrics.processTimeout("review");
        metrics.toolCall("get_file_content", "success");
        Javalin app = Javalin.create();
        new OperationalController(() -> false, metrics).register(app);

        JavalinTest.test(app, (server, client) -> {
            assertEquals(200, client.get("/health").code());
            assertEquals(200, client.get("/health/live").code());
            assertEquals(503, client.get("/health/ready").code());
            try (var response = client.get("/metrics")) {
                assertEquals(200, response.code());
                String body = response.body().string();
                assertTrue(body.contains("codeguard_review_jobs_total"));
                assertTrue(body.contains("codeguard_review_retries_total"));
                assertTrue(body.contains("codeguard_process_timeouts_total"));
                assertTrue(body.contains("codeguard_tool_calls_total"));
                assertFalse(body.contains("repo="));
            }
        });
    }
}
