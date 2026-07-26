package com.codeguard.proxy.router;

import com.codeguard.proxy.adapter.LlmAdapter;
import com.codeguard.proxy.config.ProxyConfig;
import com.codeguard.proxy.model.OpenAiChatRequest;
import com.codeguard.proxy.model.OpenAiChatResponse;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import org.junit.jupiter.api.Test;

import java.net.http.HttpRequest;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;

final class ProviderRouterTest {
    @Test
    void resolvesProviderSpecificModelNamesForFallback() {
        ProxyConfig config = new ProxyConfig(
            Map.of(),
            Map.of("deepseek-chat", new ProxyConfig.RouteConfig(List.of(
                new ProxyConfig.RouteTargetConfig("deepseek", "deepseek-chat"),
                new ProxyConfig.RouteTargetConfig("qwen", "qwen-max")
            ))),
            resilienceConfig()
        );

        ProviderRouter router = new ProviderRouter(config, Map.of(
            "deepseek", new StubAdapter("deepseek"),
            "qwen", new StubAdapter("qwen")
        ));

        List<ProviderRouter.RouteTarget> chain = router.resolveChain("deepseek-chat");
        assertEquals("deepseek-chat", chain.get(0).model());
        assertEquals("qwen-max", chain.get(1).model());
    }

    private static ProxyConfig.ResilienceConfig resilienceConfig() {
        return new ProxyConfig.ResilienceConfig(
            new ProxyConfig.ResilienceConfig.RateLimitConfig(10),
            new ProxyConfig.ResilienceConfig.CircuitBreakerConfig(50, 30, 3, 10, 5),
            new ProxyConfig.ResilienceConfig.RetryConfig(3, 1)
        );
    }

    private record StubAdapter(String providerName) implements LlmAdapter {
        @Override public HttpRequest translateRequest(OpenAiChatRequest request) {
            throw new UnsupportedOperationException();
        }

        @Override public OpenAiChatResponse translateResponse(String rawBody, int statusCode) {
            throw new UnsupportedOperationException();
        }

        @Override public CircuitBreaker getCircuitBreaker() {
            return CircuitBreaker.ofDefaults(providerName);
        }
    }
}
