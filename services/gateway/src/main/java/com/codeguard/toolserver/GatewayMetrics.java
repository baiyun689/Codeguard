package com.codeguard.toolserver;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.Timer;
import io.micrometer.prometheusmetrics.PrometheusConfig;
import io.micrometer.prometheusmetrics.PrometheusMeterRegistry;

import java.time.Duration;
import java.util.concurrent.TimeUnit;
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
        Timer.builder("codeguard.review.duration").register(registry)
            .record(Duration.ofNanos((long) (seconds * 1_000_000_000L)));
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

    public <T> void gaugeToolSessions(T state, ToDoubleFunction<T> value) {
        Gauge.builder("codeguard.tool.sessions.active", state, value).register(registry);
    }

    public String scrape() { return registry.scrape(); }

    private static String safe(String value) {
        return value == null || value.isBlank() ? "unknown" : value;
    }
}
