package com.tradeflow.worker.feature;

import com.tradeflow.application.feature.OrderStateMachineCoordinator;
import com.tradeflow.worker.WorkerMarker;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public final class OrderStateMachineWorker implements WorkerMarker {
    private final OrderStateMachineCoordinator coordinator;

    public OrderStateMachineWorker(OrderStateMachineCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    public Object execute(String operation, Map<String, String> payload) {
        return switch (operation) {
            case "one" -> coordinator.transitionOrder(payload);
            case "two" -> coordinator.mapOrderUpdate(payload);
            case "three" -> coordinator.publishCompensation(payload);
            default -> throw new IllegalArgumentException("unknown operation");
        };
    }

    @Override
    public String workerName() {
        return "order-state-machine";
    }
}
