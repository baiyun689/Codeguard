package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.OrderEventsCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class OrderEventsWorker implements WorkerMarker {
    private final OrderEventsCoordinator coordinator;

    public OrderEventsWorker(OrderEventsCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "change-order-status" ->
                    coordinator.changeOrderStatus(payload);
            case "consume-order-event" ->
                    coordinator.consumeOrderEvent(payload);
            case "apply-versioned-event" ->
                    coordinator.applyVersionedEvent(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "order-events";
    }
}
