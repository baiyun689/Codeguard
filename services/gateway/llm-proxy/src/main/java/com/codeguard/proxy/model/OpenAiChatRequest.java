package com.codeguard.proxy.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.databind.JsonNode;

import java.util.List;
import java.util.Map;

@JsonIgnoreProperties(ignoreUnknown = true)
@JsonInclude(JsonInclude.Include.NON_NULL)
public record OpenAiChatRequest(
    String model,
    List<Message> messages,
    Double temperature,
    @JsonProperty("max_tokens") Integer maxTokens,
    List<Tool> tools,
    @JsonProperty("tool_choice") Object toolChoice,
    @JsonProperty("response_format") Map<String, Object> responseFormat,
    Boolean stream
) {
    public OpenAiChatRequest withModel(String providerModel) {
        return new OpenAiChatRequest(
            providerModel, messages, temperature, maxTokens, tools, toolChoice, responseFormat, stream
        );
    }

    @JsonIgnoreProperties(ignoreUnknown = true)
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record Message(
        String role,
        Object content,          // String or List<ContentPart>
        @JsonProperty("tool_calls") List<ToolCall> toolCalls,
        @JsonProperty("tool_call_id") String toolCallId
    ) {}

    @JsonIgnoreProperties(ignoreUnknown = true)
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record ContentPart(
        String type,
        String text,
        @JsonProperty("image_url") Map<String, String> imageUrl
    ) {}

    @JsonIgnoreProperties(ignoreUnknown = true)
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record ToolCall(
        String id,
        String type,
        @JsonProperty("function") ToolCallFunction function
    ) {}

    @JsonIgnoreProperties(ignoreUnknown = true)
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record ToolCallFunction(
        String name,
        String arguments
    ) {}

    @JsonIgnoreProperties(ignoreUnknown = true)
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record Tool(
        String type,
        Function function
    ) {}

    @JsonIgnoreProperties(ignoreUnknown = true)
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record Function(
        String name,
        String description,
        JsonNode parameters
    ) {}
}
