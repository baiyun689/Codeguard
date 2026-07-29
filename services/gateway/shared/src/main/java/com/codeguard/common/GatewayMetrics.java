package com.codeguard.common;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.Timer;
import io.micrometer.prometheusmetrics.PrometheusConfig;
import io.micrometer.prometheusmetrics.PrometheusMeterRegistry;

import java.time.Duration;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.ToDoubleFunction;

public final class GatewayMetrics {
    private final PrometheusMeterRegistry registry = new PrometheusMeterRegistry(PrometheusConfig.DEFAULT);
    private final AtomicInteger activeReviews = new AtomicInteger();

    public GatewayMetrics() {
        Gauge.builder("codeguard.review.active", activeReviews, AtomicInteger::get).register(registry);
    }

    public void reviewStarted() { activeReviews.incrementAndGet(); }
    public void reviewFinished() { activeReviews.updateAndGet(value -> Math.max(0, value - 1)); }

    public void reviewSucceeded(double seconds) {
        review("success", seconds);
    }

    public void reviewFailed(String reason, double seconds) {
        review("failed", seconds);
    }

    private void review(String outcome, double seconds) {
        Counter.builder("codeguard.review.jobs").tag("outcome", outcome).register(registry).increment();
        durationTimer("codeguard.review.duration").record(seconds(seconds));
    }

    public void retry(String reason) {
        Counter.builder("codeguard.review.retries").tag("reason", safe(reason)).register(registry).increment();
    }

    public void processTimeout(String phase) {
        Counter.builder("codeguard.process.timeouts").tag("phase", safe(phase)).register(registry).increment();
    }

    public void feedbackFailure() {
        Counter.builder("codeguard.feedback.failures").register(registry).increment();
    }

    public void toolCall(String tool, String status) {
        Counter.builder("codeguard.tool.calls").tag("tool", safe(tool)).tag("status", safe(status))
            .register(registry).increment();
    }

    public void llmCall(String provider, String outcome, double seconds) {
        String safeProvider = safe(provider);
        String safeOutcome = safe(outcome);
        Counter.builder("codeguard.llm.calls")
            .tag("provider", safeProvider)
            .tag("outcome", safeOutcome)
            .register(registry)
            .increment();
        durationTimer("codeguard.llm.duration", "provider", safeProvider, "outcome", safeOutcome)
            .record(seconds(seconds));
    }

    public void llmFallback(String provider, String reason) {
        Counter.builder("codeguard.llm.fallbacks")
            .tag("provider", safe(provider))
            .tag("reason", safe(reason))
            .register(registry)
            .increment();
    }

    public void llmRetry(String provider) {
        Counter.builder("codeguard.llm.retries")
            .tag("provider", safe(provider))
            .register(registry)
            .increment();
    }

    public <T> void gaugeCircuitOpen(String provider, T state, ToDoubleFunction<T> value) {
        Gauge.builder("codeguard.llm.circuit.open", state, value)
            .tag("provider", safe(provider))
            .register(registry);
    }

    public <T> void gaugeToolSessions(T state, ToDoubleFunction<T> value) {
        Gauge.builder("codeguard.tool.sessions.active", state, value).register(registry);
    }

    public String scrape() { return registry.scrape(); }

    private static String safe(String value) {
        return value == null || value.isBlank() ? "unknown" : value;
    }

    private Timer durationTimer(String name, String... tags) {
        return Timer.builder(name)
            .tags(tags)
            .publishPercentileHistogram()
            .serviceLevelObjectives(
                Duration.ofMillis(250),
                Duration.ofSeconds(1),
                Duration.ofSeconds(5),
                Duration.ofSeconds(15),
                Duration.ofSeconds(30),
                Duration.ofMinutes(1),
                Duration.ofMinutes(5),
                Duration.ofMinutes(10))
            .register(registry);
    }

    private static Duration seconds(double seconds) {
        return Duration.ofNanos(Math.max(0L, (long) (seconds * 1_000_000_000L)));
    }
}
