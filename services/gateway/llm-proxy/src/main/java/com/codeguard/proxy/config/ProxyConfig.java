package com.codeguard.proxy.config;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.yaml.snakeyaml.Yaml;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * LLM 代理配置，从 YAML 文件加载。
 *
 * 支持 ${ENV_VAR} 环境变量替换（仅 provider key 字段，格式为 ${VAR_NAME}）。
 */
public final class ProxyConfig {
    private static final Logger log = LoggerFactory.getLogger(ProxyConfig.class);

    private final Map<String, ProviderConfig> providers;
    private final Map<String, RouteConfig> routes;
    private final ResilienceConfig resilience;

    public ProxyConfig(Map<String, ProviderConfig> providers,
                        Map<String, RouteConfig> routes,
                        ResilienceConfig resilience) {
        this.providers = Map.copyOf(providers);
        this.routes = Map.copyOf(routes);
        this.resilience = resilience;
    }

    public Map<String, ProviderConfig> providers() { return providers; }
    public Map<String, RouteConfig> routes() { return routes; }
    public ResilienceConfig resilience() { return resilience; }

    // ---- config records ----

    public record ProviderConfig(String url, String key) {}

    public record RouteConfig(List<String> chain) {}

    public record ResilienceConfig(
        RateLimitConfig rateLimit,
        CircuitBreakerConfig circuitBreaker,
        RetryConfig retry
    ) {
        public record RateLimitConfig(int permitsPerSecond) {}
        public record CircuitBreakerConfig(
            int failureRateThreshold,
            int waitDurationInOpenStateSeconds,
            int permittedCallsInHalfOpen,
            int slidingWindowSize,
            int minimumNumberOfCalls
        ) {}
        public record RetryConfig(int maxAttempts, int waitDurationMs) {}
    }

    // ---- loader ----

    public static ProxyConfig load() {
        String configPath = System.getenv().getOrDefault("CODEGUARD_LLM_CONFIG", "llm-proxy-config.yml");
        return load(Path.of(configPath));
    }

    @SuppressWarnings("unchecked")
    static ProxyConfig load(Path path) {
        if (!Files.exists(path)) {
            log.warn("LLM 代理配置文件不存在 ({}), 使用空配置", path);
            return empty();
        }
        try {
            String raw = Files.readString(path);
            Yaml yaml = new Yaml();
            Map<String, Object> data = yaml.load(raw);
            if (data == null) return empty();
            return parse(data);
        } catch (IOException e) {
            log.error("读取 LLM 代理配置文件失败: {}", path, e);
            return empty();
        }
    }

    @SuppressWarnings("unchecked")
    private static ProxyConfig parse(Map<String, Object> data) {
        // providers
        Map<String, ProviderConfig> providers = new LinkedHashMap<>();
        Map<String, Object> provData = (Map<String, Object>) data.getOrDefault("providers", Map.of());
        for (var entry : provData.entrySet()) {
            Map<String, Object> cfg = (Map<String, Object>) entry.getValue();
            String url = (String) cfg.getOrDefault("url", "");
            String key = resolveEnv((String) cfg.getOrDefault("key", ""));
            providers.put(entry.getKey(), new ProviderConfig(url, key));
        }

        // routes
        Map<String, RouteConfig> routes = new LinkedHashMap<>();
        Map<String, Object> routeData = (Map<String, Object>) data.getOrDefault("routes", Map.of());
        for (var entry : routeData.entrySet()) {
            Map<String, Object> cfg = (Map<String, Object>) entry.getValue();
            List<String> chain = (List<String>) cfg.getOrDefault("chain", List.of());
            routes.put(entry.getKey(), new RouteConfig(chain));
        }

        // resilience
        Map<String, Object> resData = (Map<String, Object>) data.getOrDefault("resilience", Map.of());
        Map<String, Object> rl = (Map<String, Object>) resData.getOrDefault("rate-limit", Map.of());
        Map<String, Object> cb = (Map<String, Object>) resData.getOrDefault("circuit-breaker", Map.of());
        Map<String, Object> rt = (Map<String, Object>) resData.getOrDefault("retry", Map.of());

        var rateLimit = new ResilienceConfig.RateLimitConfig(
            toInt(rl.get("permits-per-second"), 10));
        var circuitBreaker = new ResilienceConfig.CircuitBreakerConfig(
            toInt(cb.get("failure-rate-threshold"), 50),
            toInt(cb.get("wait-duration-in-open-state-seconds"), 30),
            toInt(cb.get("permitted-calls-in-half-open"), 3),
            toInt(cb.get("sliding-window-size"), 10),
            toInt(cb.get("minimum-number-of-calls"), 5));
        var retry = new ResilienceConfig.RetryConfig(
            toInt(rt.get("max-attempts"), 3),
            toInt(rt.get("wait-duration-ms"), 500));

        return new ProxyConfig(providers, routes, new ResilienceConfig(rateLimit, circuitBreaker, retry));
    }

    private static String resolveEnv(String value) {
        if (value == null) return "";
        if (value.startsWith("${") && value.endsWith("}")) {
            String varName = value.substring(2, value.length() - 1);
            return System.getenv().getOrDefault(varName, "");
        }
        return value;
    }

    private static int toInt(Object value, int fallback) {
        if (value instanceof Number n) return n.intValue();
        if (value instanceof String s) {
            try { return Integer.parseInt(s); } catch (NumberFormatException ignored) {}
        }
        return fallback;
    }

    private static ProxyConfig empty() {
        return new ProxyConfig(Map.of(), Map.of(),
            new ResilienceConfig(
                new ResilienceConfig.RateLimitConfig(10),
                new ResilienceConfig.CircuitBreakerConfig(50, 30, 3, 10, 5),
                new ResilienceConfig.RetryConfig(3, 500)));
    }
}
