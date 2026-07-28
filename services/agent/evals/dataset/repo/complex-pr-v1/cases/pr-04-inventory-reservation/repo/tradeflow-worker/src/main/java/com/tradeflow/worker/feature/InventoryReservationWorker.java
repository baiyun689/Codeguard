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
            case "reserve-stock" ->
                    coordinator.reserveStock(payload);
            case "reserve-with-local-lock" ->
                    coordinator.reserveWithLocalLock(payload);
            case "release-expired-reservation" ->
                    coordinator.releaseExpiredReservation(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "inventory-reservation";
    }
}
