package com.codeguard.proxy;

import com.codeguard.common.GatewayApplication;
import com.codeguard.proxy.config.ProxyConfig;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import com.fasterxml.jackson.databind.JsonNode;
import static org.junit.jupiter.api.Assertions.*;

class ProxyHttpTest {
    private static final ObjectMapper JSON = new ObjectMapper();

    private static ProxyConfig config(String url) {
        return new ProxyConfig(Map.of("deepseek", new ProxyConfig.ProviderConfig(url, "test")),
                Map.of("review", new ProxyConfig.RouteConfig(List.of(
                        new ProxyConfig.RouteTargetConfig("deepseek", "upstream-model")))),
                new ProxyConfig.ResilienceConfig(
                        new ProxyConfig.ResilienceConfig.RateLimitConfig(100),
                        new ProxyConfig.ResilienceConfig.CircuitBreakerConfig(50, 1, 1, 10, 5),
                        new ProxyConfig.ResilienceConfig.RetryConfig(1, 1)));
    }

    @Test
    void bootRoutesPreserveOpenAiJsonAndDoNotExposeTools() throws Exception {
        AtomicInteger calls = new AtomicInteger();
        AtomicReference<JsonNode> forwarded = new AtomicReference<>();
        var upstream = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        upstream.createContext("/", exchange -> {
            calls.incrementAndGet();
            var request = JSON.readTree(exchange.getRequestBody());
            forwarded.set(request);
            assertEquals("upstream-model", request.path("model").asText());
            byte[] body = """
                    {"id":"reply","object":"chat.completion","created":1,"model":"upstream-model",
                    "choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],
                    "usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}
                    """.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        upstream.start();
        try (var app = GatewayApplication.start(ProxyConfiguration.class, 0,
                Map.of("proxyConfig", config("http://127.0.0.1:" + upstream.getAddress().getPort())));
             var client = HttpClient.newHttpClient()) {
            String root = "http://localhost:" + app.port();
            var response = client.send(HttpRequest.newBuilder(URI.create(root + "/v1/chat/completions"))
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString("""
                            {"model":"review","messages":[{"role":"user","content":"test"}],
                             "thinking":{"type":"disabled"}}
                            """)).build(), HttpResponse.BodyHandlers.ofString());
            assertEquals(200, response.statusCode(), response.body());
            assertEquals("ok", JSON.readTree(response.body()).path("choices").get(0).path("message").path("content").asText());
            assertEquals(1, calls.get());
            assertEquals("disabled", forwarded.get().path("thinking").path("type").asText(),
                    "Routing through the proxy must preserve the caller's thinking mode");
            var invalid = client.send(HttpRequest.newBuilder(URI.create(root + "/v1/chat/completions"))
                    .header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString("{"))
                    .build(), HttpResponse.BodyHandlers.ofString());
            assertEquals(400, invalid.statusCode());
            assertEquals("invalid_request_error", JSON.readTree(invalid.body()).path("error").path("type").asText());
            assertEquals(1, calls.get(), "Invalid requests must never contact a provider");
            assertEquals(404, client.send(HttpRequest.newBuilder(URI.create(root + "/api/v1/tools/session"))
                    .POST(HttpRequest.BodyPublishers.ofString("{}")).build(),
                    HttpResponse.BodyHandlers.ofString()).statusCode());
            assertEquals(200, client.send(HttpRequest.newBuilder(URI.create(root + "/health/ready")).build(),
                    HttpResponse.BodyHandlers.ofString()).statusCode());
        } finally {
            upstream.stop(0);
        }
    }
}
