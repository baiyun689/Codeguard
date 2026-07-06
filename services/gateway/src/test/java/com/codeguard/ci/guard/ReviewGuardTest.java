package com.codeguard.ci.guard;

import static org.junit.jupiter.api.Assertions.*;

import java.util.concurrent.TimeoutException;

import org.junit.jupiter.api.Test;

class ReviewGuardTest {

    @Test
    void shouldAllowWithinLimit() {
        // 10次/秒，首次请求轻松通过
        ReviewGuard guard = new ReviewGuard(10, 5000);
        assertTrue(guard.tryAcquireWebhook(500));
    }

    @Test
    void shouldRejectWhenSustainedRateExceeded() {
        // 0.1次/秒(= 10秒1个许可)，100ms 超时不够攒许可
        ReviewGuard guard = new ReviewGuard(0.1, 5000);
        // 首次可能通过(RateLimiter 初始有 burst 容量)
        guard.tryAcquireWebhook(100);
        // 连续快速请求应该被拒
        for (int i = 0; i < 10; i++) {
            guard.tryAcquireWebhook(100);
        }
        assertFalse(guard.tryAcquireWebhook(100));
    }

    @Test
    void shouldBypassWhenRateLimitDisabled() {
        ReviewGuard guard = new ReviewGuard(0, 5000);
        assertTrue(guard.tryAcquireWebhook(500));
        assertTrue(guard.tryAcquireWebhook(500));
    }

    @Test
    void shouldDetectLargeDiff() {
        ReviewGuard guard = new ReviewGuard(10, 5000);
        assertTrue(guard.isDiffTooLarge(5001));
        assertFalse(guard.isDiffTooLarge(100));
    }

    @Test
    void shouldBuildDegradedResult() {
        ReviewGuard guard = new ReviewGuard(10, 5000);
        String result = guard.buildDegradedResult(6000);
        assertTrue(result.contains("6000"));
        assertTrue(result.contains("WARNING"));
        assertTrue(result.contains("跳过审查"));
    }

    @Test
    void shouldIdentifyRetryableErrors() {
        ReviewGuard guard = new ReviewGuard(10, 5000);
        assertTrue(guard.isRetryable(new TimeoutException("timed out")));
        assertTrue(guard.isRetryable(new RuntimeException("connection refused")));
        assertFalse(guard.isRetryable(new RuntimeException("JSON parse error")));
    }
}
