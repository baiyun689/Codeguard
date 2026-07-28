package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.ShipmentIntegrationCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class ShipmentIntegrationWorker implements WorkerMarker {
    private final ShipmentIntegrationCoordinator coordinator;

    public ShipmentIntegrationWorker(ShipmentIntegrationCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "one" -> coordinator.createShipment(payload);
            case "two" -> coordinator.retryShipment(payload);
            case "three" -> coordinator.createShipmentWithTimeout(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "shipment-integration";
    }
}
