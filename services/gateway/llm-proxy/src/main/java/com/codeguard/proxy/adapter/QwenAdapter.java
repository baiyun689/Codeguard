package com.codeguard.proxy.adapter;

import com.codeguard.proxy.model.OpenAiChatRequest;
import com.codeguard.proxy.model.OpenAiChatResponse;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.time.Duration;

public final class QwenAdapter implements LlmAdapter {
    private static final Logger log = LoggerFactory.getLogger(QwenAdapter.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final String baseUrl;
    private final String apiKey;
    private final CircuitBreaker circuitBreaker;
    private final HttpClient httpClient;

    public QwenAdapter(String baseUrl, String apiKey, CircuitBreaker circuitBreaker) {
        this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        this.apiKey = apiKey;
        this.circuitBreaker = circuitBreaker;
        this.httpClient = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(10))
            .build();
    }

    @Override public String providerName() { return "qwen"; }
    @Override public CircuitBreaker getCircuitBreaker() { return circuitBreaker; }

    @Override
    public HttpRequest translateRequest(OpenAiChatRequest request) {
        try {
            byte[] body = MAPPER.writeValueAsBytes(request);
            // DashScope compatible-mode endpoint is OpenAI-compatible
            String url = baseUrl.contains("/chat/completions") ? baseUrl : baseUrl + "/chat/completions";
            return HttpRequest.newBuilder()
                .uri(URI.create(url))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + apiKey)
                .POST(HttpRequest.BodyPublishers.ofByteArray(body))
                .build();
        } catch (Exception e) {
            throw new RuntimeException("序列化请求失败", e);
        }
    }

    @Override
    public OpenAiChatResponse translateResponse(String rawBody, int statusCode) {
        if (statusCode != 200) {
            try {
                JsonNode root = MAPPER.readTree(rawBody);
                String message = root.has("message") ? root.get("message").asText() : "unknown error";
                String code = root.has("code") ? root.get("code").asText() : String.valueOf(statusCode);
                throw new DeepSeekAdapter.AdapterException(statusCode, message, "provider_error", code);
            } catch (DeepSeekAdapter.AdapterException ae) { throw ae; }
            catch (Exception e) {
                String snippet = rawBody != null ? rawBody.substring(0, Math.min(300, rawBody.length())) : "empty";
                throw new DeepSeekAdapter.AdapterException(statusCode, snippet, "provider_error", String.valueOf(statusCode));
            }
        }
        try {
            return MAPPER.readValue(rawBody, OpenAiChatResponse.class);
        } catch (Exception e) {
            log.error("解析 Qwen 响应失败", e);
            throw new DeepSeekAdapter.AdapterException(502, "Failed to parse Qwen response: " + e.getMessage(),
                "parse_error", "502");
        }
    }
}
