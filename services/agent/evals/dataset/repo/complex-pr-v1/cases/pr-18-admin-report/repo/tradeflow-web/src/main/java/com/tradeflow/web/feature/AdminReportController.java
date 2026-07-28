package com.tradeflow.web.feature;

import com.tradeflow.application.feature.AdminReportCoordinator;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/admin-report")
public final class AdminReportController {
    private final AdminReportCoordinator coordinator;

    public AdminReportController(AdminReportCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    @PostMapping("/run-scheduled-export")
    public ResponseEntity<Object> runScheduledExport(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.runScheduledExport(request));
    }

    @PostMapping("/render-full-report")
    public ResponseEntity<Object> renderFullReport(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.renderFullReport(request));
    }

    @PostMapping("/render-report-row")
    public ResponseEntity<Object> renderReportRow(@RequestBody Map<String, String> request) {
        return ResponseEntity.ok(coordinator.renderReportRow(request));
    }
}
