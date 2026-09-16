package com.codeguard.ci;

import com.codeguard.common.GatewayApplication;
import com.codeguard.ci.job.JobRepository;
import com.codeguard.ci.job.JobScheduler;
import com.codeguard.toolserver.GatewaySettings;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import java.nio.file.Path;
import java.net.URI;
import java.net.http.*;
import java.util.Map;
import static org.junit.jupiter.api.Assertions.*;

class CiSpringConfigurationTest {
    @Test
    void disabledCiStartsWithoutDatabaseAndNeverExposesInternalRoutes(@TempDir Path root) throws Exception {
        var settings = GatewaySettings.from(Map.of("CODEGUARD_TOOL_SERVER_TOKEN", "test"), root);
        try (var app = GatewayApplication.start(CiServerConfiguration.class, 0, Map.of("gatewaySettings", settings));
             var client = HttpClient.newHttpClient()) {
            assertTrue(app.context().getBeansOfType(JobRepository.class).isEmpty());
            assertEquals(200, get(client, app, "/health/ready"));
            assertEquals(404, post(client, app, "/webhooks/github"));
            assertEquals(404, post(client, app, "/api/v1/tools/session"));
            assertEquals(404, post(client, app, "/v1/chat/completions"));
            assertEquals(200, get(client, app, "/metrics"));
        }
    }

    @Test
    void enabledCiWiresAndClosesSchedulerThroughSpring(@TempDir Path root) throws Exception {
        var settings = GatewaySettings.from(Map.of("CODEGUARD_TOOL_SERVER_TOKEN", "test",
                "CODEGUARD_WEBHOOK_SECRET", "secret",
                "CODEGUARD_PYTHON", "missing-test-python"), root);
        try (var repository = new JobRepository(root.resolve("jobs").toString());
             var client = HttpClient.newHttpClient()) {
            JobScheduler scheduler;
            try (var app = GatewayApplication.start(CiServerConfiguration.class, 0,
                    Map.of("gatewaySettings", settings, "jobRepository", repository))) {
                scheduler = app.context().getBean(JobScheduler.class);
                assertTrue(scheduler.isReady());
                assertEquals(503, get(client, app, "/health/ready"), "Missing Python must remain not-ready");
                assertEquals(401, post(client, app, "/webhooks/github"));
                assertEquals(404, post(client, app, "/api/v1/tools/session"));
            }
            assertFalse(scheduler.isReady(), "Spring must close the scheduler on context shutdown");
            assertTrue(repository.ping(), "An externally supplied repository remains owned by the caller");
        }
    }

    private static int get(HttpClient client, GatewayApplication app, String path) throws Exception {
        return client.send(HttpRequest.newBuilder(URI.create("http://localhost:" + app.port() + path)).build(),
                HttpResponse.BodyHandlers.discarding()).statusCode();
    }
    private static int post(HttpClient client, GatewayApplication app, String path) throws Exception {
        return client.send(HttpRequest.newBuilder(URI.create("http://localhost:" + app.port() + path))
                .header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString("{}")).build(),
                HttpResponse.BodyHandlers.discarding()).statusCode();
    }
}
