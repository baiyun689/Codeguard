package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.OutboxDeliveryCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class OutboxDeliveryWorker implements WorkerMarker {
    private final OutboxDeliveryCoordinator coordinator;

    public OutboxDeliveryWorker(OutboxDeliveryCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "one" -> coordinator.deliverReadyEvents(payload);
            case "two" -> coordinator.deliverOneEvent(payload);
            case "three" -> coordinator.scheduleRetry(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "outbox-worker";
    }
}
