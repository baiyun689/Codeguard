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
            case "one" -> coordinator.openExport(payload);
            case "two" -> coordinator.openOwnedExport(payload);
            case "three" -> coordinator.openVerifiedExport(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "file-export";
    }
}
