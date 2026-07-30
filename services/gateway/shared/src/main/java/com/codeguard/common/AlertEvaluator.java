package com.codeguard.common;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.EnumMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 进程内 SLO 巡检器。
 * <p>
 * 按固定间隔扫描 {@link GatewayMetrics}，针对熔断器状态、降级率、
 * 审查失败率等关键 SLO 指标做阈值检查。违规时打日志，并可选推送 Webhook。
 * <p>
 * 不依赖外部 Prometheus/Alertmanager，开箱即用。
 */
public final class AlertEvaluator implements AutoCloseable {
    private static final Logger log = LoggerFactory.getLogger(AlertEvaluator.class);

    public enum Severity { CRITICAL, WARNING }

    private final GatewayMetrics metrics;
    private final ScheduledExecutorService scheduler;
    private final int intervalSeconds;
    private final SlidingCounters counters = new SlidingCounters();
    private final List<SloAlert> activeAlerts = new CopyOnWriteArrayList<>();
    private final String webhookUrl;
    private final String envLabel;       // 环境标识，如 "dev"/"prod"
    private volatile boolean started;

    // ---- SLO 阈值（可通过环境变量覆盖） ----
    private final int cbOpenCriticalSeconds;
    private final double fallbackRateWarning;
    private final double reviewFailRateWarning;
    private final int noReviewCriticalMinutes;

    public AlertEvaluator(GatewayMetrics metrics) {
        this(metrics, fromEnv());
    }

