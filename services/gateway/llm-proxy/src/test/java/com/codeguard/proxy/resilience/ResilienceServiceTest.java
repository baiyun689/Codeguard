package com.codeguard.proxy.resilience;

import com.codeguard.common.GatewayMetrics;
import com.codeguard.proxy.adapter.DeepSeekAdapter.AdapterException;
import com.codeguard.proxy.config.ProxyConfig;
import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import org.junit.jupiter.api.Test;

import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

final class ResilienceServiceTest {
    @Test
    void clientErrorsDoNotCountAsProviderFailures() {
        GatewayMetrics metrics = new GatewayMetrics();
        ResilienceService service = new ResilienceService(new ProxyConfig.ResilienceConfig(
            new ProxyConfig.ResilienceConfig.RateLimitConfig(10),
            new ProxyConfig.ResilienceConfig.CircuitBreakerConfig(50, 30, 3, 10, 1),
            new ProxyConfig.ResilienceConfig.RetryConfig(1, 1)
        ), metrics);
        CircuitBreaker breaker = service.circuitBreakerFor("deepseek");

        assertThrows(AdapterException.class, () -> service.executeLlmCall(() -> {
            throw new AdapterException(400, "invalid request", "client_error", "400");
        }, "deepseek"));

        assertEquals(0, breaker.getMetrics().getNumberOfFailedCalls());
        assertTrue(metrics.scrape().contains(
            "codeguard_llm_calls_total{outcome=\"error\",provider=\"deepseek\"} 1.0"));
        assertTrue(metrics.scrape().contains(
            "codeguard_llm_circuit_open{provider=\"deepseek\"} 0.0"));
    }

    @Test
    void openCircuitDoesNotRetryOrInvokeProvider() {
        ResilienceService service = new ResilienceService(new ProxyConfig.ResilienceConfig(
            new ProxyConfig.ResilienceConfig.RateLimitConfig(10),
            new ProxyConfig.ResilienceConfig.CircuitBreakerConfig(50, 30, 3, 10, 1),
            new ProxyConfig.ResilienceConfig.RetryConfig(3, 1)
        ));
        CircuitBreaker breaker = service.circuitBreakerFor("fallback-source");
        breaker.transitionToForcedOpenState();
        AtomicInteger calls = new AtomicInteger();

        assertThrows(CallNotPermittedException.class, () ->
            service.executeLlmCall(() -> {
                calls.incrementAndGet();
                return "unexpected";
            }, "fallback-source"));

        assertEquals(0, calls.get());
    }
}
