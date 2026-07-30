package com.codeguard.proxy.adapter;

import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

final class DeepSeekAdapterTest {
    @Test
    void customOpenAiCompatibleProviderKeepsConfiguredName() {
        CircuitBreaker breaker = CircuitBreaker.ofDefaults("custom-provider");

        DeepSeekAdapter adapter = new DeepSeekAdapter(
            "custom-provider",
            "https://example.invalid/v1",
            "test-key",
            breaker
        );

        assertEquals("custom-provider", adapter.providerName());
        assertEquals(breaker, adapter.getCircuitBreaker());
    }
}
