package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.FileExportCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class FileExportWorker implements WorkerMarker {
    private final FileExportCoordinator coordinator;

    public FileExportWorker(FileExportCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "open-export" ->
                    coordinator.openExport(payload);
            case "open-owned-export" ->
                    coordinator.openOwnedExport(payload);
            case "open-verified-export" ->
                    coordinator.openVerifiedExport(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "file-export";
    }
}
