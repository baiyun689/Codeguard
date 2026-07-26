package com.codeguard.proxy.resilience;

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
    private final Retry retry;
    private final Map<String, CircuitBreaker> circuitBreakers = new ConcurrentHashMap<>();
    private final ProxyConfig.ResilienceConfig config;

    public ResilienceService(ProxyConfig.ResilienceConfig config) {
        this.config = config;

        RateLimiterConfig rlConf = RateLimiterConfig.custom()
            .limitForPeriod(config.rateLimit().permitsPerSecond())
            .limitRefreshPeriod(Duration.ofSeconds(1))
            .timeoutDuration(Duration.ofSeconds(30))
            .build();
        this.rateLimiter = RateLimiter.of("llm-global", rlConf);

        RetryConfig retryConf = RetryConfig.custom()
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
        this.retry = Retry.of("llm-global", retryConf);
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
        decorated = Retry.decorateSupplier(retry, decorated);
        return decorated.get();
    }
}
