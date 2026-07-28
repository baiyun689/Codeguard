package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.InventoryReservationCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class InventoryReservationWorker implements WorkerMarker {
    private final InventoryReservationCoordinator coordinator;

    public InventoryReservationWorker(InventoryReservationCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "one" -> coordinator.reserveStock(payload);
            case "two" -> coordinator.reserveWithLocalLock(payload);
            case "three" -> coordinator.releaseExpiredReservation(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "inventory-reservation";
    }
}
