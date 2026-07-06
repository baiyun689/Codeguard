package com.codeguard.ci.guard;

import com.google.common.util.concurrent.RateLimiter;

/**
 * 防护层: 令牌桶限流 + 大 diff 降级 + 可重试判断。
 */
public final class ReviewGuard {

    private final RateLimiter rateLimiter;
    private final int maxDiffLines;

    /**
     * @param permitsPerSecond 每秒许可数。Guava 平滑突发模式:允许短期 burst,长期平滑。
     *                         默认 0.5(= 每 2 秒 1 个许可,单用户绰绰有余)。0 表示不限流。
     * @param maxDiffLines     超过此行数的 diff 跳过审查
     */
    public ReviewGuard(double permitsPerSecond, int maxDiffLines) {
        this.rateLimiter = permitsPerSecond > 0
            ? RateLimiter.create(permitsPerSecond)
            : null;
        this.maxDiffLines = maxDiffLines;
    }

    /** 接口限流检查。未通过返回 false。 */
    public boolean tryAcquireWebhook(long timeoutMs) {
        if (rateLimiter == null) return true;
        return rateLimiter.tryAcquire(java.time.Duration.ofMillis(timeoutMs));
    }

    /** 检查 diff 是否过大需降级 */
    public boolean isDiffTooLarge(int diffLineCount) {
        return diffLineCount > maxDiffLines;
    }

    /** 生成降级结果 JSON */
    public String buildDegradedResult(int diffLineCount) {
        return String.format("""
            {
              "issues": [{
                "severity": "WARNING",
                "file": "",
                "line": 0,
                "type": "ci",
                "message": "变更过大 (%d 行)，超过 %d 行阈值，自动跳过审查以避免天价 token 消耗。建议拆分为小的 PR。",
                "suggestion": "拆分 PR",
                "confidence": 1.0
              }]
            }
            """, diffLineCount, maxDiffLines);
    }

    /** 判断异常是否可重试 */
    public boolean isRetryable(Exception e) {
        if (e instanceof java.util.concurrent.TimeoutException) return true;
        String msg = e.getMessage();
        if (msg == null) return false;
        String lower = msg.toLowerCase();
        return lower.contains("timeout") || lower.contains("connection") || lower.contains("transient");
    }
}
