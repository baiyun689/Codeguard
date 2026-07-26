package com.codeguard.proxy.adapter;

import com.codeguard.proxy.model.OpenAiChatRequest;
import com.codeguard.proxy.model.OpenAiChatResponse;
import com.fasterxml.jackson.core.JsonProcessingException;
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
import java.util.Map;

public final class ClaudeAdapter implements LlmAdapter {
    private static final Logger log = LoggerFactory.getLogger(ClaudeAdapter.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final String baseUrl;
    private final String apiKey;
    private final CircuitBreaker circuitBreaker;
    private final HttpClient httpClient;

    public ClaudeAdapter(String baseUrl, String apiKey, CircuitBreaker circuitBreaker) {
        this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        this.apiKey = apiKey;
        this.circuitBreaker = circuitBreaker;
        this.httpClient = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(10))
            .build();
    }

    @Override public String providerName() { return "claude"; }
    @Override public CircuitBreaker getCircuitBreaker() { return circuitBreaker; }

    @Override
    public HttpRequest translateRequest(OpenAiChatRequest request) {
        ObjectNode anthropic = MAPPER.createObjectNode();
        anthropic.put("model", request.model());
        anthropic.put("max_tokens", request.maxTokens() != null ? request.maxTokens() : 4096);

        // Extract system message
        String systemPrompt = null;
        List<OpenAiChatRequest.Message> conversationMessages = new ArrayList<>();
        for (var msg : request.messages()) {
            if ("system".equals(msg.role()) && systemPrompt == null) {
                Object content = msg.content();
                systemPrompt = content instanceof String ? (String) content : content.toString();
            } else {
                conversationMessages.add(msg);
            }
        }
        if (systemPrompt != null) {
            anthropic.put("system", systemPrompt);
        }

        // Convert messages
        ArrayNode messages = MAPPER.createArrayNode();
        for (var msg : conversationMessages) {
            ObjectNode m = MAPPER.createObjectNode();
            m.put("role", "user".equals(msg.role()) ? "user" : "assistant");
            // Build content array (Anthropic uses content blocks, not flat strings)
            ArrayNode content = MAPPER.createArrayNode();
            if (msg.content() instanceof String text) {
                ObjectNode textBlock = MAPPER.createObjectNode();
                textBlock.put("type", "text");
                textBlock.put("text", text);
                content.add(textBlock);
            }
            // Handle tool_calls from assistant → Anthropic tool_use blocks
            if (msg.toolCalls() != null) {
                for (var tc : msg.toolCalls()) {
                    ObjectNode toolBlock = MAPPER.createObjectNode();
                    toolBlock.put("type", "tool_use");
                    toolBlock.put("id", tc.id());
                    toolBlock.put("name", tc.function().name());
                    try {
                        toolBlock.set("input", MAPPER.readTree(tc.function().arguments()));
                    } catch (JsonProcessingException e) {
                        toolBlock.put("input", tc.function().arguments());
                    }
                    content.add(toolBlock);
                }
            }
            m.set("content", content);
            messages.add(m);
        }
        anthropic.set("messages", messages);

        // Convert tools
        if (request.tools() != null && !request.tools().isEmpty()) {
            ArrayNode tools = MAPPER.createArrayNode();
            for (var tool : request.tools()) {
                if (tool.function() != null) {
                    ObjectNode t = MAPPER.createObjectNode();
                    t.put("name", tool.function().name());
                    t.put("description", tool.function().description() != null ? tool.function().description() : "");
                    t.set("input_schema", tool.function().parameters() != null ? tool.function().parameters() : MAPPER.createObjectNode());
                    tools.add(t);
                }
            }
            anthropic.set("tools", tools);
        }

        // tool_choice mapping
        if (request.toolChoice() != null) {
            if (request.toolChoice() instanceof String s && "required".equals(s)) {
                ObjectNode tc = MAPPER.createObjectNode();
                tc.put("type", "any");
                anthropic.set("tool_choice", tc);
            } else if (request.toolChoice() instanceof Map<?,?> m && m.containsKey("function")) {
                ObjectNode tc = MAPPER.createObjectNode();
                tc.put("type", "tool");
                tc.put("name", (String) ((Map<?,?>) m.get("function")).get("name"));
                anthropic.set("tool_choice", tc);
            }
        }

        try {
            byte[] body = MAPPER.writeValueAsBytes(anthropic);
            return HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/v1/messages"))
                .timeout(Duration.ofSeconds(60))
                .header("Content-Type", "application/json")
                .header("x-api-key", apiKey)
                .header("anthropic-version", "2023-06-01")
                .POST(HttpRequest.BodyPublishers.ofByteArray(body))
                .build();
        } catch (Exception e) {
            throw new RuntimeException("序列化 Anthropic 请求失败", e);
        }
    }

