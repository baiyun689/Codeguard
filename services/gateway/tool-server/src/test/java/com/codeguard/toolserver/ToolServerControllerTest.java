package com.codeguard.toolserver;

import io.javalin.Javalin;
import io.javalin.testtools.JavalinTest;
import okhttp3.MediaType;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ToolServerControllerTest {
    private static final String TOKEN = "test-tool-token";

    @Test
    void requiresTokenBeforeCreatingOrCallingTools(@TempDir Path root) throws Exception {
        Path repository = gitRepository(root.resolve("repo"));
        JavalinTest.test(createApp(root), (app, client) -> {
            String base = "http://localhost:" + app.port();
            Request request = new Request.Builder()
                    .url(base + "/api/v1/tools/session")
                    .post(sessionBody(repository))
                    .build();
            try (Response response = client.request(request)) {
                assertEquals(401, response.code());
            }
        });
    }

    @Test
    void acceptsAllowedGitRepositoryWithCorrectToken(@TempDir Path root) throws Exception {
        Path repository = gitRepository(root.resolve("repo"));
        JavalinTest.test(createApp(root), (app, client) -> {
            String base = "http://localhost:" + app.port();
            Request request = new Request.Builder()
                    .url(base + "/api/v1/tools/session")
                    .header("X-Codeguard-Tool-Token", TOKEN)
                    .post(sessionBody(repository))
                    .build();
            try (Response response = client.request(request)) {
                assertEquals(200, response.code());
            }
        });
    }

    @Test
    void rejectsRepositoryOutsideAllowedRoot(@TempDir Path root, @TempDir Path outside) throws Exception {
        Path repository = gitRepository(outside.resolve("repo"));
        JavalinTest.test(createApp(root), (app, client) -> {
            String base = "http://localhost:" + app.port();
            Request request = new Request.Builder()
                    .url(base + "/api/v1/tools/session")
                    .header("X-Codeguard-Tool-Token", TOKEN)
                    .post(sessionBody(repository))
                    .build();
            try (Response response = client.request(request)) {
                assertEquals(400, response.code());
            }
        });
    }

    @Test
    void sourceToolAcceptsSymbolQueryAndRejectsLegacyFilePath(@TempDir Path root) throws Exception {
        Path repository = gitRepository(root.resolve("repo"));
        Path source = repository.resolve("src/main/java/demo/Service.java");
        Files.createDirectories(source.getParent());
        Files.writeString(source, "package demo; class Service { void run() {} }\n");

        JavalinTest.test(createApp(root), (app, client) -> {
            String base = "http://localhost:" + app.port();
            Request session = new Request.Builder()
                    .url(base + "/api/v1/tools/session")
                    .header("X-Codeguard-Tool-Token", TOKEN)
                    .post(sessionBody(repository))
                    .build();
            String sessionId;
            try (Response response = client.request(session)) {
                assertEquals(200, response.code());
                String body = response.body().string();
                sessionId = body.replaceAll(".*\\\"session_id\\\":\\\"([^\\\"]+)\\\".*", "$1");
            }

            Request symbolQuery = new Request.Builder()
                    .url(base + "/api/v1/tools/get_file_content")
                    .header("X-Codeguard-Tool-Token", TOKEN)
                    .header("X-Session-Id", sessionId)
                    .post(json("{\"query\":\"{\\\"symbol_id\\\":\\\"java:demo.Service#run()\\\"}\"}"))
                    .build();
            try (Response response = client.request(symbolQuery)) {
                assertEquals(200, response.code());
                String body = response.body().string();
                assertTrue(body.contains("\"success\":true"), body);
                assertTrue(body.contains("void run()"), body);
            }

            Request legacyPath = new Request.Builder()
                    .url(base + "/api/v1/tools/get_file_content")
                    .header("X-Codeguard-Tool-Token", TOKEN)
                    .header("X-Session-Id", sessionId)
                    .post(json("{\"file_path\":\"src/main/java/demo/Service.java\"}"))
                    .build();
            try (Response response = client.request(legacyPath)) {
                assertEquals(200, response.code());
                String body = response.body().string();
                assertTrue(body.contains("\"success\":false"), body);
                assertTrue(body.contains("symbol_id_only"), body);
            }

            Request mixedLegacyPath = new Request.Builder()
                    .url(base + "/api/v1/tools/get_file_content")
                    .header("X-Codeguard-Tool-Token", TOKEN)
                    .header("X-Session-Id", sessionId)
                    .post(json("{\"file_path\":\"src/main/java/demo/Service.java\","
                            + "\"query\":\"{\\\"symbol_id\\\":\\\"java:demo.Service#run()\\\"}\"}"))
                    .build();
            try (Response response = client.request(mixedLegacyPath)) {
                assertEquals(200, response.code());
                assertTrue(response.body().string().contains("symbol_id_only"));
            }
        });
    }

    private static Javalin createApp(Path root) {
        GatewaySettings settings = GatewaySettings.from(Map.of(
                "CODEGUARD_TOOL_SERVER_TOKEN", TOKEN,
                "CODEGUARD_TOOL_ALLOWED_ROOTS", root.toString()), root);
        Javalin app = Javalin.create(config -> config.showJavalinBanner = false);
        new ToolServerController(new com.codeguard.common.GatewayMetrics(), settings).registerRoutes(app);
        return app;
    }

    private static Path gitRepository(Path repository) throws Exception {
        Files.createDirectories(repository.resolve(".git"));
        return repository;
    }

    private static RequestBody json(String body) {
        return RequestBody.create(body, MediaType.parse("application/json"));
    }

    private static RequestBody sessionBody(Path repository) {
        String escaped = repository.toString().replace("\\", "\\\\");
        return json("{\"repo_path\":\"" + escaped + "\"}");
    }
}
