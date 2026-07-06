package com.codeguard.ci.guard;

import static org.junit.jupiter.api.Assertions.*;

import java.util.concurrent.TimeoutException;

import org.junit.jupiter.api.Test;

class ReviewGuardTest {

    @Test
    void shouldAllowWithinLimit() {
        ReviewGuard guard = new ReviewGuard(30, 5000);
        // 30/hour 限流下，首次获取应该通过
        assertTrue(guard.tryAcquireWebhook(500));
    }

    @Test
    void shouldDetectLargeDiff() {
        ReviewGuard guard = new ReviewGuard(30, 5000);
        assertTrue(guard.isDiffTooLarge(5001));
        assertFalse(guard.isDiffTooLarge(100));
    }

    @Test
    void shouldBuildDegradedResult() {
        ReviewGuard guard = new ReviewGuard(30, 5000);
        String result = guard.buildDegradedResult(6000);
        assertTrue(result.contains("6000"));
        assertTrue(result.contains("WARNING"));
        assertTrue(result.contains("跳过审查"));
    }

    @Test
    void shouldIdentifyRetryableErrors() {
        ReviewGuard guard = new ReviewGuard(30, 5000);
        // TimeoutException 可重试
        assertTrue(guard.isRetryable(new TimeoutException("timed out")));
        // connection refused 可重试
        assertTrue(guard.isRetryable(new RuntimeException("connection refused")));
        // JSON parse error 不可重试
        assertFalse(guard.isRetryable(new RuntimeException("JSON parse error")));
    }
}
