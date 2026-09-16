package com.codeguard.toolserver;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

public record GatewaySettings(
    int port,
    int maxConcurrentReviews,
    Duration reviewTimeout,
    Duration retryDelay,
    Duration shutdownGrace,
    String jobDbUrl,
    String jobDbUser,
    String jobDbPassword,
    Path workspaceDir,
    String pythonCommand,
    String webhookSecret,
    String githubToken,
    String githubAppId,
    String githubPrivateKey,
    double webhookRateLimit,
    int graphCacheMaxSnapshots,
    Duration graphCacheTtl,
    Duration graphBuildTimeout,
    String toolServerToken,
    List<Path> toolAllowedRoots
) {
    public static GatewaySettings fromEnv() {
        return from(System.getenv(), Path.of(System.getProperty("java.io.tmpdir", "/tmp")));
    }

    public static GatewaySettings from(Map<String, String> env, Path tempDir) {
        return new GatewaySettings(
            positiveInt(env, "CODEGUARD_TOOL_SERVER_PORT", 9090),
            positiveInt(env, "CODEGUARD_MAX_CONCURRENT_REVIEWS", 2),
            Duration.ofSeconds(positiveInt(env, "CODEGUARD_REVIEW_TIMEOUT_SECONDS", 600)),
            Duration.ofSeconds(positiveInt(env, "CODEGUARD_RETRY_DELAY_SECONDS", 30)),
            Duration.ofSeconds(positiveInt(env, "CODEGUARD_SHUTDOWN_GRACE_SECONDS", 30)),
            env.getOrDefault("CODEGUARD_JOB_DB_URL", "jdbc:mysql://localhost:3306/codeguard"),
            env.getOrDefault("CODEGUARD_JOB_DB_USER", "codeguard"),
            env.getOrDefault("CODEGUARD_JOB_DB_PASSWORD", "codeguard"),
            Path.of(env.getOrDefault("CODEGUARD_WORKSPACE_DIR", tempDir.resolve("codeguard-jobs").toString())),
            env.getOrDefault("CODEGUARD_PYTHON", "python"),
            env.getOrDefault("CODEGUARD_WEBHOOK_SECRET", ""),
            env.getOrDefault("CODEGUARD_GITHUB_TOKEN", ""),
            env.getOrDefault("CODEGUARD_GITHUB_APP_ID", ""),
            githubPrivateKey(env),
            nonNegativeDouble(env, "CODEGUARD_WEBHOOK_RATE_LIMIT", 0.5),
            positiveInt(env, "CODEGUARD_GRAPH_CACHE_MAX_SNAPSHOTS", 4),
            Duration.ofMinutes(positiveInt(env, "CODEGUARD_GRAPH_CACHE_TTL_MINUTES", 30)),
            Duration.ofSeconds(positiveInt(env, "CODEGUARD_GRAPH_BUILD_TIMEOUT_SECONDS", 120)),
            required(env, "CODEGUARD_TOOL_SERVER_TOKEN"),
            toolAllowedRoots(env, tempDir));
    }

    private static String githubPrivateKey(Map<String, String> env) {
        String file = env.getOrDefault("CODEGUARD_GITHUB_PRIVATE_KEY_FILE", "").trim();
        if (file.isEmpty()) return env.getOrDefault("CODEGUARD_GITHUB_PRIVATE_KEY", "");
        try {
            String value = Files.readString(Path.of(file));
            if (value.isBlank()) {
                throw new IllegalArgumentException("CODEGUARD_GITHUB_PRIVATE_KEY_FILE 内容不能为空");
            }
            return value;
        } catch (IOException error) {
            throw new IllegalArgumentException(
                "无法读取 CODEGUARD_GITHUB_PRIVATE_KEY_FILE: " + file, error);
        }
    }

    private static String required(Map<String, String> env, String name) {
        String value = env.getOrDefault(name, "").trim();
        if (value.isEmpty()) {
            throw new IllegalArgumentException(name + " 不能为空");
        }
        return value;
    }

    private static List<Path> toolAllowedRoots(Map<String, String> env, Path tempDir) {
        String configured = env.getOrDefault("CODEGUARD_TOOL_ALLOWED_ROOTS", "").trim();
        if (configured.isEmpty()) {
            return List.of(Path.of(env.getOrDefault(
                    "CODEGUARD_WORKSPACE_DIR", tempDir.resolve("codeguard-jobs").toString())));
        }
        List<Path> roots = Arrays.stream(configured.split(","))
                .map(String::trim)
                .filter(value -> !value.isEmpty())
                .map(Path::of)
                .toList();
        if (roots.isEmpty()) {
            throw new IllegalArgumentException("CODEGUARD_TOOL_ALLOWED_ROOTS 不能为空");
        }
        return roots;
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
