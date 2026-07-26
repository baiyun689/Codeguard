package com.codeguard.proxy.adapter;

import com.codeguard.proxy.model.OpenAiChatRequest;
import com.codeguard.proxy.model.OpenAiChatResponse;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

public final class DeepSeekAdapter implements LlmAdapter {
    private static final Logger log = LoggerFactory.getLogger(DeepSeekAdapter.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final String baseUrl;
    private final String apiKey;
    private final CircuitBreaker circuitBreaker;
    private final HttpClient httpClient;

    public DeepSeekAdapter(String baseUrl, String apiKey, CircuitBreaker circuitBreaker) {
        this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        this.apiKey = apiKey;
        this.circuitBreaker = circuitBreaker;
        this.httpClient = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(10))
            .build();
    }

    @Override public String providerName() { return "deepseek"; }
    @Override public CircuitBreaker getCircuitBreaker() { return circuitBreaker; }

    @Override
    public HttpRequest translateRequest(OpenAiChatRequest request) {
        try {
            byte[] body = MAPPER.writeValueAsBytes(request);
            return HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/chat/completions"))
                .timeout(Duration.ofSeconds(60))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + apiKey)
                .POST(HttpRequest.BodyPublishers.ofByteArray(body))
                .build();
        } catch (Exception e) {
            throw new RuntimeException("序列化 OpenAI 请求失败", e);
        }
    }

    @Override
    public OpenAiChatResponse translateResponse(String rawBody, int statusCode) {
        if (statusCode != 200) {
            try {
                // Try to parse as OpenAiChatResponse.ErrorResponse
                var errorResp = MAPPER.readValue(rawBody, OpenAiChatResponse.ErrorResponse.class);
                throw new AdapterException(statusCode, errorResp.error().message(),
                    errorResp.error().type(), errorResp.error().code());
            } catch (AdapterException ae) { throw ae; }
            catch (Exception e) {
                throw new AdapterException(statusCode, rawBody != null ? rawBody.substring(0, Math.min(500, rawBody.length())) : "unknown",
                    "provider_error", String.valueOf(statusCode));
            }
        }
        try {
            JsonNode root = MAPPER.readTree(rawBody);
            // Normalize tool_calls[].function.arguments to always be a JSON string
            JsonNode choices = root.get("choices");
            if (choices != null && choices.isArray()) {
                for (JsonNode choice : choices) {
                    JsonNode message = choice.get("message");
                    if (message == null) continue;
                    JsonNode toolCalls = message.get("tool_calls");
                    if (toolCalls == null || !toolCalls.isArray()) continue;
                    for (JsonNode tc : toolCalls) {
                        JsonNode func = tc.get("function");
                        if (func == null) continue;
                        JsonNode args = func.get("arguments");
                        if (args == null) continue;
                        // DeepSeek sometimes returns arguments as a JSON object instead of string
                        if (!args.isTextual()) {
                            ((ObjectNode) func).put("arguments", args.toString());
                        }
                    }
                }
            }
            return MAPPER.treeToValue(root, OpenAiChatResponse.class);
        } catch (AdapterException ae) { throw ae; }
        catch (Exception e) {
            log.error("解析 DeepSeek 响应失败", e);
            throw new AdapterException(502, "Failed to parse provider response: " + e.getMessage(),
                "parse_error", "502");
        }
    }

    /** Thrown when an adapter encounters a provider error. Carries HTTP status and error details. */
    public static final class AdapterException extends RuntimeException {
        private final int httpStatus;
        private final String errorType;
        private final String errorCode;
        public AdapterException(int httpStatus, String message, String errorType, String errorCode) {
            super(message);
            this.httpStatus = httpStatus;
            this.errorType = errorType;
            this.errorCode = errorCode;
        }
        public int httpStatus() { return httpStatus; }
        public String errorType() { return errorType; }
        public String errorCode() { return errorCode; }
    }
}
