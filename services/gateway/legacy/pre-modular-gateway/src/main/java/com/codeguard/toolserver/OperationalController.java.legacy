package com.codeguard.toolserver;

import io.javalin.Javalin;

import java.util.function.BooleanSupplier;

public final class OperationalController {
    private final BooleanSupplier ready;
    private final GatewayMetrics metrics;

    public OperationalController(BooleanSupplier ready, GatewayMetrics metrics) {
        this.ready = ready;
        this.metrics = metrics;
    }

    public void register(Javalin app) {
        app.get("/health", ctx -> ctx.result("OK"));
        app.get("/health/live", ctx -> ctx.result("OK"));
        app.get("/health/ready", ctx -> {
            boolean isReady = ready.getAsBoolean();
            ctx.status(isReady ? 200 : 503).result(isReady ? "READY" : "NOT_READY");
        });
        app.get("/metrics", ctx -> ctx.contentType("text/plain; version=0.0.4; charset=utf-8")
            .result(metrics.scrape()));
    }
}
