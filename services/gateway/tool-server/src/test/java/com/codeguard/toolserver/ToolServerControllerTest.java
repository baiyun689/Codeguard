package com.codeguard.toolserver;

import com.codeguard.common.GatewayApplication;
import okhttp3.OkHttpClient;
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
        try (var app = createApp(root)) {
            var client = new OkHttpClient();
            String base = "http://localhost:" + app.port();
            Request request = new Request.Builder()
                    .url(base + "/api/v1/tools/session")
                    .post(sessionBody(repository))
                    .build();
            try (Response response = client.newCall(request).execute()) {
                assertEquals(401, response.code());
            }
        }
    }

    @Test
    void acceptsAllowedGitRepositoryWithCorrectToken(@TempDir Path root) throws Exception {
        Path repository = gitRepository(root.resolve("repo"));
        try (var app = createApp(root)) {
            var client = new OkHttpClient();
            String base = "http://localhost:" + app.port();
            Request request = new Request.Builder()
                    .url(base + "/api/v1/tools/session")
                    .header("X-Codeguard-Tool-Token", TOKEN)
                    .post(sessionBody(repository))
                    .build();
            try (Response response = client.newCall(request).execute()) {
                assertEquals(200, response.code());
            }
        }
    }

    @Test
    void rejectsRepositoryOutsideAllowedRoot(@TempDir Path root, @TempDir Path outside) throws Exception {
        Path repository = gitRepository(outside.resolve("repo"));
        try (var app = createApp(root)) {
            var client = new OkHttpClient();
            String base = "http://localhost:" + app.port();
            Request request = new Request.Builder()
                    .url(base + "/api/v1/tools/session")
                    .header("X-Codeguard-Tool-Token", TOKEN)
                    .post(sessionBody(repository))
                    .build();
            try (Response response = client.newCall(request).execute()) {
                assertEquals(400, response.code());
            }
        }
    }

    @Test
    void sourceToolAcceptsSymbolQueryAndRejectsLegacyFilePath(@TempDir Path root) throws Exception {
        Path repository = gitRepository(root.resolve("repo"));
        Path source = repository.resolve("src/main/java/demo/Service.java");
        Files.createDirectories(source.getParent());
        Files.writeString(source, "package demo; class Service { void run() {} }\n");

        try (var app = createApp(root)) {
            var client = new OkHttpClient();
            String base = "http://localhost:" + app.port();
            Request session = new Request.Builder()
                    .url(base + "/api/v1/tools/session")
                    .header("X-Codeguard-Tool-Token", TOKEN)
                    .post(sessionBody(repository))
                    .build();
            String sessionId;
            try (Response response = client.newCall(session).execute()) {
                assertEquals(200, response.code());
                String body = response.body().string();
                sessionId = body.replaceAll(".*\\\"session_id\\\":\\\"([^\\\"]+)\\\".*", "$1");
            }

            Request symbolQuery = new Request.Builder()
                    .url(base + "/api/v1/tools/read_symbol")
                    .header("X-Codeguard-Tool-Token", TOKEN)
                    .header("X-Session-Id", sessionId)
                    .post(json("{\"query\":\"{\\\"symbol_id\\\":\\\"java:demo.Service#run()\\\"}\"}"))
                    .build();
            try (Response response = client.newCall(symbolQuery).execute()) {
                assertEquals(200, response.code());
                String body = response.body().string();
                assertTrue(body.contains("\"success\":true"), body);
                assertTrue(body.contains("void run()"), body);
            }

            Request legacyPath = new Request.Builder()
                    .url(base + "/api/v1/tools/read_symbol")
                    .header("X-Codeguard-Tool-Token", TOKEN)
                    .header("X-Session-Id", sessionId)
                    .post(json("{\"file_path\":\"src/main/java/demo/Service.java\"}"))
                    .build();
            try (Response response = client.newCall(legacyPath).execute()) {
                assertEquals(200, response.code());
                String body = response.body().string();
                assertTrue(body.contains("\"success\":false"), body);
                assertTrue(body.contains("symbol_id_only"), body);
            }

            Request mixedLegacyPath = new Request.Builder()
                    .url(base + "/api/v1/tools/read_symbol")
                    .header("X-Codeguard-Tool-Token", TOKEN)
                    .header("X-Session-Id", sessionId)
                    .post(json("{\"file_path\":\"src/main/java/demo/Service.java\","
                            + "\"query\":\"{\\\"symbol_id\\\":\\\"java:demo.Service#run()\\\"}\"}"))
                    .build();
            try (Response response = client.newCall(mixedLegacyPath).execute()) {
                assertEquals(200, response.code());
                assertTrue(response.body().string().contains("symbol_id_only"));
            }
        }
    }

    @Test
    void rejectsChunkedOversizeBodiesAfterAuthentication(@TempDir Path root) throws Exception {
        byte[] oversized = new byte[com.codeguard.common.GatewayHttpConfiguration.MAX_REQUEST_BYTES + 1];
        java.util.Arrays.fill(oversized, (byte) ' ');
        try (var app = createApp(root); var client = java.net.http.HttpClient.newHttpClient()) {
            var uri = java.net.URI.create("http://localhost:" + app.port() + "/api/v1/tools/session");
            var request = java.net.http.HttpRequest.newBuilder(uri)
                    .header("X-Codeguard-Tool-Token", TOKEN)
                    .header("Content-Type", "application/json")
                    .POST(java.net.http.HttpRequest.BodyPublishers.ofInputStream(
                            () -> new java.io.ByteArrayInputStream(oversized))).build();
            assertEquals(413, client.send(request, java.net.http.HttpResponse.BodyHandlers.discarding()).statusCode());
            var unauthorized = java.net.http.HttpRequest.newBuilder(uri)
                    .header("Content-Type", "application/json")
                    .POST(java.net.http.HttpRequest.BodyPublishers.ofString("invalid json")).build();
            assertEquals(401, client.send(unauthorized, java.net.http.HttpResponse.BodyHandlers.discarding()).statusCode());
            var health = java.net.http.HttpRequest.newBuilder(java.net.URI.create(
                    "http://localhost:" + app.port() + "/health/ready")).build();
            assertEquals(200, client.send(health, java.net.http.HttpResponse.BodyHandlers.discarding()).statusCode());
        }
    }

    private static GatewayApplication createApp(Path root) {
        GatewaySettings settings = GatewaySettings.from(Map.of(
                "CODEGUARD_TOOL_SERVER_TOKEN", TOKEN,
                "CODEGUARD_TOOL_ALLOWED_ROOTS", root.toString()), root);
        return GatewayApplication.start(ToolServerConfiguration.class, 0,
                Map.of("gatewaySettings", settings));
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
