package com.codeguard.proxy.adapter;

import com.codeguard.proxy.model.OpenAiChatRequest;
import com.codeguard.proxy.model.OpenAiChatResponse;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;

import java.net.http.HttpRequest;

/** 单个 LLM 提供商的协议适配器。负责 OpenAI 格式到提供商原生协议的转换（双向）。 */
public interface LlmAdapter {

    /** 提供商标识，用于日志和路由（如 "deepseek", "claude", "qwen"）。 */
    String providerName();

    /** 将 OpenAI 兼容请求转换为指向提供商 API 的 HTTP 请求。 */
    HttpRequest translateRequest(OpenAiChatRequest request);

    /** 将提供商原始 HTTP 响应转换为 OpenAI 兼容格式。 */
    OpenAiChatResponse translateResponse(String rawBody, int statusCode);

    /** 该提供商的独立熔断器实例。 */
    CircuitBreaker getCircuitBreaker();
}
