package com.codeguard.proxy.handler;

import com.codeguard.proxy.adapter.DeepSeekAdapter.AdapterException;
import com.codeguard.proxy.adapter.LlmAdapter;
import com.codeguard.proxy.model.OpenAiChatRequest;
import com.codeguard.proxy.model.OpenAiChatResponse;
import com.codeguard.proxy.resilience.ResilienceService;
import com.codeguard.proxy.router.ProviderRouter;
import com.codeguard.proxy.router.ProviderRouter.RouteTarget;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import io.javalin.http.Context;
import io.javalin.http.Handler;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.net.http.HttpClient;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;

/**
 * OpenAI 兼容的 /v1/chat/completions 请求处理器。
 *
 * 流程：验证 → 路由 → 遍历降级链（熔断跳过 + 韧性包装） → 协议转换 → 返回。
 */
public final class ChatCompletionsHandler implements Handler {
    private static final Logger log = LoggerFactory.getLogger(ChatCompletionsHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final ProviderRouter router;
    private final ResilienceService resilience;
    private final HttpClient httpClient;

    public ChatCompletionsHandler(ProviderRouter router, ResilienceService resilience) {
        this.router = router;
        this.resilience = resilience;
        this.httpClient = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(10))
            .build();
    }

    @Override
    public void handle(Context ctx) {
        // 1. Parse request
        OpenAiChatRequest request;
        try {
            request = MAPPER.readValue(ctx.body(), OpenAiChatRequest.class);
        } catch (IOException e) {
            ctx.status(400).json(OpenAiChatResponse.error(
                "Invalid JSON: " + e.getMessage(), "invalid_request_error", "400"));
            return;
        }

        // 2. Validate
        if (request.model() == null || request.model().isBlank()) {
            ctx.status(400).json(OpenAiChatResponse.error(
                "model is required", "invalid_request_error", "400"));
            return;
        }
        if (request.messages() == null || request.messages().isEmpty()) {
            ctx.status(400).json(OpenAiChatResponse.error(
                "messages is required", "invalid_request_error", "400"));
            return;
        }

        // 3. Route
        List<RouteTarget> chain = router.resolveChain(request.model());
        if (chain.isEmpty()) {
            ctx.status(404).json(OpenAiChatResponse.error(
                "unknown model: " + request.model(), "invalid_request_error", "404"));
            return;
        }

        // 4. Try chain with fallback
        Exception lastError = null;
        for (int targetIndex = 0; targetIndex < chain.size(); targetIndex++) {
            RouteTarget target = chain.get(targetIndex);
            LlmAdapter adapter = target.adapter();
            OpenAiChatRequest providerRequest = request.withModel(target.model());
            boolean canFallback = targetIndex + 1 < chain.size();

            try {
                OpenAiChatResponse response = resilience.executeLlmCall(() -> {
                    try {
                        var httpReq = adapter.translateRequest(providerRequest);
                        var httpResp = httpClient.send(httpReq, HttpResponse.BodyHandlers.ofString());
                        return adapter.translateResponse(httpResp.body(), httpResp.statusCode());
                    } catch (AdapterException e) {
                        // 4xx client errors (non-429) → 不重试，直接返回
                        if (e.httpStatus() != 429 && e.httpStatus() >= 400 && e.httpStatus() < 500) {
                            throw e;
                        }
                        // 5xx / 429 → 包装为 RuntimeException，让 Resilience4j 在本 provider 重试
                        throw new RuntimeException(
                            "Provider transient error [" + e.httpStatus() + "]: " + e.getMessage(), e);
                    } catch (IOException e) {
                        throw new RuntimeException("HTTP 调用失败: " + e.getMessage(), e);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        throw new RuntimeException("调用被中断", e);
                    }
                }, adapter.providerName());

                // Success
                log.debug("LLM 调用成功: model={} provider={}", request.model(), adapter.providerName());
                ctx.status(200).json(response);
                return;

            } catch (CallNotPermittedException e) {
                // 熔断器开路 → 跳过当前 provider，降级到下一个
                log.info("熔断器 [{}] 开路, 跳过", adapter.providerName());
                if (canFallback) {
                    resilience.recordFallback(adapter.providerName(), "circuit_open");
                }
            } catch (AdapterException e) {
                // Non-retryable client errors (4xx except 429)
                if (e.httpStatus() != 429 && e.httpStatus() >= 400 && e.httpStatus() < 500) {
                    log.error("客户端错误 [{}]: {} (provider={})", e.httpStatus(), e.getMessage(), adapter.providerName());
                    ctx.status(e.httpStatus()).json(OpenAiChatResponse.error(
                        e.getMessage(), e.errorType(), e.errorCode()));
                    return;
                }
                lastError = e;
                if (canFallback) {
                    resilience.recordFallback(adapter.providerName(), "provider_error");
                }
                log.warn("Provider [{}] 失败 (status={}), 尝试 fallback: {}",
                    adapter.providerName(), e.httpStatus(), e.getMessage());
            } catch (Exception e) {
                lastError = e;
                if (canFallback) {
                    resilience.recordFallback(adapter.providerName(), "provider_error");
                }
                log.warn("Provider [{}] 异常, 尝试 fallback: {}", adapter.providerName(), e.getMessage());
            }
        }

        // 5. All providers failed
        String detail = lastError != null ? lastError.getMessage() : "all providers unavailable";
        log.error("所有 provider 尝试失败: model={}, chain={}",
            request.model(), chain.stream().map(target -> target.adapter().providerName()).toList());
        ctx.status(502).json(OpenAiChatResponse.error(
            detail, "proxy_error", "502"));
    }
}
