package com.codeguard.proxy.resilience;

import com.codeguard.common.GatewayMetrics;
import com.codeguard.proxy.config.ProxyConfig;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.github.resilience4j.circuitbreaker.CircuitBreakerConfig;
import io.github.resilience4j.ratelimiter.RateLimiter;
import io.github.resilience4j.ratelimiter.RateLimiterConfig;
import io.github.resilience4j.retry.Retry;
import io.github.resilience4j.retry.RetryConfig;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import com.codeguard.proxy.adapter.DeepSeekAdapter.AdapterException;

import java.io.IOException;
import java.time.Duration;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeoutException;
import java.util.function.Supplier;

/**
 * LLM 调用韧性服务：限流 → 熔断（按 provider） → 重试。
 * 每个 provider 有独立的 CircuitBreaker，RateLimiter 和 Retry 全局共享。
 */
public final class ResilienceService {
    private static final Logger log = LoggerFactory.getLogger(ResilienceService.class);

    private final RateLimiter rateLimiter;
    private final RetryConfig retryConfig;
    private final Map<String, Retry> retries = new ConcurrentHashMap<>();
    private final Map<String, CircuitBreaker> circuitBreakers = new ConcurrentHashMap<>();
    private final ProxyConfig.ResilienceConfig config;
    private final GatewayMetrics metrics;

    public ResilienceService(ProxyConfig.ResilienceConfig config) {
        this(config, new GatewayMetrics());
    }

    public ResilienceService(ProxyConfig.ResilienceConfig config, GatewayMetrics metrics) {
        this.config = config;
        this.metrics = metrics;

        RateLimiterConfig rlConf = RateLimiterConfig.custom()
            .limitForPeriod(config.rateLimit().permitsPerSecond())
            .limitRefreshPeriod(Duration.ofSeconds(1))
            .timeoutDuration(Duration.ofSeconds(30))
            .build();
        this.rateLimiter = RateLimiter.of("llm-global", rlConf);

        this.retryConfig = RetryConfig.custom()
            .maxAttempts(config.retry().maxAttempts())
            .intervalBiFunction((attempt, error) -> {
                // 指数退避 + 随机抖动: 500ms → 1s → 2s, ±50% jitter
                long base = config.retry().waitDurationMs() * (1L << (attempt - 1));
                long jitter = (long) (Math.random() * base * 0.5);
                return base + jitter;
            })
            .retryOnException(e ->
                (e instanceof IOException
                    || e instanceof TimeoutException
                    || e instanceof RuntimeException)
                && !(e instanceof AdapterException))  // AdapterException 由 handler fallback 循环处理
            .build();
    }

    /**
     * 获取或创建某个 provider 的独立熔断器。
     */
    public CircuitBreaker circuitBreakerFor(String providerName) {
        return circuitBreakers.computeIfAbsent(providerName, name -> {
            var cbConf = config.circuitBreaker();
            CircuitBreakerConfig cb = CircuitBreakerConfig.custom()
                .failureRateThreshold(cbConf.failureRateThreshold())
                .waitDurationInOpenState(Duration.ofSeconds(cbConf.waitDurationInOpenStateSeconds()))
                .permittedNumberOfCallsInHalfOpenState(cbConf.permittedCallsInHalfOpen())
                .slidingWindowSize(cbConf.slidingWindowSize())
                .minimumNumberOfCalls(cbConf.minimumNumberOfCalls())
                .ignoreException(error ->
                    error instanceof AdapterException adapterError
                        && adapterError.httpStatus() >= 400
                        && adapterError.httpStatus() < 500
                        && adapterError.httpStatus() != 429)
                .build();
            CircuitBreaker breaker = CircuitBreaker.of("llm-" + name, cb);
            metrics.gaugeCircuitOpen(name, breaker, value ->
                value.getState() == CircuitBreaker.State.OPEN
                    || value.getState() == CircuitBreaker.State.FORCED_OPEN ? 1.0 : 0.0);
            breaker.getEventPublisher().onStateTransition(event ->
                log.warn("LLM circuit breaker [{}]: {} → {}",
                    name,
                    event.getStateTransition().getFromState(),
                    event.getStateTransition().getToState()));
            return breaker;
        });
    }

    /**
     * 包装 LLM 调用：限流 → 熔断（指定 provider） → 重试。
     */
    public <T> T executeLlmCall(Supplier<T> supplier, String providerName) {
        CircuitBreaker cb = circuitBreakerFor(providerName);
        Supplier<T> decorated = RateLimiter.decorateSupplier(rateLimiter, supplier);
        decorated = CircuitBreaker.decorateSupplier(cb, decorated);
        decorated = Retry.decorateSupplier(retryFor(providerName), decorated);
        long startedAt = System.nanoTime();
        try {
            T result = decorated.get();
            metrics.llmCall(providerName, "success", elapsedSeconds(startedAt));
            return result;
        } catch (RuntimeException | Error error) {
            metrics.llmCall(providerName, "error", elapsedSeconds(startedAt));
            throw error;
        }
    }

    public void recordFallback(String providerName, String reason) {
        metrics.llmFallback(providerName, reason);
    }

    private Retry retryFor(String providerName) {
        return retries.computeIfAbsent(providerName, name -> {
            Retry providerRetry = Retry.of("llm-" + name, retryConfig);
            providerRetry.getEventPublisher().onRetry(event -> metrics.llmRetry(name));
            return providerRetry;
        });
    }

    private static double elapsedSeconds(long startedAt) {
        return (System.nanoTime() - startedAt) / 1_000_000_000.0;
    }
}
