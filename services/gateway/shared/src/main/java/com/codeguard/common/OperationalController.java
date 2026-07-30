package com.codeguard.common;

import io.javalin.Javalin;

import java.util.function.BooleanSupplier;

public final class OperationalController {
    private final BooleanSupplier ready;
    private final GatewayMetrics metrics;
    private final AlertEvaluator alertEvaluator;

    public OperationalController(BooleanSupplier ready) {
        this(ready, null, null);
    }

    public OperationalController(BooleanSupplier ready, GatewayMetrics metrics) {
        this(ready, metrics, null);
    }

    public OperationalController(BooleanSupplier ready, GatewayMetrics metrics, AlertEvaluator alertEvaluator) {
        this.ready = ready;
        this.metrics = metrics;
        this.alertEvaluator = alertEvaluator;
    }

    public void register(Javalin app) {
        app.get("/health", ctx -> ctx.result("OK"));
        app.get("/health/live", ctx -> ctx.result("OK"));
        app.get("/health/ready", ctx -> {
            boolean isReady = ready.getAsBoolean();
            ctx.status(isReady ? 200 : 503).result(isReady ? "READY" : "NOT_READY");
        });
        if (metrics != null) {
            app.get("/metrics", ctx -> ctx.contentType("text/plain; version=0.0.4; charset=utf-8")
                .result(metrics.scrape()));
        }
        if (alertEvaluator != null) {
            app.get("/health/slo", ctx -> {
                var alerts = alertEvaluator.activeAlerts();
                ctx.contentType("application/json");
                if (alerts.isEmpty()) {
                    ctx.result("{\"status\":\"ok\",\"alerts\":[]}");
                } else {
                    StringBuilder sb = new StringBuilder("{\"status\":\"degraded\",\"alerts\":[");
                    for (int i = 0; i < alerts.size(); i++) {
                        if (i > 0) sb.append(",");
                        var a = alerts.get(i);
                        sb.append("{\"name\":\"").append(escape(a.name()))
                            .append("\",\"severity\":\"").append(a.severity())
                            .append("\",\"summary\":\"").append(escape(a.summary())).append("\"}");
                    }
                    sb.append("]}");
                    ctx.result(sb.toString());
                }
            });
        }
    }

    private static String escape(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }
}
