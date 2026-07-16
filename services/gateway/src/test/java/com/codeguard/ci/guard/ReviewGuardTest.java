package com.codeguard.ci.guard;

import static org.junit.jupiter.api.Assertions.*;

import org.junit.jupiter.api.Test;

class ReviewGuardTest {

    @Test
    void shouldAllowWithinLimit() {
        // 10次/秒，首次请求轻松通过
        ReviewGuard guard = new ReviewGuard(10);
        assertTrue(guard.tryAcquireWebhook(500));
    }

    @Test
    void shouldRejectWhenSustainedRateExceeded() {
        // 0.1次/秒(= 10秒1个许可)，100ms 超时不够攒许可
        ReviewGuard guard = new ReviewGuard(0.1);
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
        ReviewGuard guard = new ReviewGuard(0);
        assertTrue(guard.tryAcquireWebhook(500));
        assertTrue(guard.tryAcquireWebhook(500));
    }
}
