package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.AdminReportCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class AdminReportWorker implements WorkerMarker {
    private final AdminReportCoordinator coordinator;

    public AdminReportWorker(AdminReportCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "run-scheduled-export" ->
                    coordinator.runScheduledExport(payload);
            case "render-full-report" ->
                    coordinator.renderFullReport(payload);
            case "render-report-row" ->
                    coordinator.renderReportRow(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "admin-report";
    }
}
