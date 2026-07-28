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
            case "deliver-ready-events" ->
                    coordinator.deliverReadyEvents(payload);
            case "deliver-one-event" ->
                    coordinator.deliverOneEvent(payload);
            case "schedule-retry" ->
                    coordinator.scheduleRetry(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "outbox-worker";
    }
}
