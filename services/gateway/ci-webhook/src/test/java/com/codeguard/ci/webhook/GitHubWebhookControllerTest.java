package com.codeguard.ci.webhook;

import com.codeguard.ci.job.JobRepository;
import com.codeguard.ci.job.JobScheduler;
import com.codeguard.common.GatewayApplication;
import okhttp3.OkHttpClient;
import okhttp3.MediaType;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.HexFormat;

import static org.junit.jupiter.api.Assertions.*;

class GitHubWebhookControllerTest {

    private static final String SECRET = "test_secret";
    private JobRepository repo;
    private JobScheduler scheduler;
    private GitHubWebhookController controller;

    @BeforeEach
    void setUp() throws Exception {
        String dbPath = System.getProperty("java.io.tmpdir") + "/codeguard-ctrl-" + System.nanoTime();
        repo = new JobRepository(dbPath);
        scheduler = new JobScheduler(repo, 2,
            job -> new com.codeguard.ci.executor.ReviewExecutionOutcome.Succeeded(
                "{\"issues\":[],\"summary\":\"ok\"}", "", 0, Duration.ZERO),
            Duration.ofMillis(10), Duration.ofSeconds(1), null);
        scheduler.start();
        controller = new GitHubWebhookController(SECRET, repo, scheduler);
    }

    @AfterEach
    void tearDown() throws Exception {
        scheduler.close();
        repo.close();
    }

    @Test
    void shouldReturn401ForInvalidSignature() throws Exception {
        try (var app = createApp()) {
            var client = new OkHttpClient();
            String url = "http://localhost:" + app.port() + "/webhooks/github";
            try (Response r = client.newCall(new Request.Builder()
                    .url(url)
                    .post(RequestBody.create("{}", MediaType.parse("application/json")))
                    .header("X-Hub-Signature-256", "sha256=bad")
                    .header("X-GitHub-Event", "pull_request")
                    .build()).execute()) {
                assertEquals(401, r.code());
            }
        }
    }

    @Test
    void shouldReturn200ForNonPREvent() throws Exception {
        try (var app = createApp()) {
            var client = new OkHttpClient();
            String url = "http://localhost:" + app.port() + "/webhooks/github";
            String body = "{}";
            String sig = sign(body);
            try (Response r = client.newCall(new Request.Builder()
                    .url(url)
                    .post(RequestBody.create(body, MediaType.parse("application/json")))
                    .header("X-Hub-Signature-256", sig)
                    .header("X-GitHub-Event", "push")
                    .build()).execute()) {
                assertEquals(200, r.code());
                assertTrue(r.body().string().contains("ignored"));
            }
        }
    }

    @Test
    void shouldReturn200ForSkippedAction() throws Exception {
        try (var app = createApp()) {
            var client = new OkHttpClient();
            String url = "http://localhost:" + app.port() + "/webhooks/github";
            String body = "{\"action\":\"closed\"}";
            String sig = sign(body);
            try (Response r = client.newCall(new Request.Builder()
                    .url(url)
                    .post(RequestBody.create(body, MediaType.parse("application/json")))
                    .header("X-Hub-Signature-256", sig)
                    .header("X-GitHub-Event", "pull_request")
                    .build()).execute()) {
                assertEquals(200, r.code());
                assertTrue(r.body().string().contains("skipped"));
            }
        }
    }

    @Test
    void shouldAcceptValidPRWebhook() throws Exception {
        try (var app = createApp()) {
            var client = new OkHttpClient();
            String url = "http://localhost:" + app.port() + "/webhooks/github";
            String body = """
                {
                  "action": "opened",
                  "repository": { "full_name": "owner/repo", "clone_url": "https://gh/owner/repo.git" },
                  "pull_request": {
                    "number": 42,
                    "head": { "sha": "abc123def456", "ref": "feature/x" },
                    "base": { "ref": "main" }
                  },
                  "installation": { "id": 12345 }
                }
                """;
            String sig = sign(body);
            try (Response r = client.newCall(new Request.Builder()
                    .url(url)
                    .post(RequestBody.create(body, MediaType.parse("application/json")))
                    .header("X-Hub-Signature-256", sig)
                    .header("X-GitHub-Event", "pull_request")
                    .build()).execute()) {
                assertEquals(202, r.code());
            }
        }
    }

    private GatewayApplication createApp() {
        return GatewayApplication.start(TestConfiguration.class, 0,
                java.util.Map.of("webhookController", controller));
    }

    @org.springframework.context.annotation.Configuration(proxyBeanMethods = false)
    @org.springframework.boot.autoconfigure.EnableAutoConfiguration(
        exclude = org.springframework.boot.autoconfigure.jdbc.DataSourceAutoConfiguration.class)
    @org.springframework.context.annotation.Import(com.codeguard.common.GatewayHttpConfiguration.class)
    static class TestConfiguration {}

    private String sign(String body) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(SECRET.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            return "sha256=" + HexFormat.of().formatHex(mac.doFinal(body.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception e) { throw new RuntimeException(e); }
    }
}
