package com.codeguard.proxy.resilience;

import com.codeguard.proxy.adapter.DeepSeekAdapter.AdapterException;
import com.codeguard.proxy.config.ProxyConfig;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

final class ResilienceServiceTest {
    @Test
    void clientErrorsDoNotCountAsProviderFailures() {
        ResilienceService service = new ResilienceService(new ProxyConfig.ResilienceConfig(
            new ProxyConfig.ResilienceConfig.RateLimitConfig(10),
            new ProxyConfig.ResilienceConfig.CircuitBreakerConfig(50, 30, 3, 10, 1),
            new ProxyConfig.ResilienceConfig.RetryConfig(1, 1)
        ));
        CircuitBreaker breaker = service.circuitBreakerFor("deepseek");

        assertThrows(AdapterException.class, () -> service.executeLlmCall(() -> {
            throw new AdapterException(400, "invalid request", "client_error", "400");
        }, "deepseek"));

        assertEquals(0, breaker.getMetrics().getNumberOfFailedCalls());
    }
}
