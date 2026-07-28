package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.TenantOrderUpdateCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class TenantOrderUpdateWorker implements WorkerMarker {
    private final TenantOrderUpdateCoordinator coordinator;

    public TenantOrderUpdateWorker(TenantOrderUpdateCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "update-order" ->
                    coordinator.updateOrder(payload);
            case "apply-order-changes" ->
                    coordinator.applyOrderChanges(payload);
            case "record-order-update" ->
                    coordinator.recordOrderUpdate(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "tenant-order-update";
    }
}
