package com.codeguard.common;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import java.util.Map;
import java.util.function.BooleanSupplier;

@RestController
public final class OperationalController {
    private final BooleanSupplier ready;
    private final GatewayMetrics metrics;
    private final AlertEvaluator alertEvaluator;

    public OperationalController(BooleanSupplier ready) { this(ready, null, null); }
    public OperationalController(BooleanSupplier ready, GatewayMetrics metrics) { this(ready, metrics, null); }
    public OperationalController(BooleanSupplier ready, GatewayMetrics metrics, AlertEvaluator alertEvaluator) {
        this.ready = ready;
        this.metrics = metrics;
        this.alertEvaluator = alertEvaluator;
    }

    @GetMapping(value = {"/health", "/health/live"}, produces = "text/plain")
    public String live() { return "OK"; }

    @GetMapping(value = "/health/ready", produces = "text/plain")
    public ResponseEntity<String> ready() {
        boolean available = ready.getAsBoolean();
        return ResponseEntity.status(available ? 200 : 503).body(available ? "READY" : "NOT_READY");
    }

    @GetMapping(value = "/metrics", produces = "text/plain; version=0.0.4; charset=utf-8")
    public ResponseEntity<String> metrics() {
        return metrics == null ? ResponseEntity.notFound().build() : ResponseEntity.ok(metrics.scrape());
    }

    @GetMapping("/health/slo")
    public ResponseEntity<?> slo() {
        if (alertEvaluator == null) return ResponseEntity.notFound().build();
        var alerts = alertEvaluator.activeAlerts();
        var payload = alerts.stream().map(alert -> Map.of(
                "name", alert.name(), "severity", alert.severity(), "summary", alert.summary())).toList();
        return ResponseEntity.ok(Map.of("status", alerts.isEmpty() ? "ok" : "degraded", "alerts", payload));
    }
}