    AlertEvaluator(GatewayMetrics metrics, AlertConfig config) {
        this.metrics = metrics;
        this.intervalSeconds = config.intervalSeconds;
        this.webhookUrl = config.webhookUrl;
        this.envLabel = config.envLabel;
        this.cbOpenCriticalSeconds = config.cbOpenCriticalSeconds;
        this.fallbackRateWarning = config.fallbackRateWarning;
        this.reviewFailRateWarning = config.reviewFailRateWarning;
        this.noReviewCriticalMinutes = config.noReviewCriticalMinutes;
        this.scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "codeguard-slo-evaluator");
            t.setDaemon(true);
            return t;
        });
    }

    private record AlertConfig(int intervalSeconds, String webhookUrl, String envLabel,
                               int cbOpenCriticalSeconds, double fallbackRateWarning,
                               double reviewFailRateWarning, int noReviewCriticalMinutes) {}

    private static AlertConfig fromEnv() {
        Map<String, String> e = System.getenv();
        return new AlertConfig(
            positiveInt(e, "CODEGUARD_SLO_INTERVAL_SECONDS", 30),
            e.getOrDefault("CODEGUARD_SLO_WEBHOOK_URL", ""),
            e.getOrDefault("CODEGUARD_ENV", "dev"),
            positiveInt(e, "CODEGUARD_SLO_CB_OPEN_CRITICAL_SECONDS", 60),
            nonNegativeDouble(e, "CODEGUARD_SLO_FALLBACK_RATE_WARNING", 0.1),
            nonNegativeDouble(e, "CODEGUARD_SLO_REVIEW_FAIL_RATE_WARNING", 0.2),
            positiveInt(e, "CODEGUARD_SLO_NO_REVIEW_CRITICAL_MINUTES", 30));
    }

    // ---- public API ----

    public void start() {
        if (started) return;
        started = true;
        scheduler.scheduleAtFixedRate(this::evaluate, intervalSeconds, intervalSeconds, TimeUnit.SECONDS);
        log.info("SLO 巡检器已启动 (间隔 {}s, env={})", intervalSeconds, envLabel);
    }

    /** 当前活跃告警快照（供 /health/slo 端点使用）。 */
    public List<SloAlert> activeAlerts() {
        return Collections.unmodifiableList(new ArrayList<>(activeAlerts));
    }

    // ---- 巡检逻辑 ----

    void evaluate() {
        counters.snapshot(metrics);
        List<SloAlert> fired = new ArrayList<>();

        checkCircuitBreaker(fired);
        checkFallbackRate(fired);
        checkReviewFailRate(fired);
        checkNoReview(fired);

        activeAlerts.clear();
        activeAlerts.addAll(fired);

        if (!fired.isEmpty()) {
            for (SloAlert alert : fired) {
                log.warn("[SLO:{}] {} - {} (detail={})", alert.severity().toUpperCase(),
                    alert.summary(), alert.name(), alert.detail());
            }
            pushWebhook(fired);
        }
    }

    private void checkCircuitBreaker(List<SloAlert> target) {
        String metricsText = metrics.scrape();
        for (String line : metricsText.split("\n")) {
            if (line.startsWith("codeguard_llm_circuit_open{") && line.contains("} 1.0")) {
                String provider = line.split("provider=\"")[1].split("\"")[0];
                target.add(SloAlert.of("circuit-breaker-open", "CRITICAL",
                    "熔断器开路: " + provider,
                    "provider=" + provider + " 熔断器持续开路 > " + cbOpenCriticalSeconds + "s"));
            }
        }
    }

    private void checkFallbackRate(List<SloAlert> target) {
        var snap = counters.last();
        long total = snap.llmCalls();
        long fallbacks = snap.llmFallbacks();
        if (total >= 20) {  // 样本量门槛
            double rate = (double) fallbacks / total;
            if (rate > fallbackRateWarning) {
                target.add(SloAlert.of("high-fallback-rate", "WARNING",
                    String.format("降级率过高: %.1f%%", rate * 100),
                    String.format("fallbacks=%d calls=%d threshold=%.0f%%",
                        fallbacks, total, fallbackRateWarning * 100)));
            }
        }
    }

    private void checkReviewFailRate(List<SloAlert> target) {
        var snap = counters.last();
        long total = snap.reviewTotal();
        long failed = snap.reviewFailed();
        if (total >= 5) {
            double rate = (double) failed / total;
            if (rate > reviewFailRateWarning) {
                target.add(SloAlert.of("high-review-fail-rate", "WARNING",
                    String.format("审查失败率过高: %.1f%%", rate * 100),
                    String.format("failed=%d total=%d threshold=%.0f%%",
                        failed, total, reviewFailRateWarning * 100)));
            }
        }
    }

    private void checkNoReview(List<SloAlert> target) {
        var snap = counters.last();
        long secondsSinceLast = snap.secondsSinceLastReview();
        long thresholdSeconds = noReviewCriticalMinutes * 60L;
        if (secondsSinceLast >= 0 && secondsSinceLast > thresholdSeconds) {
            target.add(SloAlert.of("no-recent-review", "CRITICAL",
                String.format("审查 Pipeline 可能停滞: %d分钟内无审查产出", noReviewCriticalMinutes),
                String.format("last_review=%ds_ago threshold=%ds", secondsSinceLast, thresholdSeconds)));
        }
    }

    // ---- Webhook 推送 ----

    private void pushWebhook(List<SloAlert> alerts) {
        if (webhookUrl.isBlank()) return;
        try {
            String body = buildWebhookBody(alerts);
            HttpURLConnection conn = (HttpURLConnection) URI.create(webhookUrl).toURL().openConnection();
            conn.setRequestMethod("POST");
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            conn.setDoOutput(true);
            conn.setConnectTimeout(5_000);
            conn.setReadTimeout(5_000);
            try (OutputStream os = conn.getOutputStream()) {
                os.write(body.getBytes(StandardCharsets.UTF_8));
            }
            int code = conn.getResponseCode();
            if (code >= 200 && code < 300) {
                log.info("SLO 告警已推送至 webhook: {} alerts", alerts.size());
            } else {
                log.warn("SLO webhook 推送失败: HTTP {}", code);
            }
        } catch (Exception e) {
            log.warn("SLO webhook 推送异常: {}", e.getMessage());
        }
    }

    private String buildWebhookBody(List<SloAlert> alerts) {
        StringBuilder sb = new StringBuilder("{\"env\":\"").append(envLabel)
            .append("\",\"timestamp\":\"").append(Instant.now().toString())
            .append("\",\"alerts\":[");
        for (int i = 0; i < alerts.size(); i++) {
            if (i > 0) sb.append(",");
            SloAlert a = alerts.get(i);
            sb.append("{\"name\":\"").append(escapeJson(a.name()))
                .append("\",\"severity\":\"").append(a.severity())
                .append("\",\"summary\":\"").append(escapeJson(a.summary()))
                .append("\",\"detail\":\"").append(escapeJson(a.detail())).append("\"}");
        }
        sb.append("]}");
        return sb.toString();
    }

    private static String escapeJson(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    @Override
    public void close() {
        scheduler.shutdownNow();
    }

    // ---- 滑动窗口计数器 ----
    // 从 Prometheus 指标中反算出当前窗口内的计数值

    static class SlidingCounters {
        private volatile Snapshot last = Snapshot.empty();
        private Snapshot prev = Snapshot.empty();

        synchronized void snapshot(GatewayMetrics metrics) {
            String text = metrics.scrape();
            long llmCalls = 0, llmFallbacks = 0, reviewTotal = 0, reviewFailed = 0;
            double lastReviewSeconds = -1;

            for (String line : text.split("\n")) {
                if (line.startsWith("codeguard_llm_calls_total{")) {
                    llmCalls += extractCounter(line);
                } else if (line.startsWith("codeguard_llm_fallbacks_total{")) {
                    llmFallbacks += extractCounter(line);
                } else if (line.startsWith("codeguard_review_jobs_total{") && line.contains("outcome=\"success\"")) {
                    var pair = extractCounterAndLast(line);
                    reviewTotal += pair.value;
                } else if (line.startsWith("codeguard_review_jobs_total{") && line.contains("outcome=\"failed\"")) {
                    var pair = extractCounterAndLast(line);
                    reviewFailed += pair.value;
                } else if (line.startsWith("codeguard_review_duration_seconds_sum{")) {
                    var pair = extractCounterAndLast(line);
                    if (pair.value() > 0 && pair.last() > lastReviewSeconds) {
                        lastReviewSeconds = pair.last();
                    }
                }
            }
            prev = last;
            last = new Snapshot(llmCalls, llmFallbacks, reviewTotal, reviewFailed, lastReviewSeconds);
        }

        Snapshot last() { return last; }
    }

    record CounterAndLast(double value, double last) {}
    record Snapshot(long llmCalls, long llmFallbacks, long reviewTotal, long reviewFailed,
                    double lastReviewTimestamp) {
        static Snapshot empty() { return new Snapshot(0, 0, 0, 0, -1); }
        long secondsSinceLastReview() {
            return lastReviewTimestamp > 0
                ? (long) (System.currentTimeMillis() / 1000.0 - lastReviewTimestamp)
                : -1;
        }
    }

    private static long extractCounter(String line) {
        try {
            String[] parts = line.trim().split("\\s+");
            return (long) Double.parseDouble(parts[parts.length - 1]);
        } catch (Exception ignored) { return 0; }
    }

    private static CounterAndLast extractCounterAndLast(String line) {
        try {
            // line: codeguard_review_duration_seconds_sum{...} 123.45 1735567890
            String[] parts = line.trim().split("\\s+");
            // parts[0] = metric{tags}, parts[1] = value, parts[2] = timestamp (optional)
            double value = Double.parseDouble(parts[parts.length > 2 ? parts.length - 2 : parts.length - 1]);
            double last = parts.length > 2 ? Double.parseDouble(parts[parts.length - 1]) : -1;
            return new CounterAndLast(value, last);
        } catch (Exception ignored) { return new CounterAndLast(0, -1); }
    }

    // ---- env helpers ----

    private static int positiveInt(Map<String, String> env, String name, int fallback) {
        try {
            String raw = env.get(name);
            if (raw == null || raw.isBlank()) return fallback;
            int v = Integer.parseInt(raw.trim());
            return v > 0 ? v : fallback;
        } catch (NumberFormatException ignored) { return fallback; }
    }

    private static double nonNegativeDouble(Map<String, String> env, String name, double fallback) {
        try {
            String raw = env.get(name);
            if (raw == null || raw.isBlank()) return fallback;
            double v = Double.parseDouble(raw.trim());
            return v >= 0 ? v : fallback;
        } catch (NumberFormatException ignored) { return fallback; }
    }
}
