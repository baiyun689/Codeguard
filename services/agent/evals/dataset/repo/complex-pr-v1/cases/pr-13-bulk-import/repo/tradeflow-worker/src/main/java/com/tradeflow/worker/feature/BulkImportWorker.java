package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.BulkImportCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class BulkImportWorker implements WorkerMarker {
    private final BulkImportCoordinator coordinator;

    public BulkImportWorker(BulkImportCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "one" -> coordinator.resolveArchiveEntry(payload);
            case "two" -> coordinator.importRows(payload);
            case "three" -> coordinator.renderImportError(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "bulk-import";
    }
}
