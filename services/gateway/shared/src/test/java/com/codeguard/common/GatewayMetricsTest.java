package com.codeguard.common;

import org.junit.jupiter.api.Test;

import java.util.concurrent.atomic.AtomicBoolean;

import static org.junit.jupiter.api.Assertions.assertTrue;

final class GatewayMetricsTest {
    @Test
    void exposesReviewAndLlmHistogramMetrics() {
        GatewayMetrics metrics = new GatewayMetrics();

        metrics.reviewSucceeded(1.25);
        metrics.llmCall("deepseek", "success", 0.75);

        String scrape = metrics.scrape();
        assertTrue(scrape.contains("codeguard_review_duration_seconds_bucket"));
        assertTrue(scrape.contains("codeguard_llm_duration_seconds_bucket"));
        assertTrue(scrape.contains("provider=\"deepseek\""));
    }

    @Test
    void exposesCircuitStateAsOneOrZero() {
        GatewayMetrics metrics = new GatewayMetrics();
        AtomicBoolean open = new AtomicBoolean(false);
        metrics.gaugeCircuitOpen("qwen", open, value -> value.get() ? 1.0 : 0.0);

        assertTrue(metrics.scrape().contains("codeguard_llm_circuit_open{provider=\"qwen\"} 0.0"));
        open.set(true);
        assertTrue(metrics.scrape().contains("codeguard_llm_circuit_open{provider=\"qwen\"} 1.0"));
    }
}
