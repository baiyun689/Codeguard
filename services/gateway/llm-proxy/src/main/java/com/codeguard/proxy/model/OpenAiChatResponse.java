package com.codeguard.proxy.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;

@JsonIgnoreProperties(ignoreUnknown = true)
@JsonInclude(JsonInclude.Include.NON_NULL)
public record OpenAiChatResponse(
    String id,
    String object,
    long created,
    String model,
    List<Choice> choices,
    Usage usage
) {
    @JsonIgnoreProperties(ignoreUnknown = true)
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record Choice(
        int index,
        ResponseMessage message,
        @JsonProperty("finish_reason") String finishReason
    ) {}

    @JsonIgnoreProperties(ignoreUnknown = true)
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record ResponseMessage(
        String role,
        String content,
        @JsonProperty("tool_calls") List<OpenAiChatRequest.ToolCall> toolCalls
    ) {}

    @JsonIgnoreProperties(ignoreUnknown = true)
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record Usage(
        @JsonProperty("prompt_tokens") int promptTokens,
        @JsonProperty("completion_tokens") int completionTokens,
        @JsonProperty("total_tokens") int totalTokens
    ) {}

    /** OpenAI-compatible error response. */
    @JsonIgnoreProperties(ignoreUnknown = true)
    public record ErrorResponse(ErrorDetail error) {}

    @JsonIgnoreProperties(ignoreUnknown = true)
    public record ErrorDetail(String message, String type, String code) {}

    /** Factory: create a 200 OK response. */
    public static OpenAiChatResponse success(String id, String model, long created,
                                              List<Choice> choices, Usage usage) {
        return new OpenAiChatResponse(id, "chat.completion", created, model, choices, usage);
    }

    /** Factory: create an error-mapped response (returned as body, HTTP status set separately). */
    public static ErrorResponse error(String message, String type, String code) {
        return new ErrorResponse(new ErrorDetail(message, type, code));
    }
}
