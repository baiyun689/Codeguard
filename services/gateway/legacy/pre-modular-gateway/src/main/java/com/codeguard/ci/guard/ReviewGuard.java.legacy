package com.codeguard.ci.guard;

import com.google.common.util.concurrent.RateLimiter;

/** Webhook 入口令牌桶限流。审查范围和重试策略由各自的上层模块负责。 */
public final class ReviewGuard {

    private final RateLimiter rateLimiter;

    /**
     * @param permitsPerSecond 每秒许可数。Guava 平滑突发模式:允许短期 burst,长期平滑。
     *                         默认 0.5(= 每 2 秒 1 个许可,单用户绰绰有余)。0 表示不限流。
     */
    public ReviewGuard(double permitsPerSecond) {
        this.rateLimiter = permitsPerSecond > 0
            ? RateLimiter.create(permitsPerSecond)
            : null;
    }

    /** 接口限流检查。未通过返回 false。 */
    public boolean tryAcquireWebhook(long timeoutMs) {
        if (rateLimiter == null) return true;
        return rateLimiter.tryAcquire(java.time.Duration.ofMillis(timeoutMs));
    }

}