    @Override
    public OpenAiChatResponse translateResponse(String rawBody, int statusCode) {
        if (statusCode != 200) {
            try {
                JsonNode root = MAPPER.readTree(rawBody);
                JsonNode err = root.has("error") ? root.get("error") : root;
                String message = err.has("message") ? err.get("message").asText() : root.toString();
                String type = err.has("type") ? err.get("type").asText() : "api_error";
                throw new DeepSeekAdapter.AdapterException(statusCode, message, type, type);
            } catch (DeepSeekAdapter.AdapterException ae) { throw ae; }
            catch (Exception e) {
                throw new DeepSeekAdapter.AdapterException(statusCode, rawBody, "api_error", String.valueOf(statusCode));
            }
        }
        try {
            JsonNode root = MAPPER.readTree(rawBody);

            // Build OpenAI-format response
            String id = root.has("id") ? root.get("id").asText() : "claude-" + System.currentTimeMillis();
            String model = root.has("model") ? root.get("model").asText() : "claude";
            long created = System.currentTimeMillis() / 1000;

            // Convert Anthropic content blocks → OpenAI message
            var message = new OpenAiChatResponse.ResponseMessage("assistant", null, new ArrayList<>());
            JsonNode content = root.get("content");
            if (content != null && content.isArray()) {
                StringBuilder textContent = new StringBuilder();
                for (JsonNode block : content) {
                    String blockType = block.has("type") ? block.get("type").asText() : "";
                    if ("text".equals(blockType)) {
                        textContent.append(block.has("text") ? block.get("text").asText() : "");
                    } else if ("tool_use".equals(blockType)) {
                        var tcf = new OpenAiChatRequest.ToolCallFunction(
                            block.has("name") ? block.get("name").asText() : "",
                            block.has("input") ? block.get("input").toString() : "{}");
                        var tc = new OpenAiChatRequest.ToolCall(
                            block.has("id") ? block.get("id").asText() : "call_" + System.nanoTime(),
                            "function", tcf);
                        message.toolCalls().add(tc);
                    }
                }
                if (!textContent.isEmpty()) {
                    message = new OpenAiChatResponse.ResponseMessage("assistant",
                        textContent.toString(), message.toolCalls());
                }
            }

            // Map stop_reason → finish_reason
            String stopReason = root.has("stop_reason") ? root.get("stop_reason").asText() : "end_turn";
            String finishReason = switch (stopReason) {
                case "end_turn" -> "stop";
                case "tool_use" -> "tool_calls";
                case "max_tokens" -> "length";
                case "stop_sequence" -> "stop";
                default -> "stop";
            };

            // Usage
            JsonNode usage = root.get("usage");
            var u = new OpenAiChatResponse.Usage(
                usage != null && usage.has("input_tokens") ? usage.get("input_tokens").asInt() : 0,
                usage != null && usage.has("output_tokens") ? usage.get("output_tokens").asInt() : 0,
                usage != null && usage.has("input_tokens") && usage.has("output_tokens")
                    ? usage.get("input_tokens").asInt() + usage.get("output_tokens").asInt() : 0);

            return OpenAiChatResponse.success(id, model, created,
                List.of(new OpenAiChatResponse.Choice(0, message, finishReason)), u);
        } catch (DeepSeekAdapter.AdapterException ae) { throw ae; }
        catch (Exception e) {
            log.error("解析 Claude 响应失败", e);
            throw new DeepSeekAdapter.AdapterException(502,
                "Failed to parse Claude response: " + e.getMessage(), "parse_error", "502");
        }
    }
}
