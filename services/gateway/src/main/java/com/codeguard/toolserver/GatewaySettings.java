package com.codeguard.toolserver;

import java.nio.file.Path;
import java.time.Duration;
import java.util.Map;

public record GatewaySettings(
    int port,
    int maxConcurrentReviews,
    Duration reviewTimeout,
    Duration retryDelay,
    Duration shutdownGrace,
    Path jobDbPath,
    Path workspaceDir,
    String pythonCommand,
    String webhookSecret,
    String githubToken,
    String githubAppId,
    String githubPrivateKey,
    double webhookRateLimit,
    int maxDiffLines
) {
    public static GatewaySettings fromEnv() {
        return from(System.getenv(), Path.of(System.getProperty("java.io.tmpdir", "/tmp")));
    }

    static GatewaySettings from(Map<String, String> env, Path tempDir) {
        return new GatewaySettings(
            positiveInt(env, "CODEGUARD_TOOL_SERVER_PORT", 9090),
            positiveInt(env, "CODEGUARD_MAX_CONCURRENT_REVIEWS", 2),
            Duration.ofSeconds(positiveInt(env, "CODEGUARD_REVIEW_TIMEOUT_SECONDS", 600)),
            Duration.ofSeconds(positiveInt(env, "CODEGUARD_RETRY_DELAY_SECONDS", 30)),
            Duration.ofSeconds(positiveInt(env, "CODEGUARD_SHUTDOWN_GRACE_SECONDS", 30)),
            Path.of(env.getOrDefault("CODEGUARD_JOB_DB_PATH", "./data/codeguard-jobs")),
            Path.of(env.getOrDefault("CODEGUARD_WORKSPACE_DIR", tempDir.resolve("codeguard-jobs").toString())),
            env.getOrDefault("CODEGUARD_PYTHON", "python"),
            env.getOrDefault("CODEGUARD_WEBHOOK_SECRET", ""),
            env.getOrDefault("CODEGUARD_GITHUB_TOKEN", ""),
            env.getOrDefault("CODEGUARD_GITHUB_APP_ID", ""),
            env.getOrDefault("CODEGUARD_GITHUB_PRIVATE_KEY", ""),
            nonNegativeDouble(env, "CODEGUARD_WEBHOOK_RATE_LIMIT", 0.5),
            positiveInt(env, "CODEGUARD_MAX_DIFF_LINES", 5000));
    }

    private static int positiveInt(Map<String, String> env, String name, int fallback) {
        String raw = env.get(name);
        if (raw == null || raw.isBlank()) return fallback;
        try {
            int value = Integer.parseInt(raw.trim());
            if (value < 1) throw new NumberFormatException();
            return value;
        } catch (NumberFormatException invalid) {
            throw new IllegalArgumentException(name + " 必须是正整数");
        }
    }

    private static double nonNegativeDouble(Map<String, String> env, String name, double fallback) {
        String raw = env.get(name);
        if (raw == null || raw.isBlank()) return fallback;
        try {
            double value = Double.parseDouble(raw.trim());
            if (value < 0) throw new NumberFormatException();
            return value;
        } catch (NumberFormatException invalid) {
            throw new IllegalArgumentException(name + " 必须是非负数");
        }
    }
}
